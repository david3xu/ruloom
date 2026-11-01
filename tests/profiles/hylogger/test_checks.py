"""Checks tests: verdict semantics, schema, bands, vocabulary, fidelity."""

import hashlib

from profiles.hylogger.analysis.calibration import Band, Thresholds
from profiles.hylogger.validation.checks import check_artifact_csv, run_checks
from profiles.hylogger.validation.dataset import to_csv_bytes
from profiles.hylogger.intake.reader import Sample


def _thresholds() -> Thresholds:
    return Thresholds(
        bands={
            "spacing_median_m": Band("spacing_median_m", 0.005, 0.01),
            "repeated_depth_fraction": Band("repeated_depth_fraction", 0.0, 0.2),
            "valid_fraction": Band("valid_fraction", 0.5, 1.0),
            "swir_answer_rate": Band("swir_answer_rate", 0.0, 1.0, vacuous=True),
            "tir_answer_rate": Band("tir_answer_rate", 0.9, 1.0),
            "repeated_valid_fraction": Band("repeated_valid_fraction", 0.0, 0.05),
            "backward_steps": Band("backward_steps", 0, 0),
        },
        swir_vocabulary=frozenset({"A", "B"}),
        tir_vocabulary=frozenset({"X", "Y"}),
    )


def _base() -> list[Sample]:
    return [
        Sample(depth_m=10.0 + i * 0.0078, valid=(i != 3),
               swir_group="A" if i % 2 == 0 else "B", tir_group="X")
        for i in range(8)
    ]


def test_clean_artifact_passes() -> None:
    report = run_checks(_base(), _thresholds(), source_samples=_base(),
                        artifact_name="base")
    assert report.verdict == "pass"
    assert report.failed == []
    assert report.skipped == ["swir_answer_rate"]  # vacuous band
    assert len(report.artifact_sha256) == 64


def test_skip_is_not_pass() -> None:
    report = run_checks(_base(), _thresholds(), source_samples=_base())
    skipped = [r for r in report.results if r.name == "swir_answer_rate"]
    assert len(skipped) == 1
    assert skipped[0].status == "skip"
    assert skipped[0].status != "pass"


def test_verdict_fail_on_any_fail() -> None:
    bad = list(_base())
    bad[0] = Sample(depth_m=99.0, valid=True, swir_group="A", tir_group="X")
    report = run_checks(bad, _thresholds(), source_samples=_base())
    assert report.verdict == "fail"
    assert "backward_steps" in report.failed
    assert "depth_non_decreasing" in report.failed


def test_empty_artifact_fails() -> None:
    report = run_checks([], _thresholds())
    assert report.verdict == "fail"
    assert "has_rows" in report.failed


def test_schema_bad_header_fails() -> None:
    report = check_artifact_csv(b"depth,valid\n1.0,true\n", _thresholds())
    assert report.verdict == "fail"
    assert report.results[0].name == "csv_schema"
    assert report.results[0].status == "fail"


def test_schema_bad_cells_fail() -> None:
    good = to_csv_bytes(_base())
    bad_valid = good.replace(b",true,", b",yes,", 1)
    assert check_artifact_csv(bad_valid, _thresholds()).verdict == "fail"
    bad_depth = good.replace(b"10.0,", b"deep,", 1)
    assert check_artifact_csv(bad_depth, _thresholds()).verdict == "fail"


def test_schema_good_csv_passes_schema() -> None:
    report = check_artifact_csv(to_csv_bytes(_base()), _thresholds(),
                                source_samples=_base())
    assert report.results[0].name == "csv_schema"
    assert report.results[0].status == "pass"
    assert report.verdict == "pass"
    assert report.artifact_sha256 == hashlib.sha256(to_csv_bytes(_base())).hexdigest()


def test_wrong_spacing_trips_spacing_median() -> None:
    wide = [
        Sample(depth_m=s.depth_m * 10, valid=s.valid,
               swir_group=s.swir_group, tir_group=s.tir_group)
        for s in _base()
    ]
    report = run_checks(wide, _thresholds(), source_samples=_base())
    assert report.verdict == "fail"
    assert "spacing_median" in report.failed


def test_invented_group_trips_vocabulary() -> None:
    bad = list(_base())
    bad[1] = Sample(depth_m=bad[1].depth_m, valid=True,
                    swir_group="NOT_A_REAL_GROUP", tir_group="X")
    report = run_checks(bad, _thresholds(), source_samples=_base())
    assert report.verdict == "fail"
    assert "swir_vocabulary" in report.failed


def test_repeated_valid_trips_masked_out() -> None:
    bad = [
        Sample(depth_m=10.0, valid=s.valid,
               swir_group=s.swir_group, tir_group=s.tir_group)
        for s in _base()
    ]
    report = run_checks(bad, _thresholds(), source_samples=_base())
    assert report.verdict == "fail"
    assert "repeated_depths_masked_out" in report.failed


def test_fidelity_skip_without_source() -> None:
    report = run_checks(_base(), _thresholds())
    assert {r.name for r in report.results if r.status == "skip"} >= {
        "swir_answer_rate", "swir_fidelity", "tir_fidelity"}
    assert report.verdict == "pass"


def test_fidelity_skip_when_source_cannot_establish() -> None:
    source = [
        Sample(depth_m=10.0 + i * 0.0078, valid=True,
               swir_group=None, tir_group="X")
        for i in range(8)
    ]
    report = run_checks(_base(), _thresholds(), source_samples=source)
    swir_fid = next(r for r in report.results if r.name == "swir_fidelity")
    assert swir_fid.status == "skip"
    assert "split-half" in swir_fid.detail


def test_fidelity_fail_beyond_source_tolerance() -> None:
    source = [
        Sample(depth_m=10.0 + i * 0.0078, valid=True,
               swir_group="A", tir_group="X")
        for i in range(8)
    ]
    artifact = [
        Sample(depth_m=10.0 + i * 0.0078, valid=True,
               swir_group="B", tir_group="X")
        for i in range(8)
    ]
    report = run_checks(artifact, _thresholds(), source_samples=source)
    swir_fid = next(r for r in report.results if r.name == "swir_fidelity")
    assert swir_fid.status == "fail"
    tir_fid = next(r for r in report.results if r.name == "tir_fidelity")
    assert tir_fid.status == "pass"


def _image_reference():
    from profiles.hylogger.imaging.generate import make_fixture_png
    from profiles.hylogger.imaging.statistics import compute_statistics

    payload = make_fixture_png(
        24, 12, [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
    )
    return compute_statistics(payload, "fixture.png")


def test_image_checks_pass_for_fitted_synthetic() -> None:
    """A synthetic fitted to the reference passes all three image checks."""
    from profiles.hylogger.imaging.checks import (
        HISTOGRAM_DISTANCE_MAX,
        run_image_checks,
    )
    from profiles.hylogger.imaging.generate import synthesize

    reference = _image_reference()
    synthetic = synthesize(reference.to_dict(), 7)
    report = run_image_checks(
        reference, synthetic, artifact_name="synthetic.png"
    )
    assert report.verdict == "pass", [
        (r.name, r.status, r.detail) for r in report.results
    ]
    assert [r.name for r in report.results] == [
        "image_histogram_distance",
        "image_dimensions",
        "image_strip_alignment",
    ]
    distance = next(
        r for r in report.results if r.name == "image_histogram_distance"
    )
    assert "literal" in distance.detail
    assert float(distance.detail.split()[0]) <= HISTOGRAM_DISTANCE_MAX


def test_image_checks_fail_for_foreign_synthetic() -> None:
    """A synthetic fitted to other statistics fails the distance check."""
    from profiles.hylogger.imaging.checks import run_image_checks
    from profiles.hylogger.imaging.generate import (
        make_fixture_png,
        synthesize,
    )
    from profiles.hylogger.imaging.statistics import compute_statistics

    reference = _image_reference()
    other = compute_statistics(
        make_fixture_png(
            24, 12, [(5, 5, 5), (250, 5, 5), (5, 250, 5), (5, 5, 250)]
        ),
        "other.png",
    )
    synthetic = synthesize(other.to_dict(), 7)
    report = run_image_checks(
        reference, synthetic, artifact_name="synthetic.png"
    )
    assert report.verdict == "fail"
    assert "image_histogram_distance" in report.failed


def test_image_checks_fail_for_wrong_dimensions() -> None:
    """A synthetic with different dimensions fails the dimension check."""
    from profiles.hylogger.imaging.checks import run_image_checks
    from profiles.hylogger.imaging.generate import (
        make_fixture_png,
        synthesize,
    )
    from profiles.hylogger.imaging.statistics import compute_statistics

    reference = _image_reference()
    small = compute_statistics(
        make_fixture_png(
            12, 6, [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
        ),
        "small.png",
    )
    synthetic = synthesize(small.to_dict(), 7)
    report = run_image_checks(
        reference, synthetic, artifact_name="synthetic.png"
    )
    assert report.verdict == "fail"
    assert "image_dimensions" in report.failed
