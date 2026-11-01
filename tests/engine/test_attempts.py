"""Immutable attempt records: the selected best attempt is a whole record.

Selecting a best attempt selects the plan, seed, artifact manifest,
check results, and resource use bound together at that attempt; the
packager writes the selected attempt's own plan, seed, and artifact
triple. These scripted scenarios pin the selection paths the audit
probe exposed (best-first, best-middle, tied, final-success) plus the
record field binding (manifest digest, source hash, seed, tokens).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from engine.core.manifest import ENGINE_API, validate_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute
from models.base import Completion, Usage


def _usage() -> Usage:
    return Usage(model="fake", input_tokens=2, output_tokens=1, calls=1)


class FakeClient:
    """Scripted model client replaying queued plan payloads."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = list(payloads)

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data=dict(self._payloads.pop(0)),
            usage=_usage(),
            meta={"model": "fake"},
        )


def _attempt_manifest() -> Any:
    payload: dict[str, Any] = {
        "id": "fake-attempts",
        "version": 1,
        "engine_api": ENGINE_API,
        "source": {"reader": "fake.reader"},
        "capabilities": {
            "intake": {"impl": "fake.intake"},
            "decide": {"impl": "fake.decide", "calls_model": "planner"},
            "judge": {"impl": "fake.judge", "writes_verdict": True},
            "repair": {"impl": "fake.repair", "calls_model": "planner"},
            "package": {"impl": "fake.package"},
        },
        "graph": [
            {"step": "intake"},
            {
                "loop": ["decide", "judge"],
                "on_fail": "repair",
                "max_attempts": "max_attempts",
                "stage": "work",
            },
            {"step": "package"},
            {"gate": "review"},
        ],
        "checks": [
            {
                "id": "check_a",
                "kind": "constraint",
                "threshold": {"source": "literal"},
                "required": True,
            },
            {
                "id": "check_b",
                "kind": "constraint",
                "threshold": {"source": "literal"},
                "required": True,
            },
        ],
        "models": {"roles": {"planner": {"model": "fake"}}},
        "limits": {},
    }
    return validate_manifest(payload)


def _registry(
    scripted_plans: list[dict[str, Any]],
    judge_failures: list[list[str]],
    packaged: dict[str, Any],
) -> dict[str, Any]:
    plan_queue = [dict(p) for p in scripted_plans]
    failure_queue = [list(f) for f in judge_failures]

    def intake(ctx: Any) -> None:
        ctx.set_source_meta(
            name="attempt-source",
            sha256="SRC123",
            licence=None,
            attribution=None,
        )
        ctx.event("intake", "ok", {})

    def _next_plan(ctx: Any) -> dict[str, Any]:
        assert plan_queue, "scripted plan queue exhausted"
        completion = ctx.model("planner").complete("prompt", {})
        plan = dict(plan_queue.pop(0))
        assert plan == dict(completion.data)
        return plan

    def decide(ctx: Any) -> None:
        plan = _next_plan(ctx)
        ctx.set_plan(plan)
        ctx.record_artifact(
            "out.bin", "text/csv", f"payload-{plan['seed']}".encode()
        )

    def judge(ctx: Any) -> None:
        failed = failure_queue.pop(0)
        items = [
            {
                "check_id": check_id,
                "kind": "constraint",
                "status": "fail" if check_id in failed else "pass",
                "measured": check_id,
                "threshold": {"source": "literal"},
                "threshold_source": "literal",
                "detail": check_id,
            }
            for check_id in ("check_a", "check_b")
        ]
        ctx.record_checks(items)

    def repair(ctx: Any) -> None:
        ctx.set_plan(_next_plan(ctx))

    def package(ctx: Any) -> None:
        sha = ctx.selected_artifact_sha()
        payload = ctx.artifact_payload(sha)
        plan = ctx.selected_plan()
        artifact_dir = ctx.prepare_artifact_dir()
        packaged.update(
            {
                "plan": dict(plan),
                "seed": plan["seed"],
                "artifact_sha256": sha,
                "payload": payload.decode("utf-8"),
                "run_dir": str(artifact_dir),
            }
        )
        (artifact_dir / "packaged.json").write_text(
            json.dumps(packaged, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return {
        "intake": intake,
        "decide": decide,
        "judge": judge,
        "repair": repair,
        "package": package,
    }


def _execute(
    tmp_path: Path,
    scripted_plans: list[dict[str, Any]],
    judge_failures: list[list[str]],
    packaged: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    manifest = _attempt_manifest()
    clients = FakeClient(scripted_plans)
    code, outcome = execute(
        manifest,
        _registry(scripted_plans, judge_failures, packaged),
        source=tmp_path / "input.csv",
        max_attempts=3,
        model_factory=lambda role, config: clients,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    return code, outcome


def _write_source(tmp_path: Path) -> None:
    (tmp_path / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")


def _payload_sha(seed: int) -> str:
    return hashlib.sha256(f"payload-{seed}".encode()).hexdigest()


def test_best_first_packages_first_plan(tmp_path: Path) -> None:
    _write_source(tmp_path)
    packaged: dict[str, Any] = {}
    code, outcome = _execute(
        tmp_path,
        [{"seed": 1, "n_rows": 4}],
        [[]],
        packaged,
    )
    assert code == EXIT_OK
    assert packaged["plan"] == {"seed": 1, "n_rows": 4}
    assert packaged["seed"] == 1
    assert packaged["artifact_sha256"] == _payload_sha(1)
    assert packaged["payload"] == "payload-1"


def test_tied_failures_package_first_plan(tmp_path: Path) -> None:
    _write_source(tmp_path)
    packaged: dict[str, Any] = {}
    code, outcome = _execute(
        tmp_path,
        [
            {"seed": 11, "n_rows": 4},
            {"seed": 22, "n_rows": 8},
            {"seed": 22, "n_rows": 8},
        ],
        [["check_a"], ["check_a"]],
        packaged,
    )
    assert code == EXIT_FAILED
    assert outcome["attempts"] == 2
    assert packaged["plan"] == {"seed": 11, "n_rows": 4}
    assert packaged["seed"] == 11
    assert packaged["artifact_sha256"] == _payload_sha(11)
    assert packaged["payload"] == "payload-11"


def test_best_middle_packages_middle_plan(tmp_path: Path) -> None:
    _write_source(tmp_path)
    packaged: dict[str, Any] = {}
    code, outcome = _execute(
        tmp_path,
        [
            {"seed": 1, "n_rows": 4},
            {"seed": 2, "n_rows": 4},
            {"seed": 2, "n_rows": 4},
            {"seed": 3, "n_rows": 4},
            {"seed": 3, "n_rows": 4},
        ],
        [["check_a", "check_b"], ["check_b"], ["check_a", "check_b"]],
        packaged,
    )
    # The repair queue feeds attempts 2 and 3; failures [2, 1, 2] make
    # the middle attempt the best without improvement on attempt 3.
    assert code == EXIT_FAILED
    assert outcome["attempts"] == 3
    assert packaged["plan"] == {"seed": 2, "n_rows": 4}
    assert packaged["seed"] == 2
    assert packaged["artifact_sha256"] == _payload_sha(2)
    assert packaged["payload"] == "payload-2"


def test_final_success_packages_last_plan(tmp_path: Path) -> None:
    _write_source(tmp_path)
    packaged: dict[str, Any] = {}
    code, outcome = _execute(
        tmp_path,
        [
            {"seed": 1, "n_rows": 4},
            {"seed": 9, "n_rows": 6},
            {"seed": 9, "n_rows": 6},
        ],
        [["check_b"], []],
        packaged,
    )
    assert code == EXIT_OK
    assert outcome["status"] == "released"
    assert packaged["plan"] == {"seed": 9, "n_rows": 6}
    assert packaged["seed"] == 9
    assert packaged["artifact_sha256"] == _payload_sha(9)
    assert packaged["payload"] == "payload-9"


def test_attempt_records_bind_digest_source_seed_tokens(
    tmp_path: Path,
) -> None:
    _write_source(tmp_path)
    packaged: dict[str, Any] = {}
    _execute(
        tmp_path,
        [
            {"seed": 5, "n_rows": 4},
            {"seed": 6, "n_rows": 4},
            {"seed": 6, "n_rows": 4},
            {"seed": 7, "n_rows": 4},
            {"seed": 7, "n_rows": 4},
        ],
        [["check_a", "check_b"], ["check_b"], ["check_a", "check_b"]],
        packaged,
    )
    run_dir = Path(str(packaged["run_dir"]))
    manifest = _attempt_manifest()
    receipts = [
        json.loads(
            (run_dir / "attempts" / f"attempt-{n}" / "receipt.json").read_text(
                encoding="utf-8"
            )
        )
        for n in (1, 2, 3)
    ]
    assert [r["verdict"] for r in receipts] == ["fail", "fail", "fail"]
    assert [r["seed"] for r in receipts] == [5, 6, 7]
    for receipt in receipts:
        assert receipt["manifest_digest"] == manifest.digest
        assert receipt["source_sha256"] == "SRC123"
        assert receipt["tokens"] == 3
        assert len(receipt["artifact_refs"]) == 1
    first_plan = json.loads(
        (run_dir / "attempts" / "attempt-1" / "plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_plan == {"seed": 5, "n_rows": 4}
    second_checks = json.loads(
        (run_dir / "attempts" / "attempt-2" / "checks.json").read_text(
            encoding="utf-8"
        )
    )
    assert {c["check_id"]: c["status"] for c in second_checks} == {
        "check_a": "pass",
        "check_b": "fail",
    }
