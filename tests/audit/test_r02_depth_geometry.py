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


def test_run_fixture_passes() -> None:
    """Control: the block-structured run fixture passes uncorrupted."""
    base = _run_fixture()
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


def test_uniform_depth_contraction_must_not_pass() -> None:
    """Scaling every depth by 0.7 must fail required geometry checks.

    The 2026-11-02 re-audit (A04) counterexample: start stays 0.0, the
    end contracts to 4.8965 m (inside the old one-sided extent bound)
    and the median spacing 0.0035 m sits inside the calibration band,
    so the corrupted artifact passed every check. Two-sided plan
    fidelity (requested step and span) must refuse it.
    """
    base = _base()
    contracted = [replace(s, depth_m=s.depth_m * 0.7) for s in base]
    assert contracted[0].depth_m == 0.0
    report = run_checks(
        contracted, THRESHOLDS, source_samples=base,
        expected_geometry=_expected(),
    )
    assert report.verdict == "fail"
    assert "spacing_step_match" in report.failed
    assert "depth_span_match" in report.failed


def _scattered(order: int) -> list[int]:
    """A deterministic scatter permutation of indices.

    Golden-ratio stride: consecutive output positions draw from input
    positions ~0.618*order apart, so block-structured runs are
    thoroughly mixed while the label multiset is preserved exactly.
    """
    import math

    step = max(2, int(order * 0.6180339887))
    while math.gcd(step, order) != 1:
        step -= 1
    index = list(range(order))
    index.sort(key=lambda i: (i * step) % order)
    return index


def _run_fixture(n: int = 1400) -> list[Sample]:
    """Block-structured TIR runs over the standard depth lattice.

    Valid rows answer TIR in blocks of 100 (SILICA, OLIVINE, CHLORITE
    rotating), so neighbouring persistence is near 1.0 while three
    labels keep richness/vocabulary honest.
    """
    samples = []
    for i in range(n):
        valid = i % 7 != 0
        tir = None
        if valid:
            tir = ("SILICA", "OLIVINE", "CHLORITE")[(i // 100) % 3]
        samples.append(
            Sample(
                depth_m=i * 0.005,
                valid=valid,
                swir_group=("CHLORITE" if valid else None),
                tir_group=tir,
            )
        )
    return samples


def test_tir_run_derangement_must_not_pass() -> None:
    """Scrambling TIR run order while keeping every marginal must fail.

    A golden-ratio stride permutes answered TIR labels among the valid
    rows: the label multiset (hence every marginal TVD), answer rates,
    depths and row counts are unchanged, but neighbouring persistence
    collapses. Only the TIR persistence-fidelity check may trip, proving
    its specificity for local-sequence structure (A04).
    """
    base = _run_fixture()
    answered = [i for i, s in enumerate(base) if s.valid and s.tir_group is not None]
    labels = [base[i].tir_group for i in answered]
    order = _scattered(len(labels))
    assert order != sorted(order)
    shuffled = [labels[k] for k in order]
    assert sorted(shuffled) == sorted(labels)
    assert shuffled != labels
    mapping = dict(zip(answered, shuffled))
    broken = [
        replace(s, tir_group=mapping.get(i, s.tir_group))
        for i, s in enumerate(base)
    ]
    report = run_checks(
        broken, THRESHOLDS, source_samples=base,
        expected_geometry=_expected(),
    )
    assert report.verdict == "fail"
    assert report.failed == ["tir_persistence_fidelity"]
