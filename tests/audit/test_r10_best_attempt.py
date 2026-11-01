"""Audit counterexample fixture: R10 best-attempt evidence names another plan.

Restoring the best attempt restores its artifact and verdict but not
its plan, so the packager writes the last attempted plan instead. The
test asserts the corrected behaviour (packaged plan and seed describe
the packaged artifact); it currently fails because the evidence pairs
seed-11 data with a seed-22 plan, recorded as strict xfail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_FAILED, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

needs_fix = pytest.mark.xfail(
    strict=True, reason="audit R10 open: best-attempt plan/seed mismatch"
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
                "dataset_name": "r10-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


@needs_fix
def test_best_attempt_packages_its_own_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path)
    thresholds = tmp_path / "strict.json"
    thresholds.write_text('{"mean_relative_tolerance": 0.0}', encoding="utf-8")
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(thresholds))

    code, result = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=2,
        model_factory=lambda role, config: FakeModel(
            [{"seed": 11, "n_rows": 4}, {"seed": 22, "n_rows": 8}]
        ),
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    assert result["attempts"] == 2
    artifact = Path(str(result["artifact_dir"]))
    packaged = json.loads((artifact / "plan.json").read_text(encoding="utf-8"))
    data_rows = len((artifact / "data.csv").read_text(encoding="utf-8").splitlines()) - 1
    assert packaged["n_rows"] == data_rows
    assert packaged["seed"] == int(
        (artifact / "seed.txt").read_text(encoding="utf-8").strip()
    )
