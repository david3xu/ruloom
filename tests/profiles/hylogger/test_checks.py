"""Checks tests: verdict semantics, schema, bands, vocabulary, fidelity."""

import hashlib

import pytest

from profiles.hylogger.analysis.calibration import Band, Thresholds
from profiles.hylogger.validation.checks import (
    band_position,
    check_artifact_csv,
    run_checks,
)
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
    report = run_checks(
        _base(), _thresholds(), source_samples=_base(),
        expected_geometry={"n_rows": 8, "start_m": 10.0, "step_m": 0.0078},
        artifact_name="base")
    assert report.verdict == "pass"
    assert report.failed == []
    assert report.skipped == ["swir_answer_rate"]  # vacuous band
    assert len(report.artifact_sha256) == 64
    for name in (
        "depth_finite", "depth_nonnegative", "invalid_label_rate",
        "swir_answer_fidelity", "tir_answer_fidelity",
        "swir_source_vocabulary", "tir_source_vocabulary",
        "swir_label_richness", "tir_label_richness",
        "row_count_match", "depth_start_match",
        "depth_end_within_requested",
    ):
        assert next(
            r for r in report.results if r.name == name).status == "pass"


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


def test_rp05_checks_skip_without_source_or_plan() -> None:
    """Source-relative and plan-binding checks skip (never pass) unevidenced."""
    report = run_checks(_base(), _thresholds())
    skipped = {r.name for r in report.results if r.status == "skip"}
    assert skipped >= {
        "swir_answer_rate",
        "invalid_label_rate",
        "swir_answer_fidelity", "tir_answer_fidelity",
        "swir_source_vocabulary", "tir_source_vocabulary",
        "swir_label_richness", "tir_label_richness",
        "swir_fidelity", "tir_fidelity",
        "row_count_match", "depth_start_match",
        "depth_end_within_requested",
    }
    # Absolute depth checks need no source and still judge.
    assert next(
        r for r in report.results if r.name == "depth_finite").status == "pass"
    assert next(
        r for r in report.results if r.name == "depth_nonnegative").status == "pass"
    assert report.verdict == "pass"


def test_non_finite_and_negative_depths_fail() -> None:
    bad = list(_base())
    bad[0] = Sample(depth_m=float("inf"), valid=True,
                    swir_group="A", tir_group="X")
    bad[1] = Sample(depth_m=-3.25, valid=True,
                    swir_group="B", tir_group="X")
    report = run_checks(bad, _thresholds(), source_samples=_base())
    assert report.verdict == "fail"
    assert "depth_finite" in report.failed
    assert "depth_nonnegative" in report.failed


def test_vacuous_fidelity_tolerance_skips_never_passes() -> None:
    """A split-half tolerance of 1.0 is unsupported, not a fidelity pass."""
    first = [
        Sample(depth_m=1.0 + i * 0.005, valid=True,
               swir_group="A", tir_group="X")
        for i in range(4)
    ]
    second = [
        Sample(depth_m=2.0 + i * 0.005, valid=True,
               swir_group="B", tir_group="X")
        for i in range(4)
    ]
    source = first + second
    report = run_checks(source, _thresholds(), source_samples=source)
    swir_fid = next(r for r in report.results if r.name == "swir_fidelity")
    assert swir_fid.status == "skip"
    assert "vacuous" in swir_fid.detail


def test_source_reference_path_matches_samples_path() -> None:
    """The granted statistics view judges identically to inline samples."""
    from profiles.hylogger.validation.checks import source_fidelity_reference

    reference = source_fidelity_reference(_base())
    direct = run_checks(_base(), _thresholds(), source_samples=_base())
    via_ref = run_checks(_base(), _thresholds(), source_reference=reference)
    assert via_ref.verdict == direct.verdict == "pass"
    assert [(r.name, r.status) for r in via_ref.results] == [
        (r.name, r.status) for r in direct.results]


def test_invalid_label_rate_is_source_relative() -> None:
    """Invalid labelling fails only above the source hole's own rate."""
    source = [
        Sample(depth_m=10.0 + i * 0.0078, valid=(i != 3),
               swir_group=None, tir_group=None)
        for i in range(8)
    ]
    assert "invalid_label_rate" not in [
        r.name for r in run_checks(
            source, _thresholds(), source_samples=source).results
        if r.status == "fail"
    ]
    bad = list(source)
    bad[3] = Sample(depth_m=bad[3].depth_m, valid=False,
                    swir_group="A", tir_group="X")
    report = run_checks(bad, _thresholds(), source_samples=source)
    assert report.verdict == "fail"
    assert "invalid_label_rate" in report.failed


def test_foreign_label_and_collapse_fail() -> None:
    bad = list(_base())
    bad[1] = Sample(depth_m=bad[1].depth_m, valid=True,
                    swir_group="ZZZ", tir_group="X")
    report = run_checks(bad, _thresholds(), source_samples=_base())
    assert "swir_source_vocabulary" in report.failed
    collapsed = [
        Sample(depth_m=s.depth_m, valid=s.valid,
               swir_group=("A" if s.swir_group else None),
               tir_group=s.tir_group)
        for s in _base()
    ]
    report = run_checks(collapsed, _thresholds(), source_samples=_base())
    assert report.verdict == "fail"
    assert "swir_label_richness" in report.failed


def test_geometry_binding_variants() -> None:
    expected = {"n_rows": 8, "start_m": 10.0, "step_m": 0.0078}
    report = run_checks(_base(), _thresholds(), source_samples=_base(),
                        expected_geometry=expected)
    assert report.verdict == "pass"
    short = run_checks(_base()[:4], _thresholds(), source_samples=_base(),
                       expected_geometry=expected)
    assert "row_count_match" in short.failed
    moved = [
        Sample(depth_m=s.depth_m + 1.0, valid=s.valid,
               swir_group=s.swir_group, tir_group=s.tir_group)
        for s in _base()
    ]
    report = run_checks(moved, _thresholds(), source_samples=_base(),
                        expected_geometry=expected)
    assert "depth_start_match" in report.failed
    # Malformed plan context degrades to skip, never to a pass-or-fail claim.
    report = run_checks(_base(), _thresholds(), source_samples=_base(),
                        expected_geometry={"n_rows": "eight"})
    assert {r.name for r in report.results if r.status == "skip"} >= {
        "row_count_match", "depth_start_match",
        "depth_end_within_requested"}


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


def test_band_position_endpoints_and_midpoint() -> None:
    assert band_position(0.0, 0.0, 1.0) == 0.0
    assert band_position(0.5, 0.0, 1.0) == 0.5
    assert band_position(1.0, 0.0, 1.0) == 1.0
    assert band_position(0.5, 0.0, 2.0) == 0.25
    assert band_position(0.003472, 0.003472, 0.008898) == 0.0
    assert band_position(0.008898, 0.003472, 0.008898) == 1.0


def test_band_position_outside_is_signed_distance() -> None:
    assert band_position(-0.1, 0.0, 0.2) == pytest.approx(-0.5)
    assert band_position(0.3, 0.0, 0.2) == pytest.approx(1.5)
    assert band_position(0.0, 0.0, 0.0) == 0.0


def test_band_checks_carry_positions_and_keep_verdict() -> None:
    report = run_checks(_base(), _thresholds(), source_samples=_base(),
                        artifact_name="base")
    assert report.verdict == "pass"
    spacing = next(r for r in report.results if r.name == "spacing_median")
    assert spacing.status == "pass"
    assert spacing.band_position is not None
    assert 0.0 <= spacing.band_position <= 1.0
    skipped = next(r for r in report.results if r.name == "swir_answer_rate")
    assert skipped.status == "skip"
    assert skipped.band_position is None
    for result in report.results:
        if result.status == "fail":
            assert result.band_position is not None
            assert (
                result.band_position < 0.0 or result.band_position > 1.0
            )


def test_failing_band_reports_out_of_band_distance() -> None:
    wide = [
        Sample(depth_m=s.depth_m * 10, valid=s.valid,
               swir_group=s.swir_group, tir_group=s.tir_group)
        for s in _base()
    ]
    report = run_checks(wide, _thresholds(), source_samples=_base())
    assert report.verdict == "fail"
    spacing = next(r for r in report.results if r.name == "spacing_median")
    assert spacing.status == "fail"
    assert spacing.band_position is not None
    assert spacing.band_position > 1.0


def test_fidelity_checks_carry_tvd_positions() -> None:
    report = run_checks(_base(), _thresholds(), source_samples=_base())
    swir_fid = next(r for r in report.results if r.name == "swir_fidelity")
    assert swir_fid.status == "pass"
    assert swir_fid.band_position == 0.0
    tir_fid = next(r for r in report.results if r.name == "tir_fidelity")
    assert tir_fid.status == "pass"
    assert tir_fid.band_position == 0.0


def test_report_dict_carries_positions_without_renaming() -> None:
    report = check_artifact_csv(to_csv_bytes(_base()), _thresholds(),
                                source_samples=_base())
    body = report.to_dict()
    assert set(body) == {
        "artifact_name", "artifact_sha256", "verdict", "results",
    }
    for entry in body["results"]:
        assert set(entry) == {"name", "status", "detail", "band_position"}
    spacing = next(e for e in body["results"] if e["name"] == "spacing_median")
    assert 0.0 <= spacing["band_position"] <= 1.0


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
