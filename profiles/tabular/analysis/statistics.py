"""Summary statistics for plain tabular sources.

A :class:`TabularProfile` holds aggregates ONLY -- row counts, column
names, per-column numeric summaries and text value rates. It never stores
raw cell values. The model-visible serialisation is the *disclosed* view
(:meth:`TabularProfile.to_disclosed_json`), which withholds rare text
literals; :meth:`TabularProfile.to_json` keeps the full aggregate record
for code-side use (checks, logs) and is never handed to a model.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


#: Minimum source-row support for a text literal to appear in the
#: model-visible view. A text value seen only once is indistinguishable
#: from an identifier (a name, contact, or diagnosis code): disclosing it
#: would hand the model a value that singles out one source row. The
#: disclosed view (and the seeded generator, which draws from it) therefore
#: carries only literals shared by at least two source rows, and records
#: how many distinct values were withheld. This is a disclosure floor, not
#: a quality threshold: it never judges a value as good or bad, it only
#: withholds rare ones. Numeric columns need no such rule -- a mean or a
#: range is already an aggregate over the column.
MIN_TEXT_COUNT_FOR_MODEL_VIEW = 2


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


def disclosed_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    """The model-visible projection of tabular statistics.

    Returns a deep copy in which every text column carries only literals
    shared by at least ``MIN_TEXT_COUNT_FOR_MODEL_VIEW`` source rows;
    rarer literals are withheld and counted in ``suppressed_values``. Rates
    are renormalised over the disclosed values. Numeric summaries pass
    through untouched: they are aggregates already. The input is never
    mutated.
    """
    if not isinstance(stats, Mapping):
        raise TypeError(
            f"stats must be a mapping, got {type(stats).__name__}"
        )
    view = copy.deepcopy(dict(stats))
    text = view.get("text")
    if not isinstance(text, dict):
        return view
    for summary in text.values():
        if not isinstance(summary, dict):
            continue
        values = summary.get("values")
        if not isinstance(values, dict):
            continue
        kept = {
            value: count
            for value, count in values.items()
            if isinstance(count, int)
            and not isinstance(count, bool)
            and count >= MIN_TEXT_COUNT_FOR_MODEL_VIEW
        }
        total = sum(kept.values())
        summary["values"] = kept
        summary["rates"] = (
            {value: count / total for value, count in kept.items()}
            if total
            else {}
        )
        summary["suppressed_values"] = len(values) - len(kept)
    return view


@dataclass
class TabularProfile:
    """Aggregate statistics for one tabular source."""

    source_name: str
    source_sha256: str
    stats: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise the full statistics (code-side record, not for models)."""
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

    def disclosed_stats(self) -> dict[str, Any]:
        """The model-visible statistics: rare text literals withheld."""
        return disclosed_stats(self.stats)

    def to_disclosed_json(self) -> str:
        """Serialise the disclosed view (the only model-visible content)."""
        return (
            json.dumps(
                {
                    "source_name": self.source_name,
                    "source_sha256": self.source_sha256,
                    "stats": self.disclosed_stats(),
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
