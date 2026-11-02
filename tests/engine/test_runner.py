"""Engine graph execution with a fake profile.

Covers step/loop/gate flow, the loop bound, best-attempt tracking, the
no-improvement stop, and every mechanical permission denial: raw source
refused, model client withheld, advisory outputs excluded from pass/fail,
and verdict writes restricted to declared capabilities.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from engine.core.manifest import ENGINE_API, validate_manifest
from engine.core.runner import (
    EXIT_DECLINED,
    EXIT_FAILED,
    EXIT_OK,
    PermissionDenied,
    RunnerError,
    execute,
)
from models.base import Completion, Usage


def _usage() -> Usage:
    return Usage(model="fake", input_tokens=1, output_tokens=1, calls=1)


class FakeClient:
    """Scripted model client returning queued payloads."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        if self._payloads:
            self._last = dict(self._payloads.pop(0))
        return Completion(
            data=dict(getattr(self, "_last", {})),
            usage=_usage(),
            meta={"model": "fake"},
        )


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
            "note": {"impl": "fake.note", "advisory": True},
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
            },
            {
                "id": "content_ok",
                "kind": "fidelity",
                "threshold": {"source": "relative_tolerance"},
            },
        ],
        "models": {"roles": {"planner": {"model": "fake"}}},
        "limits": {},
    }
    payload.update(overrides)
    return validate_manifest(payload)


def _source(tmp_path: Path, text: str = "a,b\n1,2\n") -> Path:
    path = tmp_path / "input.csv"
    path.write_text(text, encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    registry: Mapping[str, Any],
    manifest: Any = None,
    *,
    max_attempts: int = 3,
    answers: list[str] | None = None,
    client_payloads: list[dict[str, Any]] | None = None,
    said: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    lines: list[str] = []
    manifest = manifest or _manifest()

    def factory(role: str, config: Mapping[str, Any]) -> FakeClient:
        return FakeClient(list(client_payloads or []))

    script = list(answers or ["accept", "a note"])

    def ask(prompt: str) -> str:
        lines.append(prompt)
        return script.pop(0)

    code, outcome = execute(
        manifest,
        registry,
        source=_source(tmp_path),
        max_attempts=max_attempts,
        model_factory=factory,
        out=tmp_path / "out",
        say=(said.append if said is not None else lines.append),
        ask=ask,
    )
    return code, outcome


def _passing_registry(failures: list[list[str]], log: dict) -> dict:
    """A registry whose judge fails per-attempt ids from ``failures``."""

    def intake(ctx: Any) -> None:
        raw = ctx.raw_bytes()
        assert raw == b"a,b\n1,2\n"
        ctx.set_plan({"rev": 0})
        ctx.event("intake", "ok", {})

    def decide(ctx: Any) -> None:
        client = ctx.model("planner")
        completion = client.complete("prompt", {})
        ctx.set_plan(dict(completion.data))
        payload = f"payload-rev-{completion.data.get('rev', 0)}".encode()
        ctx.record_artifact("out.bin", "text/csv", payload)
        ctx.record_advisory("note", {"rev": completion.data.get("rev", 0)})

    def judge(ctx: Any) -> None:
        attempt = ctx.attempt_number
        wanted = failures[min(attempt - 1, len(failures) - 1)]
        items = [
            {
                "check_id": "shape_ok",
                "kind": "constraint",
                "status": "fail" if "shape_ok" in wanted else "pass",
                "measured": "ok",
                "threshold": {"source": "literal"},
                "threshold_source": "literal",
            },
            {
                "check_id": "content_ok",
                "kind": "fidelity",
                "status": "fail" if "content_ok" in wanted else "pass",
                "measured": "ok",
                "threshold": {"source": "relative_tolerance"},
                "threshold_source": "relative_tolerance",
            },
        ]
        ctx.record_checks(items)

    def repair(ctx: Any) -> None:
        log["repairs"] = log.get("repairs", 0) + 1
        client = ctx.model("planner")
        completion = client.complete("repair", {})
        ctx.set_plan(dict(completion.data))

    def note(ctx: Any) -> None:
        ctx.record_advisory("summary", {"done": True})

    return {
        "intake": intake,
        "decide": decide,
        "judge": judge,
        "repair": repair,
        "note": note,
    }


def test_pass_first_attempt_releases(tmp_path: Path) -> None:
    log: dict = {}
    said: list[str] = []
    code, outcome = _run(
        tmp_path,
        _passing_registry([[]], log),
        client_payloads=[{"rev": 0}],
        said=said,
        answers=["accept", "looks good"],
    )
    assert code == EXIT_OK
    assert outcome["status"] == "released"
    assert outcome["verdict"] == "pass"
    assert outcome["attempts"] == 1
    assert log.get("repairs", 0) == 0
    # Advisory outputs are recorded but never decide the verdict.
    assert outcome["advisories"]
    assert outcome["artifact_sha256"]


def test_repair_to_pass_in_two_attempts(tmp_path: Path) -> None:
    log: dict = {}
    code, outcome = _run(
        tmp_path,
        _passing_registry([["content_ok"], []], log),
        client_payloads=[{"rev": 0}, {"rev": 1}],
        max_attempts=2,
    )
    assert code == EXIT_OK
    assert outcome["verdict"] == "pass"
    assert outcome["attempts"] == 2
    assert log["repairs"] == 1


def test_bound_means_no_repair_call(tmp_path: Path) -> None:
    log: dict = {}
    code, outcome = _run(
        tmp_path,
        _passing_registry([["content_ok"]], log),
        client_payloads=[{"rev": 0}],
        max_attempts=1,
    )
    assert code == EXIT_FAILED
    assert outcome["status"] == "failed"
    assert log.get("repairs", 0) == 0
    assert outcome["failed"] == ["content_ok"]


def test_no_improvement_stops_early(tmp_path: Path) -> None:
    log: dict = {}
    code, outcome = _run(
        tmp_path,
        _passing_registry([["shape_ok"], ["shape_ok", "content_ok"]], log),
        client_payloads=[{"rev": 0}, {"rev": 1}, {"rev": 2}],
        max_attempts=5,
    )
    assert code == EXIT_FAILED
    assert outcome["attempts"] == 2
    assert log["repairs"] == 1


def test_best_attempt_is_kept(tmp_path: Path) -> None:
    log: dict = {}
    code, outcome = _run(
        tmp_path,
        _passing_registry([["shape_ok", "content_ok"], ["content_ok"]], log),
        client_payloads=[{"rev": 0}, {"rev": 1}],
        max_attempts=2,
    )
    assert code == EXIT_FAILED
    assert outcome["failed"] == ["content_ok"]
    assert outcome["artifact_sha256"].encode() is not None


def test_raw_source_refused_without_permission(tmp_path: Path) -> None:
    def sneaky(ctx: Any) -> None:
        ctx.raw_bytes()

    manifest = _manifest(
        capabilities={
            "sneaky": {"impl": "fake.sneaky"},
        },
        graph=[{"step": "sneaky"}, {"gate": "review"}],
        checks=[
            {
                "id": "shape_ok",
                "kind": "constraint",
                "threshold": {"source": "literal"},
            }
        ],
    )
    with pytest.raises(PermissionDenied):
        sneaky(_scoped(tmp_path, manifest, "sneaky"))


def test_source_path_refused_without_grant(tmp_path: Path) -> None:
    manifest = _manifest()
    ctx = _scoped(tmp_path, manifest, "judge")
    with pytest.raises(PermissionDenied):
        ctx.source_path


def test_source_frame_refused_without_grant(tmp_path: Path) -> None:
    manifest = _manifest()
    ctx = _scoped(tmp_path, manifest, "judge")
    with pytest.raises(PermissionDenied):
        ctx.source_frame()
    with pytest.raises(PermissionDenied):
        ctx.set_source_frame([{"a": "1"}])


def test_companion_dir_refused_without_grant(tmp_path: Path) -> None:
    manifest = _manifest()
    ctx = _scoped(tmp_path, manifest, "judge")
    with pytest.raises(PermissionDenied):
        ctx.images_dir


def test_capability_context_exposes_no_mutable_state(tmp_path: Path) -> None:
    manifest = _manifest()
    ctx = _scoped(tmp_path, manifest, "judge")
    for forbidden in ("state", "manifest", "runner", "_runner", "verdict"):
        assert not hasattr(ctx, forbidden), forbidden


def _scoped(tmp_path: Path, manifest: Any, capability_id: str) -> Any:
    from engine.core.runner import _Runner

    runner = _Runner(
        manifest=manifest,
        registry={cap: (lambda ctx: None) for cap in manifest.capabilities},
        source=_source(tmp_path),
        max_attempts=1,
        model_factory=lambda role, config: FakeClient([{}]),
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "",
    )
    return runner.scoped(capability_id)


def test_model_withheld_without_grant(tmp_path: Path) -> None:
    manifest = _manifest()
    ctx = _scoped(tmp_path, manifest, "judge")
    with pytest.raises(PermissionDenied):
        ctx.model("planner")


def test_model_role_mismatch_denied(tmp_path: Path) -> None:
    # A capability bound to an unbound role is refused at composition:
    # no client is ever built for it.
    manifest = _manifest(
        capabilities={
            "asker": {"impl": "fake.asker", "calls_model": "critic"},
        },
        graph=[{"step": "asker"}, {"gate": "review"}],
    )
    from engine.core.runner import _Runner

    with pytest.raises(RunnerError):
        _Runner(
            manifest=manifest,
            registry={"asker": (lambda ctx: None)},
            source=_source(tmp_path),
            max_attempts=1,
            model_factory=lambda role, config: FakeClient([{}]),
            out=tmp_path / "out",
            say=lambda message: None,
            ask=lambda prompt: "",
        )


def test_disabled_role_is_missing(tmp_path: Path) -> None:
    # A capability bound to a disabled role is refused at composition:
    # the runner never reaches the capability, let alone a model call.
    manifest = _manifest(
        models={"roles": {"planner": {"model": "fake", "enabled": False}}}
    )
    with pytest.raises(RunnerError):
        _scoped(tmp_path, manifest, "decide")


def test_writes_verdict_required(tmp_path: Path) -> None:
    manifest = _manifest()
    ctx = _scoped(tmp_path, manifest, "decide")
    with pytest.raises(PermissionDenied):
        ctx.record_checks([])


def test_undeclared_check_rejected(tmp_path: Path) -> None:
    def judge(ctx: Any) -> None:
        ctx.record_artifact("out.bin", "text/csv", b"data")
        ctx.record_checks(
            [
                {
                    "check_id": "invented_check",
                    "kind": "constraint",
                    "status": "pass",
                    "measured": None,
                    "threshold": None,
                    "threshold_source": "literal",
                }
            ]
        )

    manifest = _manifest(
        capabilities={
            "judge": {"impl": "fake.judge", "writes_verdict": True},
        },
        graph=[{"step": "judge"}, {"gate": "review"}],
    )
    code, outcome = _run(tmp_path, {"judge": judge}, manifest=manifest)
    assert code == EXIT_FAILED
    assert outcome["status"] == "error"


def test_advisory_excluded_from_verdict(tmp_path: Path) -> None:
    def judge(ctx: Any) -> None:
        ctx.record_artifact("out.bin", "text/csv", b"data")
        ctx.record_checks(
            [
                {
                    "check_id": "shape_ok",
                    "kind": "constraint",
                    "status": "fail",
                    "measured": "bad",
                    "threshold": {"source": "literal"},
                    "threshold_source": "literal",
                }
            ]
        )
        ctx.record_advisory("cheerful", {"assessment": "looks fine"})

    def retry(ctx: Any) -> None:
        return None

    manifest = _manifest(
        capabilities={
            "judge": {"impl": "fake.judge", "writes_verdict": True},
            "retry": {"impl": "fake.retry"},
        },
        graph=[
            {
                "loop": ["judge"],
                "on_fail": "retry",
                "max_attempts": "max_attempts",
            },
            {"gate": "review"},
        ],
        checks=[
            {
                "id": "shape_ok",
                "kind": "constraint",
                "threshold": {"source": "literal"},
            }
        ],
    )
    code, outcome = _run(
        tmp_path,
        {"judge": judge, "retry": retry},
        manifest=manifest,
        max_attempts=1,
    )
    assert code == EXIT_FAILED
    assert outcome["failed"] == ["shape_ok"]
    assert outcome["advisories"] == [
        {
            "capability": "judge",
            "name": "cheerful",
            "payload": {"assessment": "looks fine"},
        }
    ]


def test_max_attempts_has_no_default(tmp_path: Path) -> None:
    manifest = _manifest()
    with pytest.raises(TypeError):
        execute(
            manifest,
            {"intake": (lambda ctx: None)},
            source=_source(tmp_path),
            model_factory=lambda role, config: FakeClient([{}]),
            out=tmp_path / "out",
        )


def test_gate_modify_declines_without_release(tmp_path: Path) -> None:
    log: dict = {}
    code, outcome = _run(
        tmp_path,
        _passing_registry([[]], log),
        client_payloads=[{"rev": 0}],
        answers=["modify", "needs work"],
    )
    assert code == EXIT_DECLINED
    assert outcome["status"] == "declined"


def test_gate_reject_declines(tmp_path: Path) -> None:
    log: dict = {}
    code, outcome = _run(
        tmp_path,
        _passing_registry([[]], log),
        client_payloads=[{"rev": 0}],
        answers=["reject", "no"],
    )
    assert code == EXIT_DECLINED


def test_gate_invalid_declines(tmp_path: Path) -> None:
    log: dict = {}
    code, outcome = _run(
        tmp_path,
        _passing_registry([[]], log),
        client_payloads=[{"rev": 0}],
        answers=["maybe", "unsure"],
    )
    assert code == EXIT_DECLINED
    assert outcome["status"] == "invalid"
