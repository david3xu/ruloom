"""Durable terminal evidence: every run leaves its record behind (R11).

The run record is created before intake runs and every transition and
error is persisted as it occurs, so a model failure after intake leaves
a durable failed run record -- not just an in-memory run id. Bounds
from the manifest are validated against the resolved plan before
generation or checks run, and a required critic's budget failure blocks
the run while an optional critic's failure is recorded visibly without
affecting the verdict.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from engine.audit.runlog import RunLog
from engine.core.manifest import ENGINE_API, load_manifest, validate_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute
from models.base import Completion, ModelError, Usage
from profiles.tabular.capabilities import CAPABILITIES as TABULAR_CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
TABULAR_MANIFEST = load_manifest(
    REPO_ROOT / "profiles" / "tabular" / "profile.json"
)


def _run_dirs(out: Path) -> list[Path]:
    return sorted(
        c for c in (out / "runs").iterdir() if (c / "run.json").is_file()
    )


def _run_record(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def _write_tabular_source(path: Path) -> Path:
    csv_path = path / "sample-tabular.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (path / "sample-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "durability-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Durability fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


class ExplodingPlanner:
    """A planner double that fails after intake has already succeeded."""

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        raise ModelError("planner exploded after intake")


def test_model_failure_after_intake_leaves_durable_failed_record(
    tmp_path: Path,
) -> None:
    source = _write_tabular_source(tmp_path)
    out = tmp_path / "out"
    code, outcome = execute(
        TABULAR_MANIFEST,
        TABULAR_CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: ExplodingPlanner(),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    assert outcome["status"] == "error"
    assert "planner exploded" in str(outcome.get("error", ""))
    run_id = outcome["run_id"]
    assert run_id is not None

    # The failed run's history is durable, not just an in-memory id.
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert run_dir.name == run_id
    record = _run_record(run_dir)
    assert record["status"] == "error"
    assert "planner exploded" in str(record["error"])
    assert record["attempts"] == 0
    assert not (run_dir / "release.zip").exists()
    log = RunLog.read_jsonl(run_dir / "events.jsonl")
    assert log.verify()
    stages = [event["stage"] for event in log.events]
    assert "run_start" in stages
    assert "plan" in stages
    plan_errors = [
        event for event in log.events
        if event["stage"] == "plan" and event["status"] == "error"
    ]
    assert len(plan_errors) == 1


def test_run_record_exists_before_intake_runs(tmp_path: Path) -> None:
    """A failure in the first capability still leaves a run directory."""
    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    manifest = validate_manifest(
        {
            "id": "fake-early-failure",
            "version": 1,
            "engine_api": ENGINE_API,
            "source": {"reader": "fake.reader"},
            "capabilities": {"intake": {"impl": "fake.intake"}},
            "graph": [{"step": "intake"}, {"gate": "review"}],
            "checks": [
                {
                    "id": "shape_ok",
                    "kind": "constraint",
                    "threshold": {"source": "literal"},
                }
            ],
            "models": {"roles": {}},
            "limits": {},
        }
    )

    def intake(ctx: Any) -> None:
        raise RuntimeError("intake exploded immediately")

    out = tmp_path / "out"
    code, outcome = execute(
        manifest,
        {"intake": intake},
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: None,
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = _run_record(run_dirs[0])
    assert record["status"] == "error"
    assert "intake exploded" in str(record["error"])


def test_oversize_plan_refused_before_generation(tmp_path: Path) -> None:
    """A plan over the manifest limit never reaches the generator."""
    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    manifest = validate_manifest(
        {
            "id": "fake-bounds",
            "version": 1,
            "engine_api": ENGINE_API,
            "source": {"reader": "fake.reader"},
            "capabilities": {
                "intake": {"impl": "fake.intake"},
                "plan": {"impl": "fake.plan"},
                "generate": {"impl": "fake.generate"},
                "judge": {"impl": "fake.judge", "writes_verdict": True},
            },
            "graph": [
                {"step": "intake"},
                {"step": "plan"},
                {
                    "loop": ["generate", "judge"],
                    "on_fail": "plan",
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
            "models": {"roles": {}},
            "limits": {"max_rows": 5},
        }
    )
    generated = {"calls": 0}

    def intake(ctx: Any) -> None:
        ctx.set_source_meta(
            name="bounds-source",
            sha256="BOUNDSSRC",
            licence=None,
            attribution=None,
        )

    def plan(ctx: Any) -> None:
        ctx.set_plan({"seed": 1, "n_rows": 6})

    def generate(ctx: Any) -> None:
        generated["calls"] += 1
        ctx.record_artifact("out.bin", "text/csv", b"payload")

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
                    "detail": "ok",
                }
            ]
        )

    out = tmp_path / "out"
    code, outcome = execute(
        manifest,
        {
            "intake": intake,
            "plan": plan,
            "generate": generate,
            "judge": judge,
        },
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: None,
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    assert generated["calls"] == 0
    assert "exceeds manifest limit max_rows" in str(outcome.get("error", ""))
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = _run_record(run_dirs[0])
    assert record["status"] == "error"
    assert "max_rows" in str(record["error"])
    assert not (run_dirs[0] / "release.zip").exists()


def _critic_manifest(policy: str) -> Any:
    return validate_manifest(
        {
            "id": f"fake-critic-{policy}",
            "version": 1,
            "engine_api": ENGINE_API,
            "source": {"reader": "fake.reader"},
            "capabilities": {
                "intake": {"impl": "fake.intake"},
                "plan": {"impl": "fake.plan", "calls_model": "planner"},
                "generate": {"impl": "fake.generate"},
                "judge": {"impl": "fake.judge", "writes_verdict": True},
                "critic": {
                    "impl": "fake.critic",
                    "calls_model": "critic",
                    "advisory": True,
                    "critic_policy": policy,
                },
                "package": {"impl": "fake.package"},
                "release": {"impl": "fake.release"},
            },
            "graph": [
                {"step": "intake"},
                {"step": "plan"},
                {
                    "loop": ["generate", "judge"],
                    "on_fail": "plan",
                    "max_attempts": "max_attempts",
                    "stage": "work",
                },
                {"step": "critic"},
                {"step": "package"},
                {"gate": "review"},
                {"step": "release"},
            ],
            "checks": [
                {
                    "id": "shape_ok",
                    "kind": "constraint",
                    "threshold": {"source": "literal"},
                    "required": True,
                }
            ],
            "models": {
                "roles": {
                    "planner": {"model": "fake"},
                    "critic": {"model": "fake"},
                },
                "budget": {"max_tokens_per_run": 10},
            },
            "limits": {},
        }
    )


class BudgetSpendingPlanner:
    """A planner double spending the whole run budget in one call."""

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data={"seed": 1, "n_rows": 1},
            usage=Usage(
                model="fake", input_tokens=10, output_tokens=0, calls=1
            ),
            meta={"model": "fake"},
        )


def _critic_registry(spent: dict[str, int]) -> dict[str, Any]:
    def intake(ctx: Any) -> None:
        ctx.set_source_meta(
            name="critic-source",
            sha256="CRITICSRC",
            licence=None,
            attribution=None,
        )

    def plan(ctx: Any) -> None:
        completion = ctx.model("planner").complete("prompt", {})
        ctx.set_plan(dict(completion.data))

    def generate(ctx: Any) -> None:
        ctx.record_artifact("out.bin", "text/csv", b"payload")

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
                    "detail": "ok",
                }
            ]
        )

    def critic(ctx: Any) -> None:
        spent["calls"] += 1
        client = ctx.model("critic")
        note = client.complete("critic?", {})
        ctx.record_advisory("note", dict(note.data))

    def package(ctx: Any) -> None:
        sha = ctx.selected_artifact_sha()
        payload = ctx.artifact_payload(sha)
        plan = ctx.selected_plan()
        artifact_dir = ctx.prepare_artifact_dir()
        (artifact_dir / "data.csv").write_bytes(payload)
        (artifact_dir / "plan.json").write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # A realistic bound report: the release consumer requires the
        # report to carry the artifact hash it attests, as every real
        # profile report does.
        (artifact_dir / "validation-report.json").write_text(
            json.dumps(
                {
                    "artifact_name": "durability:attempt-1",
                    "artifact_sha256": hashlib.sha256(payload).hexdigest(),
                    "verdict": "pass",
                    "results": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (artifact_dir / "seed.txt").write_text(
            f"{plan['seed']}\n", encoding="utf-8"
        )
        (artifact_dir / "provenance.md").write_text(
            ctx.provenance_text(), encoding="utf-8"
        )
        ctx.flush_log(artifact_dir / "run.jsonl")

    def release(ctx: Any) -> None:
        artifact_dir = Path(ctx.artifact_dir())
        sha = ctx.selected_artifact_sha()
        zip_path, _ = ctx.pack_release()
        ctx.event(
            "release",
            "ok",
            {"zip": str(zip_path)},
            artifact_sha256=sha,
        )
        ctx.flush_log(artifact_dir / "run.jsonl")

    return {
        "intake": intake,
        "plan": plan,
        "generate": generate,
        "judge": judge,
        "critic": critic,
        "package": package,
        "release": release,
    }


def _write_critic_source(tmp_path: Path) -> Path:
    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    return source


def test_required_critic_budget_failure_blocks_run(tmp_path: Path) -> None:
    source = _write_critic_source(tmp_path)
    spent = {"calls": 0}
    out = tmp_path / "out"
    code, outcome = execute(
        _critic_manifest("required"),
        _critic_registry(spent),
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: BudgetSpendingPlanner(),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    # The critic call is refused before it is made: it never runs.
    assert spent["calls"] == 1
    assert "budget" in str(outcome.get("error", "")).lower()
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = _run_record(run_dirs[0])
    assert record["status"] == "error"
    assert "budget" in str(record["error"]).lower()
    assert not (run_dirs[0] / "release.zip").exists()
    assert outcome["advisories"] == []


def test_optional_critic_budget_failure_is_visible_and_non_fatal(
    tmp_path: Path,
) -> None:
    source = _write_critic_source(tmp_path)
    spent = {"calls": 0}
    out = tmp_path / "out"
    code, outcome = execute(
        _critic_manifest("optional"),
        _critic_registry(spent),
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: BudgetSpendingPlanner(),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    assert outcome["status"] == "released"
    assert outcome["verdict"] == "pass"
    # The failure is recorded visibly and cannot affect the verdict.
    failures = [
        entry
        for entry in outcome["advisories"]
        if entry["name"] == "advisory-failed"
    ]
    assert len(failures) == 1
    assert failures[0]["payload"]["budget_exhausted"] is True
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "release.zip").is_file()
    log = RunLog.read_jsonl(run_dirs[0] / "events.jsonl")
    assert log.verify()
    advisory_failures = [
        event
        for event in log.events
        if event["status"] == "advisory-failed"
    ]
    assert len(advisory_failures) == 1


def test_event_exports_match(tmp_path: Path) -> None:
    """events.jsonl and the legacy run.jsonl export carry the same log."""
    source = _write_critic_source(tmp_path)
    out = tmp_path / "out"
    code, _ = execute(
        _critic_manifest("optional"),
        _critic_registry({"calls": 0}),
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: BudgetSpendingPlanner(),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    (run_dir,) = _run_dirs(out)
    assert (run_dir / "events.jsonl").read_text(
        encoding="utf-8"
    ) == (run_dir / "run.jsonl").read_text(encoding="utf-8")
