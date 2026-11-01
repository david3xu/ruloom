"""Audit counterexample fixture: R07 advisory capability rewrites the verdict.

Every capability receives the same mutable engine state, so an
advisory-only capability with no ``writes_verdict`` permission can
overwrite ``state['verdict']`` and release an artifact whose own
report says ``fail``. The test asserts the corrected behaviour (a
failing deterministic report is never released); it currently fails
because the run is released, recorded as strict xfail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_FAILED, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

needs_fix = pytest.mark.xfail(
    strict=True, reason="audit R07 open: advisory capability rewrites verdict"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")

STRICT_THRESHOLDS = '{"mean_relative_tolerance": 0.0}'


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
                "dataset_name": "r07-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _override(ctx: Any) -> None:
    """Advisory-only capability that rewrites the deterministic verdict."""
    assert ctx.permissions.advisory and not ctx.permissions.writes_verdict
    ctx.state["verdict"] = "pass"
    ctx.state["failed"] = []


@needs_fix
def test_failing_deterministic_report_is_never_released(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    thresholds = tmp_path / "strict.json"
    thresholds.write_text(STRICT_THRESHOLDS, encoding="utf-8")
    out = tmp_path / "out"

    os.environ["RULOOM_THRESHOLDS"] = str(thresholds)
    try:
        registry = dict(CAPABILITIES)
        registry["describe"] = _override
        code, result = execute(
            MANIFEST,
            registry,
            source=source,
            max_attempts=1,
            model_factory=lambda role, config: FakeModel(
                [{"seed": 11, "n_rows": 4}]
            ),
            out=out,
            say=lambda message: None,
            ask=lambda prompt: "accept",
        )
    finally:
        del os.environ["RULOOM_THRESHOLDS"]

    assert code == EXIT_FAILED, result
    assert result["status"] != "released"
    artifact_dir = result.get("artifact_dir")
    assert artifact_dir is None or not (
        Path(str(artifact_dir)) / "release.zip"
    ).exists()


def test_strict_thresholds_fail_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the same strict run fails deterministically when no
    advisory touches the verdict, so the defect above is the override."""
    source = _write_source(tmp_path)
    thresholds = tmp_path / "strict.json"
    thresholds.write_text(STRICT_THRESHOLDS, encoding="utf-8")
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(thresholds))
    code, result = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: FakeModel([{"seed": 11, "n_rows": 4}]),
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    assert result["status"] != "released"
