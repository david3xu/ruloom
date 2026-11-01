"""Evaluation proof: all 43 real holes pass; controls trip named checks.

Loads the committed ``profiles/hylogger/thresholds.json`` (derived from
the 38 calibration holes) and runs checks over every real hole in
``data/sources`` (5) and ``data/calibration`` (38) with each hole as its
own fidelity source. Four negative controls on HD012 prove the ruler
fails bad artifacts at the responsible check.
"""

import glob
from dataclasses import replace
from pathlib import Path

import pytest

from profiles.hylogger.analysis.calibration import load_thresholds
from profiles.hylogger.validation.checks import run_checks
from profiles.hylogger.intake.reader import Sample, read_scalar_csv

REPO_ROOT = Path(__file__).resolve().parents[3]
THRESHOLDS = REPO_ROOT / "profiles" / "hylogger" / "thresholds.json"
HD012 = REPO_ROOT / "data" / "sources" / "HD012-scalars.csv"

pytestmark = pytest.mark.skipif(
    not HD012.is_file(),
    reason="requires git-ignored NVCL source data under data/ (absent in clean checkout)",
)


def _all_hole_paths() -> list[str]:
    return sorted(
        glob.glob(str(REPO_ROOT / "data" / "calibration" / "*-scalars.csv"))
        + glob.glob(str(REPO_ROOT / "data" / "sources" / "*-scalars.csv"))
    )


def test_all_real_holes_pass() -> None:
    thresholds = load_thresholds(THRESHOLDS)
    assert len(thresholds.provenance["holes"]) == 38
    verdicts = {}
    for path in _all_hole_paths():
        samples = read_scalar_csv(path)
        report = run_checks(samples, thresholds, source_samples=samples,
                            artifact_name=Path(path).stem)
        verdicts[Path(path).stem] = report.verdict
        assert report.verdict == "pass", (path, report.failed, report.skipped)
    assert len(verdicts) == 43
    assert set(verdicts.values()) == {"pass"}


def _hd012() -> list[Sample]:
    return read_scalar_csv(HD012)


def test_control_base_passes() -> None:
    thresholds = load_thresholds(THRESHOLDS)
    base = _hd012()
    assert run_checks(base, thresholds, source_samples=base).verdict == "pass"


def test_control_backward_step() -> None:
    thresholds = load_thresholds(THRESHOLDS)
    base = _hd012()
    i = next(
        k for k in range(1, len(base) - 2)
        if base[k - 1].depth_m < base[k].depth_m < base[k + 1].depth_m
    )
    bad = list(base)
    bad[i] = replace(bad[i], depth_m=base[i + 1].depth_m)
    bad[i + 1] = replace(bad[i + 1], depth_m=base[i].depth_m)
    report = run_checks(bad, thresholds, source_samples=base)
    assert report.verdict == "fail"
    assert "backward_steps" in report.failed
    assert "depth_non_decreasing" in report.failed


def test_control_invented_group() -> None:
    thresholds = load_thresholds(THRESHOLDS)
    base = _hd012()
    bad = list(base)
    changed = 0
    for k, s in enumerate(base):
        if changed >= 50:
            break
        if s.valid and s.swir_group is not None:
            bad[k] = replace(s, swir_group="NOT_A_REAL_GROUP")
            changed += 1
    assert changed == 50
    report = run_checks(bad, thresholds, source_samples=base)
    assert report.verdict == "fail"
    assert "swir_vocabulary" in report.failed


def test_control_wrong_spacing() -> None:
    thresholds = load_thresholds(THRESHOLDS)
    base = _hd012()
    bad = [replace(s, depth_m=s.depth_m * 10) for s in base]
    report = run_checks(bad, thresholds, source_samples=base)
    assert report.verdict == "fail"
    assert "spacing_median" in report.failed


def test_control_repeated_depth_placement() -> None:
    thresholds = load_thresholds(THRESHOLDS)
    base = _hd012()
    start, width = 1000, 600
    anchor = base[start].depth_m
    bad = list(base)
    for k in range(start, start + width):
        bad[k] = replace(bad[k], depth_m=anchor)
    report = run_checks(bad, thresholds, source_samples=base)
    assert report.verdict == "fail"
    assert "repeated_depths_masked_out" in report.failed
