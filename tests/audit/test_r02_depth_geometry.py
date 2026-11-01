"""Audit counterexample fixtures: R02 impossible depths pass checks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from profiles.hylogger.analysis.calibration import load_thresholds
from profiles.hylogger.intake.reader import Sample
from profiles.hylogger.validation.checks import run_checks

needs_fix = pytest.mark.xfail(
    strict=True, reason="audit R02 open: impossible depths pass checks"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = load_thresholds(REPO_ROOT / "profiles" / "hylogger" / "thresholds.json")


def _base(n: int = 1400) -> list[Sample]:
    tick = 0
    samples = []
    for i in range(n):
        valid = i % 7 != 0
        label = None
        if valid:
            label = ("CHLORITE", "WHITE-MICA")[tick % 2]
            tick += 1
        samples.append(
            Sample(
                depth_m=i * 0.005,
                valid=valid,
                swir_group=label,
                tir_group=("SILICA" if valid else None),
            )
        )
    return samples


def test_base_fixture_passes() -> None:
    """Control: the uncorrupted fixture passes, so failures below are real."""
    base = _base()
    report = run_checks(base, THRESHOLDS, source_samples=base)
    assert report.verdict == "pass"


@needs_fix
def test_negative_depth_shift_must_not_pass() -> None:
    """Shifting every depth by -1000000 m must not pass."""
    base = _base()
    shifted = [replace(s, depth_m=s.depth_m - 1000000.0) for s in base]
    assert shifted[0].depth_m < 0
    report = run_checks(shifted, THRESHOLDS, source_samples=base)
    assert report.verdict == "fail"


@needs_fix
def test_infinite_final_depth_must_not_pass() -> None:
    """Assigning +infinity to the final depth must not pass."""
    base = _base()
    broken = base[:-1] + [replace(base[-1], depth_m=float("inf"))]
    report = run_checks(broken, THRESHOLDS, source_samples=base)
    assert report.verdict == "fail"
