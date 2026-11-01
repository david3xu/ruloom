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


def test_disclosed_view_withholds_singleton_literals() -> None:
    from profiles.tabular.analysis.statistics import disclosed_stats

    columns = ["name", "contact"]
    rows = [
        {"name": "Ava Reyes", "contact": "shared@example.com"},
        {"name": "Bo Chen", "contact": "shared@example.com"},
        {"name": "Cy Dada", "contact": "solo@example.com"},
    ]
    profile = TabularProfile.from_rows(columns, rows, "contacts", "abc123")
    view = profile.disclosed_stats()
    assert view["text"]["name"]["values"] == {}
    assert view["text"]["name"]["suppressed_values"] == 3
    assert view["text"]["contact"]["values"] == {"shared@example.com": 2}
    assert view["text"]["contact"]["rates"] == {"shared@example.com": 1.0}
    assert view["text"]["contact"]["suppressed_values"] == 1
    blob = profile.to_disclosed_json()
    assert "Ava Reyes" not in blob
    assert "solo@example.com" not in blob
    assert "shared@example.com" in blob
    # The full code-side record is not mutated by the projection.
    assert profile.stats["text"]["name"]["values"]["Ava Reyes"] == 1
    assert "suppressed_values" not in profile.stats["text"]["name"]
    assert disclosed_stats(profile.stats) == view
