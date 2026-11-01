"""Tabular validation tests: artifact CSV serialisation round-trips."""

from __future__ import annotations

from profiles.tabular.validation.dataset import read_csv_bytes, to_csv_bytes

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


def test_dataset_round_trip() -> None:
    payload = to_csv_bytes(ROWS, COLUMNS)
    columns, rows = read_csv_bytes(payload)
    assert columns == COLUMNS
    assert rows == ROWS
