"""Tabular generation tests: seeded, deterministic synthesis."""

from __future__ import annotations

from profiles.tabular.analysis.statistics import TabularProfile
from profiles.tabular.generation.generate import generate as generate_fn

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


def test_generate_is_deterministic() -> None:
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    first = generate_fn(profile, {"seed": 7, "n_rows": 20})
    second = generate_fn(profile, {"seed": 7, "n_rows": 20})
    assert first == second
    third = generate_fn(profile, {"seed": 8, "n_rows": 20})
    assert third != first
