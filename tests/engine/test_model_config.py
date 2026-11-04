"""Model configuration: requires fail-closed, budget, overrides, reporter.

Covers the section-5 remainder: role bindings may declare
``requires: {open_weight: true}`` (refused on any hosted adapter before
any model call), manifests declare a per-run token budget enforced
before the next call, role fields resolve CLI > env > profile > default
and are recorded in the run log, and the disabled reporter role can
never resolve.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from engine import cli as cli_module
from engine.audit.runlog import RunLog
from engine.core.manifest import ENGINE_API, ManifestError, validate_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute
from models.base import Completion, Usage


def _usage(
    input_tokens: int = 1,
    output_tokens: int = 1,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
) -> Usage:
    return Usage(
        model="fake",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        calls=1,
    )


class FakeClient:
    """Scripted model client counting every call it actually receives."""

    def __init__(self, usage: Usage, plans: list[dict[str, Any]] | None = None) -> None:
        self._usage = usage
        self._plans = list(plans) if plans is not None else [{"rev": 0}]
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        data = dict(self._plans.pop(0)) if self._plans else {"rev": 0}
        return Completion(data=data, usage=self._usage, meta={"model": "fake"})


def _manifest(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "id": "fake",
        "version": 1,
        "engine_api": ENGINE_API,
        "source": {"reader": "fake.reader"},
        "capabilities": {
            "intake": {"impl": "fake.intake", "reads_raw_source": True},
            "decide": {"impl": "fake.decide", "calls_model": "planner"},
            "judge": {"impl": "fake.judge", "writes_verdict": True},
            "repair": {"impl": "fake.repair", "calls_model": "planner"},
        },
        "graph": [
            {"step": "intake"},
            {
                "loop": ["decide", "judge"],
                "on_fail": "repair",
                "max_attempts": "max_attempts",
                "stage": "work",
            },
            {"gate": "review"},
        ],
        "checks": [
            {
                "id": "shape_ok",
                "kind": "constraint",
                "threshold": {"source": "literal"},
            }
        ],
        "models": {"roles": {"planner": {"model": "fake"}}},
        "limits": {},
    }
    payload.update(overrides)
    return validate_manifest(payload)


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "input.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    manifest: Any,
    registry: Mapping[str, Any],
    client: FakeClient,
    *,
    max_attempts: int = 2,
    role_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    role_binding_sources: Mapping[str, Mapping[str, str]] | None = None,
    model_policy: str = "hosted",
) -> tuple[int, dict[str, Any]]:
    script = ["accept", "a note"]

    def ask(prompt: str) -> str:
        return script.pop(0)

    return execute(
        manifest,
        registry,
        source=_source(tmp_path),
        max_attempts=max_attempts,
        model_factory=lambda role, config: client,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=ask,
        role_bindings=role_bindings,
        role_binding_sources=role_binding_sources,
        model_policy=model_policy,
    )


def _registry(failures: list[list[str]], client: FakeClient) -> dict:
    """decide calls the model; judge fails per-attempt ids; repair recalls."""

    def intake(ctx: Any) -> None:
        ctx.set_plan({"rev": 0})
        ctx.event("intake", "ok", {})

    def decide(ctx: Any) -> None:
        completion = ctx.model("planner").complete("prompt", {})
        ctx.set_plan(dict(completion.data))
        ctx.record_artifact("out.bin", "text/csv", b"data")

    def judge(ctx: Any) -> None:
        attempt = ctx.attempt_number
        wanted = failures[min(attempt - 1, len(failures) - 1)]
        ctx.record_checks(
            [
                {
                    "check_id": "shape_ok",
                    "kind": "constraint",
                    "status": "fail" if "shape_ok" in wanted else "pass",
                    "measured": "ok",
                    "threshold": {"source": "literal"},
                    "threshold_source": "literal",
                }
            ]
        )

    def repair(ctx: Any) -> None:
        completion = ctx.model("planner").complete("repair", {})
        ctx.set_plan(dict(completion.data))

    return {
        "intake": intake,
        "decide": decide,
        "judge": judge,
        "repair": repair,
    }


# --- (1) REQUIRES FAIL-CLOSED -----------------------------------------------


def _raw_manifest() -> dict:
    return {
        "id": "fake",
        "version": 1,
        "engine_api": ENGINE_API,
        "source": {"reader": "fake.reader"},
        "capabilities": {
            "decide": {"impl": "fake.decide", "calls_model": "local_critic"},
        },
        "graph": [{"step": "decide"}, {"gate": "review"}],
        "checks": [
            {
                "id": "shape_ok",
                "kind": "constraint",
                "threshold": {"source": "literal"},
            }
        ],
        "models": {
            "roles": {
                "local_critic": {
                    "model": "muse-glimmer-30B",
                    "adapter": "meta_cli",
                    "requires": {"open_weight": True},
                }
            }
        },
        "limits": {},
    }


def test_open_weight_hosted_binding_refused_at_manifest_load() -> None:
    with pytest.raises(ManifestError, match="open_weight"):
        validate_manifest(_raw_manifest())


def test_open_weight_local_binding_loads() -> None:
    payload = _raw_manifest()
    payload["models"]["roles"]["local_critic"]["adapter"] = "glimmer_local"
    manifest = validate_manifest(payload)
    assert manifest.models["roles"]["local_critic"]["adapter"] == "glimmer_local"


def test_open_weight_hosted_binding_refused_by_composition_root() -> None:
    with pytest.raises(ValueError, match="open_weight"):
        cli_module.default_client(
            "local_critic",
            {
                "model": "muse-glimmer-30B",
                "adapter": "meta_cli",
                "requires": {"open_weight": True},
            },
        )


def test_open_weight_hosted_binding_refused_before_any_model_call(
    tmp_path: Path,
) -> None:
    # validate_manifest already refuses hosted open_weight bindings, so a
    # violating manifest can only arrive programmatically: composition
    # still refuses it before any model call.
    good = _manifest()
    evil = dataclasses.replace(
        good,
        models={
            "roles": {
                "planner": {
                    "model": "fake",
                    "adapter": "meta_cli",
                    "requires": {"open_weight": True},
                }
            }
        },
    )
    client = FakeClient(_usage())
    code, outcome = _run(tmp_path, evil, _registry([[]], client), client)
    assert code == EXIT_FAILED
    assert outcome["status"] == "error"
    assert "open_weight" in outcome["error"]
    assert client.calls == 0


# --- (2) BUDGET BEFORE THE CALL ----------------------------------------------


def _budget_manifest(ceiling: int) -> Any:
    return _manifest(
        models={
            "roles": {"planner": {"model": "fake"}},
            "budget": {"max_tokens_per_run": ceiling},
        }
    )


def test_budget_refuses_second_call_before_making_it(tmp_path: Path) -> None:
    manifest = _budget_manifest(50)
    client = FakeClient(_usage(input_tokens=60, output_tokens=30))
    code, outcome = _run(
        tmp_path, manifest, _registry([["shape_ok"], []], client), client
    )
    assert code == EXIT_FAILED
    assert outcome["status"] == "error"
    assert "budget" in outcome["error"]
    # The plan call was allowed; the repair call was refused pre-call.
    assert client.calls == 1


def test_budget_counts_input_output_and_reasoning(tmp_path: Path) -> None:
    # Each call costs 4 + 1 + 1 = 6 tokens; cached tokens never count.
    # Ceiling 10: the first two calls are allowed (0 and 6 below the
    # ceiling); the third call is refused before it is made.
    manifest = _budget_manifest(10)
    client = FakeClient(
        _usage(input_tokens=4, output_tokens=1, reasoning_tokens=1, cached_tokens=1000),
        plans=[{"rev": 0}, {"rev": 1}, {"rev": 2}],
    )
    code, outcome = _run(
        tmp_path,
        manifest,
        _registry([["shape_ok"], ["shape_ok"], []], client),
        client,
        max_attempts=3,
    )
    assert code == EXIT_FAILED
    assert "budget" in outcome["error"]
    assert client.calls == 2


def test_budget_allows_small_calls(tmp_path: Path) -> None:
    manifest = _budget_manifest(10)
    client = FakeClient(_usage(), plans=[{"rev": 0}, {"rev": 1}])
    code, outcome = _run(
        tmp_path, manifest, _registry([["shape_ok"], []], client), client
    )
    assert code == EXIT_OK
    assert outcome["verdict"] == "pass"


def test_malformed_budget_refused_at_load() -> None:
    payload = _raw_manifest()
    payload["models"] = {"roles": {}, "budget": {"max_tokens_per_run": 0}}
    with pytest.raises(ManifestError, match="budget"):
        validate_manifest(payload)


# --- (3) OVERRIDE PRECEDENCE CLI > ENV > PROFILE > DEFAULT -------------------


def _roles_manifest() -> Any:
    return validate_manifest(
        {
            "id": "fake",
            "version": 1,
            "engine_api": ENGINE_API,
            "source": {"reader": "fake.reader"},
            "capabilities": {
                "decide": {"impl": "fake.decide", "calls_model": "planner"},
            },
            "graph": [{"step": "decide"}, {"gate": "review"}],
            "checks": [
                {
                    "id": "shape_ok",
                    "kind": "constraint",
                    "threshold": {"source": "literal"},
                }
            ],
            "models": {
                "roles": {
                    "planner": {
                        "model": "profile-model",
                        "adapter": "meta_cli",
                        "reasoning_effort": "low",
                    },
                    "local_critic": {
                        "model": "profile-glimmer",
                        "adapter": "glimmer_local",
                    },
                }
            },
            "limits": {},
        }
    )


def test_profile_values_apply_without_overrides() -> None:
    resolved, sources = cli_module.resolve_role_bindings(_roles_manifest(), env={})
    assert resolved["planner"]["model"] == "profile-model"
    assert resolved["planner"]["reasoning_effort"] == "low"
    assert sources["planner"] == {
        "adapter": "profile",
        "model": "profile",
        "reasoning_effort": "profile",
    }
    assert sources["local_critic"]["base_url"] == "default"
    assert resolved["local_critic"]["base_url"].startswith("http")


def test_env_beats_profile() -> None:
    resolved, sources = cli_module.resolve_role_bindings(
        _roles_manifest(),
        env={"RULOOM_ROLE_PLANNER_MODEL": "env-model"},
    )
    assert resolved["planner"]["model"] == "env-model"
    assert sources["planner"]["model"] == "env"


def test_cli_beats_env_beats_profile() -> None:
    resolved, sources = cli_module.resolve_role_bindings(
        _roles_manifest(),
        cli_models=["planner=cli-model"],
        cli_efforts=["planner=high"],
        cli_base_urls=["local_critic=http://cli:9999"],
        env={
            "RULOOM_ROLE_PLANNER_MODEL": "env-model",
            "RULOOM_ROLE_PLANNER_EFFORT": "env-effort",
            "RULOOM_ROLE_LOCAL_CRITIC_BASE_URL": "http://env:9999",
        },
    )
    assert resolved["planner"]["model"] == "cli-model"
    assert resolved["planner"]["reasoning_effort"] == "high"
    assert resolved["local_critic"]["base_url"] == "http://cli:9999"
    assert sources["planner"] == {
        "adapter": "profile",
        "model": "cli",
        "reasoning_effort": "cli",
    }
    assert sources["local_critic"]["base_url"] == "cli"


def test_role_names_match_case_insensitively() -> None:
    resolved, _ = cli_module.resolve_role_bindings(
        _roles_manifest(), cli_models=["PLANNER=upper-model"], env={}
    )
    assert resolved["planner"]["model"] == "upper-model"


def test_unknown_role_override_refused() -> None:
    with pytest.raises(ManifestError, match="unknown role"):
        cli_module.resolve_role_bindings(
            _roles_manifest(), cli_models=["nope=x"], env={}
        )


def test_bad_assignment_shape_refused() -> None:
    with pytest.raises(ValueError, match="ROLE=VALUE"):
        cli_module.parse_role_assignment("planner", "--role-model")


def test_effective_bindings_recorded_in_run_log(tmp_path: Path) -> None:
    resolved = {
        "planner": {"model": "fake", "adapter": "meta_cli", "reasoning_effort": "max"}
    }
    sources = {"planner": {"model": "profile", "reasoning_effort": "profile"}}

    def decide(ctx: Any) -> None:
        completion = ctx.model("planner").complete("prompt", {})
        ctx.record_artifact("out.bin", "text/csv", b"data")
        ctx.set_plan(dict(completion.data))

    def judge(ctx: Any) -> None:
        # No verdict writes exist: the engine decides acceptance from the
        # recorded check result alone.
        ctx.record_checks(
            [
                {
                    "check_id": "shape_ok",
                    "kind": "constraint",
                    "status": "pass",
                    "measured": "ok",
                    "threshold": {"source": "literal"},
                    "threshold_source": "literal",
                }
            ]
        )

    def pack(ctx: Any) -> None:
        (tmp_path / "out").mkdir(parents=True, exist_ok=True)
        ctx.flush_log(tmp_path / "out" / "run.jsonl")

    pack_manifest = validate_manifest(
        {
            "id": "fake",
            "version": 1,
            "engine_api": ENGINE_API,
            "source": {"reader": "fake.reader"},
            "capabilities": {
                "decide": {"impl": "fake.decide", "calls_model": "planner"},
                "judge": {"impl": "fake.judge", "writes_verdict": True},
                "pack": {"impl": "fake.pack"},
            },
            "graph": [
                {"step": "decide"},
                {"step": "judge"},
                {"step": "pack"},
                {"gate": "review"},
            ],
            "checks": [
                {
                    "id": "shape_ok",
                    "kind": "constraint",
                    "threshold": {"source": "literal"},
                }
            ],
            "models": {"roles": {"planner": {"model": "fake"}}},
            "limits": {},
        }
    )
    client = FakeClient(_usage())
    code, _ = execute(
        pack_manifest,
        {"decide": decide, "judge": judge, "pack": pack},
        source=_source(tmp_path),
        max_attempts=1,
        model_factory=lambda role, config: client,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
        role_bindings=resolved,
        role_binding_sources=sources,
    )
    assert code == EXIT_OK
    log = RunLog.read_jsonl(tmp_path / "out" / "run.jsonl")
    assert log.verify()
    bindings = next(event for event in log.events if event["stage"] == "model_bindings")
    assert bindings["status"] == "ok"
    planner = bindings["details"]["roles"]["planner"]
    assert planner["model"] == "fake"
    assert planner["sources"] == {
        "model": "profile",
        "reasoning_effort": "profile",
    }


# --- (4) DISABLED ROLE CANNOT BE RESOLVED ------------------------------------


def test_reporter_seat_exists_but_stays_disabled() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = cli_module.load_manifest(root / "profiles" / "hylogger" / "profile.json")
    reporter = manifest.models["roles"]["reporter"]
    assert reporter["adapter"] == "meta_cli"
    assert reporter["model"] == "muse-wonder-1.0"
    assert reporter["enabled"] is False
    assert "reporter" not in manifest.capabilities


def test_capability_bound_to_reporter_refused_at_composition(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = cli_module.load_manifest(root / "profiles" / "hylogger" / "profile.json")
    capabilities = dict(manifest.capabilities)
    capabilities["snoop"] = {"impl": "fake.snoop", "calls_model": "reporter"}
    graph = list(manifest.graph) + [{"step": "snoop"}]
    variant = dataclasses.replace(
        manifest,
        capabilities=capabilities,
        graph=[dict(node) for node in graph],
    )
    client = FakeClient(_usage())
    registry = {capability: (lambda ctx: None) for capability in capabilities}
    registry["snoop"] = lambda ctx: ctx.model("reporter")
    code, outcome = _run(tmp_path, variant, registry, client)
    assert code == EXIT_FAILED
    assert outcome["status"] == "error"
    assert "reporter" in outcome["error"]
    assert "disabled" in outcome["error"]
    assert client.calls == 0


# --- (5) AZURE FOUNDRY ROLE CONFIG RESOLUTION ---------------------------------


def test_azure_foundry_role_binding_defaults_and_validation() -> None:
    manifest = _manifest()
    # Missing base_url
    with pytest.raises(ManifestError, match="requires explicit base_url"):
        cli_module.resolve_role_bindings(
            manifest,
            cli_adapters=["planner=azure_foundry"],
            cli_subscription_id="11111111-2222-3333-4444-555555555555",
            env={},
        )

    # Missing subscription_id
    with pytest.raises(ManifestError, match="requires explicit subscription_id"):
        cli_module.resolve_role_bindings(
            manifest,
            cli_adapters=["planner=azure_foundry"],
            cli_base_urls=["planner=https://audit-fixture.openai.azure.com/openai/v1/"],
            env={},
        )

    # Valid binding with defaults
    resolved, sources = cli_module.resolve_role_bindings(
        manifest,
        cli_adapters=["planner=azure_foundry"],
        cli_base_urls=["planner=https://audit-fixture.openai.azure.com/openai/v1/"],
        cli_subscription_id="11111111-2222-3333-4444-555555555555",
        env={},
    )
    planner = resolved["planner"]
    assert planner["adapter"] == "azure_foundry"
    assert planner["model"] == "muse-spark-1.3"
    assert planner["provider"] == "azure"
    assert planner["family"] == "meta"
    assert planner["reasoning_effort"] == "xhigh"
    assert planner["base_url"] == "https://audit-fixture.openai.azure.com/openai/v1/"
    assert planner["subscription_id"] == "11111111-2222-3333-4444-555555555555"
    assert sources["planner"]["reasoning_effort"] == "default"


def test_azure_foundry_precedence_cli_over_env() -> None:
    manifest = _manifest()
    sub_cli = "11111111-2222-3333-4444-555555555555"
    sub_env = "22222222-3333-4444-5555-666666666666"
    url_cli = "https://cli-account.openai.azure.com/openai/v1/"
    url_env = "https://env-account.openai.azure.com/openai/v1/"

    resolved, sources = cli_module.resolve_role_bindings(
        manifest,
        cli_adapters=["planner=azure_foundry"],
        cli_base_urls=[f"planner={url_cli}"],
        cli_subscription_id=sub_cli,
        env={
            "RULOOM_AZURE_SUBSCRIPTION_ID": sub_env,
            "RULOOM_ROLE_PLANNER_BASE_URL": url_env,
        },
    )
    assert resolved["planner"]["subscription_id"] == sub_cli
    assert sources["planner"]["subscription_id"] == "cli"
    assert resolved["planner"]["base_url"] == url_cli
    assert sources["planner"]["base_url"] == "cli"

    # Env used when CLI not provided
    resolved_env, sources_env = cli_module.resolve_role_bindings(
        manifest,
        cli_adapters=["planner=azure_foundry"],
        cli_subscription_id=None,
        env={
            "RULOOM_AZURE_SUBSCRIPTION_ID": sub_env,
            "RULOOM_ROLE_PLANNER_BASE_URL": url_env,
        },
    )
    assert resolved_env["planner"]["subscription_id"] == sub_env
    assert sources_env["planner"]["subscription_id"] == "env"
    assert resolved_env["planner"]["base_url"] == url_env
    assert sources_env["planner"]["base_url"] == "env"


def test_azure_foundry_recorded_in_runlog(tmp_path: Path) -> None:
    manifest = _manifest()
    sub_id = "11111111-2222-3333-4444-555555555555"
    base_url = "https://audit-fixture.openai.azure.com/openai/v1/"
    resolved, role_sources = cli_module.resolve_role_bindings(
        manifest,
        cli_adapters=["planner=azure_foundry"],
        cli_base_urls=[f"planner={base_url}"],
        cli_subscription_id=sub_id,
        env={},
    )
    client = FakeClient(_usage())

    def intake(ctx: Any) -> None:
        ctx.set_plan({"rev": 0})

    def decide(ctx: Any) -> None:
        ctx.model("planner").complete("p", {"type": "object"})
        ctx.record_artifact("out.bin", "text/csv", b"data")

    def judge(ctx: Any) -> None:
        ctx.record_checks(
            [
                {
                    "check_id": "shape_ok",
                    "kind": "constraint",
                    "status": "pass",
                    "measured": "ok",
                    "threshold": {"source": "literal"},
                    "threshold_source": "literal",
                }
            ]
        )

    code, outcome = _run(
        tmp_path,
        manifest,
        {
            "intake": intake,
            "decide": decide,
            "judge": judge,
            "repair": lambda ctx: None,
        },
        client,
        role_bindings=resolved,
        role_binding_sources=role_sources,
        model_policy="azure-only",
    )
    assert code == EXIT_OK
    log = RunLog.read_jsonl(tmp_path / "out" / "runs" / outcome["run_id"] / "run.jsonl")
    bindings_events = [e for e in log.events if e["stage"] == "model_bindings"]
    assert len(bindings_events) == 1
    roles = bindings_events[0]["details"]["roles"]
    assert roles["planner"]["adapter"] == "azure_foundry"
    assert roles["planner"]["base_url"] == base_url
    assert roles["planner"]["subscription_id"] == sub_id


def test_azure_foundry_budget_accounting_avoids_double_counting(
    tmp_path: Path,
) -> None:
    from engine.core.runner import usage_tokens

    # 1. Direct usage_tokens function test:
    # Sample from root smoke: 227 prompt + 1054 completion incl 977 reasoning
    smoke_usage = _usage(
        input_tokens=227,
        output_tokens=1054,
        reasoning_tokens=977,
        cached_tokens=0,
    )
    # With adapter="azure_foundry", output_tokens already includes reasoning:
    # 227 + 1054 = 1281
    assert usage_tokens(smoke_usage, adapter="azure_foundry") == 1281
    # Legacy adapter (default) sums all three: 227 + 1054 + 977 = 2258
    assert usage_tokens(smoke_usage) == 2258

    # 2. Runner budget test:
    # Manifest ceiling: 1500 tokens.
    # First call: 227 + 1054 (977 reasoning).
    # If double-counted (2258), the runner would exceed 1500 and refuse attempt 2.
    # With correct accounting (1281 < 1500), attempt 2 IS admitted!
    manifest = _budget_manifest(1500)
    sub_id = "11111111-2222-3333-4444-555555555555"
    base_url = "https://audit-fixture.openai.azure.com/openai/v1/"
    resolved, role_sources = cli_module.resolve_role_bindings(
        manifest,
        cli_adapters=["planner=azure_foundry"],
        cli_base_urls=[f"planner={base_url}"],
        cli_subscription_id=sub_id,
        env={},
    )

    usages = [
        _usage(input_tokens=227, output_tokens=1054, reasoning_tokens=977),
        _usage(input_tokens=50, output_tokens=50, reasoning_tokens=0),
        _usage(input_tokens=50, output_tokens=50, reasoning_tokens=0),
    ]
    call_idx = 0

    class SequenceClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
            nonlocal call_idx
            u = usages[call_idx]
            call_idx += 1
            self.calls += 1
            return Completion(
                data={"seed": 42, "n_rows": 100},
                usage=u,
                meta={
                    "provider": "azure",
                    "request_id": f"req-{self.calls}",
                    "model": "muse-spark-1.3",
                },
            )

    client = SequenceClient()
    # Attempt 1 fails check; repair is called; attempt 2 passes check.
    code, outcome = _run(
        tmp_path,
        manifest,
        _registry([["shape_ok"], []], client),  # type: ignore
        client,  # type: ignore
        role_bindings=resolved,
        role_binding_sources=role_sources,
        model_policy="azure-only",
    )
    assert code == EXIT_OK
    assert client.calls == 3
    # Verify attempt tokens recorded in attempt records:
    # Attempt 1 token cost must be exactly 1281 (not 2258)
    run_dir = tmp_path / "out" / "runs" / outcome["run_id"]
    receipt1 = json.loads(
        (run_dir / "attempts" / "attempt-1" / "receipt.json").read_text()
    )
    assert receipt1["tokens"] == 1281


def test_budgeted_client_refuses_vision_critic_pre_call(tmp_path: Path) -> None:
    from engine.core.runner import _BudgetedClient, _Runner
    from models.azure_foundry import AzureFoundryModel

    model = AzureFoundryModel(
        base_url="https://audit-fixture.openai.azure.com/openai/v1/",
        subscription_id="11111111-2222-3333-4444-555555555555",
        token_provider=lambda s: "tok",
    )
    manifest = _manifest()
    runner = _Runner(
        manifest=manifest,
        registry={
            "intake": lambda c: None,
            "decide": lambda c: None,
            "judge": lambda c: None,
            "repair": lambda c: None,
        },
        source=_source(tmp_path),
        max_attempts=2,
        model_factory=lambda r, c: model,
        out=tmp_path / "out",
        say=lambda m: None,
        ask=lambda p: "",
    )
    budgeted = _BudgetedClient(model, runner, "critic")

    with pytest.raises(AttributeError, match="has no method 'complete_with_image'"):
        budgeted.complete_with_image("prompt", {}, tmp_path / "img.png")

    # Verify no model_call_started event was logged in runner.log
    started_events = [
        e for e in runner.log.events if e["stage"] == "model_call_started"
    ]
    assert len(started_events) == 0
