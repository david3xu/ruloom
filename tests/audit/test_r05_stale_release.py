"""Audit counterexample fixture: R05 rejected rerun leaves a stale release.

Repeating identical data used to overwrite the run evidence under a
single ``runs/<hash>`` directory, so rejecting the second run still
left the first run's ``release.zip`` behind. Every execution now gets
a unique run id and its own durable history under ``runs/<run-id>/``;
identical output bytes only reuse the content-addressed payload cache
(``runs/<payload-sha>/data.csv``) and never overwrite another run's
plan, log, decision, or release reference.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.audit.runlog import RunLog
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_DECLINED, EXIT_OK, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")


class FakeModel:
    """A planner double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=1, output_tokens=1, calls=1),
            meta={"model": "fake"},
        )


def _write_source(path: Path) -> Path:
    csv_path = path / "sample-tabular.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (path / "sample-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "r05-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _run(
    source: Path, out: Path, plans: Sequence[Mapping[str, Any]], answers: Sequence[str]
) -> tuple[int, dict[str, Any]]:
    queue = list(answers)
    client = FakeModel(plans)
    return execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=len(plans),
        model_factory=lambda role, config: client,
        out=out,
        say=lambda message: None,
        ask=lambda prompt: queue.pop(0),
    )


def _run_record(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def test_accept_then_reject_leaves_no_release(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    out = tmp_path / "out"

    code, first = _run(
        source, out, [{"seed": 1, "n_rows": 100}], ["accept", "first accepted"]
    )
    assert code == EXIT_OK
    first_dir = Path(str(first["artifact_dir"]))
    assert (first_dir / "release.zip").is_file()
    first_release = (first_dir / "release.zip").read_bytes()

    code, second = _run(
        source, out, [{"seed": 1, "n_rows": 100}], ["reject", "second rejected"]
    )
    assert code == EXIT_DECLINED
    assert second["status"] == "declined"
    second_dir = Path(str(second["artifact_dir"]))

    # Unique persistent runs: the rerun owns a different directory.
    assert second["run_id"] != first["run_id"]
    assert second_dir != first_dir

    # The rejected run has NO release reference of its own.
    assert not (second_dir / "release.zip").exists()
    assert not (second_dir / "checksums.sha256").exists()
    second_record = _run_record(second_dir)
    assert second_record["status"] == "declined"
    assert second_record["decision"]["verdict"] == "reject"
    assert second_record["decision"]["note"] == "second rejected"
    rejected_log = RunLog.read_jsonl(second_dir / "run.jsonl")
    assert rejected_log.verify()

    # The earlier accepted release remains attached to its own run.
    assert (first_dir / "release.zip").read_bytes() == first_release
    first_record = _run_record(first_dir)
    assert first_record["status"] == "released"
    assert first_record["decision"]["verdict"] == "accept"
    assert first_record["decision"]["note"] == "first accepted"
    accepted_log = RunLog.read_jsonl(first_dir / "run.jsonl")
    assert accepted_log.verify()

    # Identical output bytes reuse the content-addressed payload cache,
    # which still resolves the shared payload by hash.
    payload_sha = first["artifact_sha256"]
    assert payload_sha == second["artifact_sha256"]
    cached = out / "runs" / str(payload_sha) / "data.csv"
    assert cached.is_file()
    assert cached.read_bytes() == (first_dir / "data.csv").read_bytes()
