"""Summary statistics for plain tabular sources.

A :class:`TabularProfile` holds aggregates ONLY -- row counts, column
names, per-column numeric summaries and text value rates. It never stores
raw cell values, so :meth:`TabularProfile.to_json` is the only
source-derived content a model may ever see.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


def _is_numeric(values: Sequence[str]) -> bool:
    seen = False
    for value in values:
        if value == "":
            continue
        try:
            float(value)
        except ValueError:
            return False
        seen = True
    return seen


def _numeric_summary(values: Sequence[str]) -> dict[str, float]:
    numbers = [float(v) for v in values if v != ""]
    mean = sum(numbers) / len(numbers)
    variance = sum((v - mean) ** 2 for v in numbers) / len(numbers)
    return {
        "mean": mean,
        "std": math.sqrt(variance),
        "min": min(numbers),
        "max": max(numbers),
    }


def _text_summary(values: Sequence[str]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = len(values)
    return {
        "values": dict(counts),
        "rates": {k: c / total for k, c in counts.items()} if total else {},
    }


def build_stats(
    columns: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """Aggregate-only statistics for one tabular source."""
    rows = list(rows)
    numeric: dict[str, dict[str, float]] = {}
    text: dict[str, dict[str, Any]] = {}
    for column in columns:
        values = [row.get(column, "") for row in rows]
        if _is_numeric(values):
            numeric[column] = _numeric_summary(values)
        else:
            text[column] = _text_summary(values)
    return {
        "n_rows": len(rows),
        "columns": list(columns),
        "numeric": numeric,
        "text": text,
    }


@dataclass
class TabularProfile:
    """Aggregate statistics for one tabular source."""

    source_name: str
    source_sha256: str
    stats: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise the statistics (the only model-visible content)."""
        return (
            json.dumps(
                {
                    "source_name": self.source_name,
                    "source_sha256": self.source_sha256,
                    "stats": self.stats,
                },
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def from_rows(
        cls,
        columns: Sequence[str],
        rows: Sequence[Mapping[str, str]],
        source_name: str,
        source_sha256: str,
    ) -> "TabularProfile":
        """Build a profile from source columns and rows."""
        return cls(
            source_name=source_name,
            source_sha256=source_sha256,
            stats=build_stats(columns, rows),
        )
