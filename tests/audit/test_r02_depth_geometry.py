"""Audit fixtures: R02 impossible depths are rejected (RP-05 enforces Q-DATA-R02).

Each test asserts the corrected behaviour (corrupted input must not
pass). A uniform -1000000 m shift trips ``depth_nonnegative``
(StartDepth metres are never negative); a +infinity final depth trips
``depth_finite``; and the plan-binding checks (``row_count_match``,
``depth_start_match``, ``depth_end_within_requested``) refuse an
artifact whose row count, start depth, or end extent does not match
the requested geometry. No source data files are read; fixtures are
built in code against the committed thresholds.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from profiles.hylogger.analysis.calibration import load_thresholds
from profiles.hylogger.intake.reader import Sample
from profiles.hylogger.validation.checks import run_checks

REPO_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = load_thresholds(REPO_ROOT / "profiles" / "hylogger" / "thresholds.json")

STEP_M = 0.005


def _expected(n: int = 1400) -> dict[str, float]:
    """The requested geometry the base fixture was built from."""
    return {"n_rows": n, "start_m": 0.0, "step_m": STEP_M}


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
    report = run_checks(
        base, THRESHOLDS, source_samples=base,
        expected_geometry=_expected(),
    )
    assert report.verdict == "pass"
    assert report.failed == []


def test_negative_depth_shift_must_not_pass() -> None:
    """Shifting every depth by -1000000 m must not pass."""
    base = _base()
    shifted = [replace(s, depth_m=s.depth_m - 1000000.0) for s in base]
    assert shifted[0].depth_m < 0
    report = run_checks(shifted, THRESHOLDS, source_samples=base)
    assert report.verdict == "fail"
    assert "depth_nonnegative" in report.failed


def test_infinite_final_depth_must_not_pass() -> None:
    """Assigning +infinity to the final depth must not pass."""
    base = _base()
    broken = base[:-1] + [replace(base[-1], depth_m=float("inf"))]
    report = run_checks(broken, THRESHOLDS, source_samples=base)
    assert report.verdict == "fail"
    assert "depth_finite" in report.failed


def test_truncated_row_count_must_not_pass() -> None:
    """Fewer rows than the requested plan count must not pass."""
    base = _base()
    report = run_checks(
        base[:100], THRESHOLDS, source_samples=base,
        expected_geometry=_expected(),
    )
    assert report.verdict == "fail"
    assert "row_count_match" in report.failed


def test_shifted_start_must_not_pass() -> None:
    """A start depth off the requested start must not pass."""
    base = _base()
    moved = [replace(s, depth_m=s.depth_m + 5.0) for s in base]
    report = run_checks(
        moved, THRESHOLDS, source_samples=base,
        expected_geometry=_expected(),
    )
    assert report.verdict == "fail"
    assert "depth_start_match" in report.failed


def test_stretched_extent_must_not_pass() -> None:
    """An end depth beyond the requested extent must not pass."""
    base = _base()
    stretched = [
        replace(s, depth_m=s.depth_m * 2.0) for s in base
    ]
    report = run_checks(
        stretched, THRESHOLDS, source_samples=base,
        expected_geometry=_expected(),
    )
    assert report.verdict == "fail"
    assert "depth_end_within_requested" in report.failed
