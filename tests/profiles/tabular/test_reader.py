"""Tabular intake tests: reading a source CSV into columns and rows."""

from __future__ import annotations

from pathlib import Path

from profiles.tabular.intake.reader import read_tabular_csv

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


def _write_source(tmp_path: Path) -> Path:
    csv_path = tmp_path / "orders-tabular.csv"
    lines = [",".join(COLUMNS)]
    lines += [",".join(row[col] for col in COLUMNS) for row in ROWS]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path


def test_reader_returns_columns_and_rows(tmp_path: Path) -> None:
    columns, rows = read_tabular_csv(_write_source(tmp_path))
    assert columns == COLUMNS
    assert rows == ROWS
