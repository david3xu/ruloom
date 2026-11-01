"""Tabular source reader: plain CSV rows as string mappings."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence

PathLike = str | Path


def read_tabular_csv(path: PathLike) -> tuple[list[str], list[dict[str, str]]]:
    """Read a plain CSV file into ``(columns, rows)``.

    Every cell is kept as its raw string; typing decisions belong to the
    statistics stage, not the reader. Raises :class:`ValueError` on an
    empty file or a missing header.
    """
    text = Path(path).read_text(encoding="utf-8")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError(f"no header row in {path}")
    columns = list(reader.fieldnames)
    rows = [{col: (row.get(col) or "") for col in columns} for row in reader]
    return columns, rows


def read_tabular_csvs(
    paths: Sequence[PathLike],
) -> dict[str, tuple[list[str], list[dict[str, str]]]]:
    """Read several CSV files keyed by file stem."""
    out: dict[str, tuple[list[str], list[dict[str, str]]]] = {}
    for path in paths:
        out[Path(path).stem] = read_tabular_csv(path)
    return out
