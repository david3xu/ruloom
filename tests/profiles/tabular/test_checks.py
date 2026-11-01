"""Tabular validation tests: schema/shape checks plus the fidelity check."""

from __future__ import annotations

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
