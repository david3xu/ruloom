"""Fixture-only call-policy regressions: no live models or paid requests."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from engine import cli
from engine.audit.runlog import RunLog
from engine.core.manifest import load_manifest
from engine.core.model_policy import ModelPolicyError, enforce_model_policy
from engine.core.runner import execute
from models.base import Completion, Usage
from profiles import manifest_path
from tests.engine.test_plan_replay import (
    RefusingModel,
    _run_dirs,
    _write_hylogger_source,
    _write_hylogger_thresholds,
    _write_tabular_source,
)


@pytest.mark.parametrize("policy", ["offline", "local-only", "unknown"])
def test_hosted_call_denied_before_factory(tmp_path: Path, policy: str) -> None:
    source = _write_tabular_source(tmp_path)
    manifest = load_manifest(manifest_path("tabular"))
    made = []

    def factory(role, config):
        made.append(role)
        raise AssertionError("must not construct a hosted client")

    code, outcome = execute(
        manifest,
        cli.load_registry("tabular"),
        source=source,
        max_attempts=1,
        model_factory=factory,
        out=tmp_path / "out",
        model_policy=policy,
        say=lambda message: None,
    )
    assert code == 1
    assert made == []
    assert outcome["status"] in ("error", "refused")
    assert not list((tmp_path / "out").rglob("release.zip"))


@pytest.mark.parametrize("profile", ["tabular", "hylogger"])
def test_offline_replay_runs_all_checks_without_models(tmp_path, monkeypatch, profile):
    if profile == "tabular":
        source = _write_tabular_source(tmp_path)
        rows = 20
    else:
        source = _write_hylogger_source(tmp_path)
        monkeypatch.setenv(
            "RULOOM_THRESHOLDS", str(_write_hylogger_thresholds(tmp_path))
        )
        rows = 8
    plan = tmp_path / "saved.json"
    plan.write_text(json.dumps({"seed": 8, "n_rows": rows}))
    model = RefusingModel()
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nFixture only.\n"))
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "run",
                str(source),
                "--profile",
                profile,
                "--max-attempts",
                "2",
                "--out",
                str(out),
                "--model-policy",
                "offline",
                "--plan",
                str(plan),
            ],
            model=model,
        )
        == 0
    )
    assert model.calls == 0
    run = _run_dirs(out)[0]
    record = json.loads((run / "run.json").read_text())
    assert record["run"]["request"]["model_policy"] == "offline"
    log = RunLog.read_jsonl(run / "run.jsonl")
    assert log.verify()
    assert not [e for e in log.events if e["stage"] == "model_invocation"]
    report = next(out.rglob("evidence-report.md")).read_text()
    assert "saved-plan replay" in report
    assert "Model policy: offline" in report


def test_offline_failure_keeps_checks_and_never_repairs(tmp_path, monkeypatch):
    source = _write_tabular_source(tmp_path)
    (tmp_path / "tabular-thresholds.json").write_text('{"mean_relative_tolerance": 0}')
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(tmp_path / "tabular-thresholds.json"))
    model = RefusingModel()
    manifest = load_manifest(manifest_path("tabular"))
    code, outcome = execute(
        manifest,
        cli.load_registry("tabular"),
        source=source,
        max_attempts=3,
        model_factory=lambda role, config: model,
        out=tmp_path / "out",
        fixed_plan={"seed": 8, "n_rows": 20},
        model_policy="offline",
        ask=lambda prompt: pytest.fail("failed data cannot reach human acceptance"),
        say=lambda message: None,
    )
    assert code == 1
    assert outcome["status"] == "failed"
    assert outcome["verdict"] == "fail"
    assert outcome["attempts"] == 1
    assert "mean_value" in outcome["failed"]
    assert model.calls == 0
    assert not list((tmp_path / "out").rglob("release.zip"))
    log = RunLog.read_jsonl(_run_dirs(tmp_path / "out")[0] / "run.jsonl")
    assert any(e["stage"] == "model_policy_denied" for e in log.events)


@pytest.mark.parametrize("critic_policy", ["optional", "required"])
def test_offline_critic_denial_honours_required_status(tmp_path, critic_policy):
    source = _write_tabular_source(tmp_path)
    manifest = load_manifest(manifest_path("hylogger"))
    import copy
    import dataclasses

    capabilities = copy.deepcopy(manifest.capabilities)
    capabilities["image_critique"]["critic_policy"] = critic_policy
    manifest = dataclasses.replace(
        manifest, graph=[{"step": "image_critique"}], capabilities=capabilities
    )
    made = []

    def critic(ctx):
        ctx.model("critic").complete("fixture", {})

    code, outcome = execute(
        manifest,
        {**cli.load_registry("hylogger"), "image_critique": critic},
        source=source,
        max_attempts=1,
        model_factory=lambda *args: made.append(args),
        out=tmp_path / "out",
        model_policy="offline",
        say=lambda message: None,
    )
    assert made == []
    if critic_policy == "optional":
        assert code == 0  # unavailable advisory, never a successful critique
        assert "offline" in str(outcome["advisories"])
    else:
        assert code == 1
        assert outcome["status"] == "error"
        assert "offline" in outcome["error"]
    assert not list((tmp_path / "out").rglob("release.zip"))


def test_model_failure_is_recorded_without_successful_identity(tmp_path):
    from models.base import ModelError

    class BrokenClient:
        def complete(self, prompt, schema):
            raise ModelError("fixture unavailable")

    code, outcome = execute(
        load_manifest(manifest_path("tabular")),
        cli.load_registry("tabular"),
        source=_write_tabular_source(tmp_path),
        max_attempts=1,
        out=tmp_path / "out",
        model_factory=lambda *args: BrokenClient(),
        say=lambda message: None,
    )
    assert code == 1 and outcome["status"] == "error"
    log = RunLog.read_jsonl(_run_dirs(tmp_path / "out")[0] / "run.jsonl")
    assert (
        len([event for event in log.events if event["stage"] == "model_call_started"])
        == 1
    )
    assert (
        len([event for event in log.events if event["stage"] == "model_call_failed"])
        == 1
    )
    assert not [event for event in log.events if event["stage"] == "model_invocation"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8080",
        "http://127.0.0.1@example.com",
        "http://127.0.0.1:8080?x=1",
        "http://127.0.0.1:8080#frag",
        "http://0.0.0.0",
        "file:///tmp/model",
        "http://127.0.0.1:bad",
        "http://[::ffff:8.8.8.8]:80",
    ],
)
def test_local_policy_refuses_remote_or_ambiguous_urls(url):
    with pytest.raises(ModelPolicyError):
        enforce_model_policy(
            "local-only", {"adapter": "glimmer_local", "base_url": url}
        )


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://[::1]:8080/v1"])
def test_local_policy_accepts_literal_loopback(url):
    enforce_model_policy("local-only", {"adapter": "glimmer_local", "base_url": url})


def test_local_adapter_override_changes_default_model_and_factory_flag(tmp_path):
    manifest = load_manifest(manifest_path("tabular"))
    resolved, sources = cli.resolve_role_bindings(
        manifest,
        cli_adapters=["planner=glimmer_local"],
        env={},
    )
    assert resolved["planner"]["model"] == "muse-glimmer-30B"
    assert sources["planner"]["adapter"] == "cli"
    calls = []

    class FixtureClient:
        def complete(self, prompt, schema):
            return Completion(
                {"seed": 8, "n_rows": 20}, Usage(model="fixture-local", calls=1)
            )

    def factory(role, config):
        calls.append(config)
        return FixtureClient()

    code, _ = execute(
        manifest,
        cli.load_registry("tabular"),
        source=_write_tabular_source(tmp_path),
        max_attempts=1,
        out=tmp_path / "out",
        model_factory=factory,
        role_bindings=resolved,
        model_policy="local-only",
        ask=lambda prompt: "accept" if prompt.startswith("Decision") else "Fixture.",
        say=lambda message: None,
    )
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["local_only"] is True


@pytest.mark.parametrize("target", [True, 0, -1, 200001])
def test_invalid_exact_row_target_refuses_before_model(tmp_path, target):
    source = _write_tabular_source(tmp_path)
    code, outcome = execute(
        load_manifest(manifest_path("tabular")),
        cli.load_registry("tabular"),
        source=source,
        max_attempts=1,
        out=tmp_path / "out",
        model_factory=lambda *args: pytest.fail("invalid target must not call a model"),
        spec={"target_rows": target},
        say=lambda message: None,
    )
    assert code == 1 and outcome["status"] == "refused"


def test_saved_plan_cannot_change_exact_row_target(tmp_path):
    code, outcome = execute(
        load_manifest(manifest_path("tabular")),
        cli.load_registry("tabular"),
        source=_write_tabular_source(tmp_path),
        max_attempts=1,
        out=tmp_path / "out",
        model_factory=lambda *args: pytest.fail("replay must not call a model"),
        spec={"target_rows": 10},
        fixed_plan={"seed": 8, "n_rows": 20},
        model_policy="offline",
        say=lambda message: None,
    )
    assert code == 1
    assert "target_rows 10" in outcome["error"]
    assert outcome["attempts"] == 0
