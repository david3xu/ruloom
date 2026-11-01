"""Tabular validation tests: schema/shape checks plus the fidelity check."""

from __future__ import annotations

import pytest

from profiles.tabular.analysis.calibration import Thresholds
from profiles.tabular.analysis.statistics import TabularProfile
from profiles.tabular.generation.generate import generate as generate_fn
from profiles.tabular.validation import checks as checks_module
from profiles.tabular.validation import dataset as dataset_module

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


def _profile() -> TabularProfile:
    return TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")


def test_checks_pass_on_matching_artifact() -> None:
    profile = _profile()
    payload = generate_fn(profile, {"seed": 7, "n_rows": 20})
    columns, rows = dataset_module.read(payload)
    report = checks_module.run_checks(
        columns,
        rows,
        Thresholds(),
        plan_n_rows=20,
        profile_columns=profile.stats["columns"],
        profile_mean=profile.stats["numeric"]["value"]["mean"],
        mean_column="value",
        artifact_sha256="abc",
    )
    assert report.verdict == "pass", [r.detail for r in report.results]


def test_checks_fail_on_wrong_shape() -> None:
    report = checks_module.run_checks(
        ["id", "value"],
        [{"id": "1", "value": "2"}],
        Thresholds(),
        plan_n_rows=20,
        profile_columns=COLUMNS,
        profile_mean=12.0,
        mean_column="value",
        artifact_sha256="abc",
    )
    assert report.verdict == "fail"
    assert "schema_columns" in report.failed
    assert "row_count" in report.failed


def test_band_position_endpoints_and_midpoint() -> None:
    band = checks_module.band_position
    assert band(0.0, 0.0, 1.0) == 0.0
    assert band(0.5, 0.0, 1.0) == 0.5
    assert band(1.0, 0.0, 1.0) == 1.0
    assert band(5.0, 0.0, 10.0) == 0.5


def test_band_position_outside_is_signed_distance() -> None:
    band = checks_module.band_position
    assert band(-1.0, 0.0, 2.0) == -0.5
    assert band(3.0, 0.0, 2.0) == 1.5
    assert band(0.0, 0.0, 0.0) == 0.0


def test_mean_value_carries_position_and_keeps_verdict() -> None:
    profile = _profile()
    mean = profile.stats["numeric"]["value"]["mean"]
    reference = abs(mean)
    tolerance = Thresholds().mean_relative_tolerance

    def rows_at(drift: float) -> list[dict[str, str]]:
        value = mean + drift * reference
        return [{**row, "value": f"{value}"} for row in ROWS]

    exact = checks_module.run_checks(
        COLUMNS, rows_at(0.0), Thresholds(), plan_n_rows=20,
        profile_columns=COLUMNS, profile_mean=mean, mean_column="value",
    )
    fidelity = next(r for r in exact.results if r.name == "mean_value")
    assert fidelity.status == "pass"
    assert fidelity.band_position == 0.0

    half = checks_module.run_checks(
        COLUMNS, rows_at(tolerance / 2), Thresholds(), plan_n_rows=20,
        profile_columns=COLUMNS, profile_mean=mean, mean_column="value",
    )
    half_fidelity = next(r for r in half.results if r.name == "mean_value")
    assert half_fidelity.status == "pass"
    assert half_fidelity.band_position == pytest.approx(0.5)

    over = checks_module.run_checks(
        COLUMNS, rows_at(tolerance * 1.5), Thresholds(), plan_n_rows=20,
        profile_columns=COLUMNS, profile_mean=mean, mean_column="value",
    )
    over_fidelity = next(r for r in over.results if r.name == "mean_value")
    assert over_fidelity.status == "fail"
    assert over_fidelity.band_position == pytest.approx(1.5)

    skipped = checks_module.run_checks(
        COLUMNS, ROWS, Thresholds(), plan_n_rows=20,
        profile_columns=COLUMNS, profile_mean=None, mean_column="value",
    )
    missing = next(r for r in skipped.results if r.name == "mean_value")
    assert missing.status == "skip"
    assert missing.band_position is None


def test_report_dict_carries_positions_without_renaming() -> None:
    profile = _profile()
    payload = generate_fn(profile, {"seed": 7, "n_rows": 20})
    columns, rows = dataset_module.read(payload)
    report = checks_module.run_checks(
        columns, rows, Thresholds(), plan_n_rows=20,
        profile_columns=profile.stats["columns"],
        profile_mean=profile.stats["numeric"]["value"]["mean"],
        mean_column="value",
    )
    body = report.to_dict()
    assert set(body) == {
        "artifact_name", "artifact_sha256", "verdict", "results",
    }
    for entry in body["results"]:
        assert set(entry) == {"name", "status", "detail", "band_position"}
