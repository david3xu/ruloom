"""Seeded tabular generator: one trivial structure from profile aggregates.

Generation uses a single ``random.Random`` seeded by the plan. Numeric
columns draw from a gaussian fitted to the profile mean/std and clamp to
the profile range; text columns draw from the profile value rates. No
model output ever becomes a data value.
"""

from __future__ import annotations

import random
from typing import Any, Mapping

from .dataset import to_csv_bytes
from .statistics import TabularProfile


def _plan_seed_n_rows(plan: Mapping[str, Any]) -> tuple[int, int]:
    seed = plan["seed"]
    n_rows = plan["n_rows"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"plan seed must be an integer, got {seed!r}")
    if isinstance(n_rows, bool) or not isinstance(n_rows, int) or n_rows < 1:
        raise ValueError(f"plan n_rows must be a positive integer, got {n_rows!r}")
    return seed, n_rows


def _weighted_choice(
    rng: random.Random, values: list[str], weights: list[float]
) -> str:
    total = sum(weights)
    pick = rng.random() * total
    running = 0.0
    for value, weight in zip(values, weights):
        running += weight
        if pick < running:
            return value
    return values[-1]


def generate_rows(
    profile: TabularProfile, seed: int, n_rows: int
) -> list[dict[str, str]]:
    """Generate ``n_rows`` synthetic rows from profile aggregates."""
    rng = random.Random(seed)
    stats = profile.stats
    columns: list[str] = list(stats.get("columns", []))
    numeric: dict[str, dict[str, float]] = stats.get("numeric", {})
    text: dict[str, dict[str, Any]] = stats.get("text", {})
    rows: list[dict[str, str]] = []
    for _ in range(n_rows):
        row: dict[str, str] = {}
        for column in columns:
            if column in numeric:
                summary = numeric[column]
                std = summary["std"] or 1.0
                value = rng.gauss(summary["mean"], std)
                value = min(max(value, summary["min"]), summary["max"])
                row[column] = repr(round(value, 6))
            elif column in text:
                summary = text[column]
                values = list(summary["values"])
                if not values:
                    row[column] = ""
                    continue
                total = sum(summary["values"].values())
                weights = [summary["values"][v] / total for v in values]
                row[column] = _weighted_choice(rng, values, weights)
            else:
                row[column] = ""
        rows.append(row)
    return rows


def generate(profile: TabularProfile, plan: Mapping[str, Any]) -> bytes:
    """Generate the synthetic CSV artifact for ``plan``."""
    seed, n_rows = _plan_seed_n_rows(plan)
    if not isinstance(profile, TabularProfile):
        raise TypeError(
            f"profile must be a TabularProfile, got {type(profile).__name__}"
        )
    return to_csv_bytes(
        generate_rows(profile, seed, n_rows),
        list(profile.stats.get("columns", [])),
    )
