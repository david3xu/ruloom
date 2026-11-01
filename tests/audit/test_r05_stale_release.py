"""Audit counterexample fixture: R05 rejected rerun leaves a stale release.

Repeating identical data overwrites the run evidence under the same
``runs/<hash>`` directory, so rejecting the second run still leaves the
first run's ``release.zip`` behind. The test asserts the corrected
behaviour (no release reference after a reject); it currently fails
because the stale ZIP survives, recorded as strict xfail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_DECLINED, EXIT_OK, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

needs_fix = pytest.mark.xfail(
    strict=True, reason="audit R05 open: rejected rerun leaves stale release.zip"
)

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


@needs_fix
def test_accept_then_reject_leaves_no_release(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    out = tmp_path / "out"

    code, first = _run(
        source, out, [{"seed": 1, "n_rows": 100}], ["accept", "first accepted"]
    )
    assert code == EXIT_OK
    artifact = Path(str(first["artifact_dir"]))
    assert (artifact / "release.zip").is_file()

    code, second = _run(
        source, out, [{"seed": 1, "n_rows": 100}], ["reject", "second rejected"]
    )
    assert code == EXIT_DECLINED
    assert second["status"] == "declined"
    assert not (artifact / "release.zip").exists()
