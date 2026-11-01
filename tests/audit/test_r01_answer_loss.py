"""Audit fixtures: R01 data loss is rejected (RP-05 enforces Q-DATA-R01).

Each test asserts the corrected behaviour (corrupted input must not
pass). Severe SWIR answer removal trips the source-relative
``swir_answer_fidelity`` check (the calibrated band is vacuous, so the
rate is judged against the source hole's own answer rate); collapsing
every label to KAOLIN trips ``swir_source_vocabulary`` (foreign to the
source hole) and ``swir_label_richness``; labelling every invalid row
trips ``invalid_label_rate``. No source data files are read; fixtures
are built in code against the committed thresholds.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

from profiles.hylogger.analysis.calibration import load_thresholds
from profiles.hylogger.analysis.statistics import build_stats
from profiles.hylogger.intake.reader import Sample
from profiles.hylogger.validation.checks import run_checks

REPO_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = load_thresholds(REPO_ROOT / "profiles" / "hylogger" / "thresholds.json")

STEP_M = 0.005
SWIR_LABELS = ("CHLORITE", "WHITE-MICA")


def _uniform(n: int = 1400) -> list[Sample]:
    """Balanced valid fixture: halves identical, geometry inside every band."""
    samples = [
        Sample(
            depth_m=i * STEP_M,
            valid=(i % 7 != 0),
            swir_group=None,
            tir_group=("SILICA" if i % 7 != 0 else None),
        )
        for i in range(n)
    ]
    tick = 0
    for sample in samples:
        if sample.valid:
            object.__setattr__(sample, "swir_group", SWIR_LABELS[tick % 2])
            tick += 1
    return samples


def test_base_fixture_passes() -> None:
    """Control: the uncorrupted fixture passes, so failures below are real."""
    base = _uniform()
    report = run_checks(base, THRESHOLDS, source_samples=base)
    assert report.verdict == "pass"


def test_99_percent_swir_answer_removal_must_not_pass() -> None:
    """Deleting 99% of SWIR answers within each label must not pass."""
    base = _uniform()
    counts: Counter[str] = Counter()
    sparse = []
    for sample in base:
        if sample.valid and sample.swir_group:
            counts[sample.swir_group] += 1
            if counts[sample.swir_group] % 100:
                sample = replace(sample, swir_group=None)
        sparse.append(sample)
    assert build_stats(sparse)["swir"]["answer_rate"] < 0.02
    report = run_checks(sparse, THRESHOLDS, source_samples=base)
    assert report.verdict == "fail"
    assert "swir_answer_fidelity" in report.failed


def _split_halves(n: int = 1400) -> list[Sample]:
    """First-half/second-half label split: wide split-half tolerance."""
    half = n // 2
    return [
        Sample(
            depth_m=i * STEP_M,
            valid=(i % 7 != 0),
            swir_group=(
                ("CHLORITE" if i < half else "WHITE-MICA") if i % 7 != 0 else None
            ),
            tir_group=("SILICA" if i % 7 != 0 else None),
        )
        for i in range(n)
    ]


def test_all_kaolin_collapse_must_not_pass() -> None:
    """Collapsing every answered SWIR group to KAOLIN must not pass."""
    split = _split_halves()
    report = run_checks(split, THRESHOLDS, source_samples=split)
    assert report.verdict == "pass"
    collapsed = [
        replace(s, swir_group="KAOLIN") if s.valid and s.swir_group else s
        for s in split
    ]
    report = run_checks(collapsed, THRESHOLDS, source_samples=split)
    assert report.verdict == "fail"
    assert "swir_source_vocabulary" in report.failed
    assert "swir_label_richness" in report.failed


def test_labels_on_invalid_rows_must_not_pass() -> None:
    """Known group labels assigned to every invalid row must not pass."""
    base = _uniform()
    labelled = [
        replace(sample, swir_group="CHLORITE", tir_group="SILICA")
        if not sample.valid
        else sample
        for sample in base
    ]
    assert any(not s.valid and s.swir_group for s in labelled)
    report = run_checks(labelled, THRESHOLDS, source_samples=base)
    assert report.verdict == "fail"
    assert "invalid_label_rate" in report.failed
