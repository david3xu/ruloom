"""Tabular analysis tests: aggregate-only statistics."""

from __future__ import annotations

import pytest

from profiles.tabular.analysis.statistics import TabularProfile

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


def test_statistics_are_aggregates_only() -> None:
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    blob = profile.to_json()
    # No raw cell value may leak into the model-visible statistics.
    assert "orders-tabular" in blob
    assert profile.stats["n_rows"] == 20
    assert profile.stats["columns"] == COLUMNS
    assert profile.stats["numeric"]["value"]["mean"] == pytest.approx(12.0)
    assert set(profile.stats["text"]["label"]["values"]) == {"a", "b"}
