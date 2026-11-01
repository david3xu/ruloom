"""Seeded synthetic HyLogger generator.

Builds a synthetic hole from a statistics :class:`Profile` only -- no raw
source rows are read and no model is called. A single explicit
``random.Random`` PRNG drives every stochastic choice, so the same
``(profile, seed, n_rows)`` triple always yields identical CSV bytes.

Method, all fitted from Profile aggregates:

- depths start at ``geometry.depth_start_m`` and step by the source median
  spacing; a step repeats the previous depth only when the previous row is
  invalid (and the current row is invalid too, so repeats stay masked out);
- valid flags come from a two-state Markov chain whose stay probabilities
  are fitted from the source valid/invalid mean run lengths;
- invalid rows carry no groups; each valid row draws a coverage pattern
  (both/swir-only/tir-only/neither answered) at source rates, then draws
  answered labels from the source marginals, conditioned on the previous
  answered label through the source transition counts where the Profile
  supports it, and on the co-answered label through the source joint
  counts for both-answered rows.
"""

from __future__ import annotations

import random
from typing import Any, Mapping

from .dataset import to_csv_bytes
from .reader import Sample
from .statistics import Profile


def _plan_seed_n_rows(plan: Mapping[str, Any]) -> tuple[int, int]:
    """Validate the plan dict and return ``(seed, n_rows)``."""
    if not isinstance(plan, Mapping):
        raise TypeError(f"plan must be a mapping, got {type(plan).__name__}")
    if "seed" not in plan or "n_rows" not in plan:
        raise ValueError("plan must contain 'seed' and 'n_rows'")
    seed, n_rows = plan["seed"], plan["n_rows"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError(f"plan seed must be an integer, got {seed!r}")
    if isinstance(n_rows, bool) or not isinstance(n_rows, int):
        raise TypeError(f"plan n_rows must be an integer, got {n_rows!r}")
    if n_rows <= 0:
        raise ValueError(f"plan n_rows must be positive, got {n_rows!r}")
    return seed, n_rows


def _weighted_choice(rng: random.Random, items: list[str], weights: list[float]) -> str:
    """Draw one item proportional to weights using only ``rng``."""
    total = sum(weights)
    pick = rng.random() * total
    upto = 0.0
    for item, weight in zip(items, weights):
        upto += weight
        if pick < upto:
            return item
    return items[-1]


def _marginal_table(counts: Mapping[str, int]) -> tuple[list[str], list[float]]:
    """Sorted labels and counts for deterministic weighted sampling."""
    labels = sorted(counts)
    if not labels:
        raise ValueError("no answered labels in profile marginals")
    return labels, [float(counts[label]) for label in labels]


def generate_samples(profile: Profile, seed: int, n_rows: int) -> list[Sample]:
    """Generate ``n_rows`` synthetic samples from Profile aggregates only."""
    stats = profile.stats
    try:
        geometry = stats["geometry"]
        validity = stats["validity"]
        spacing = geometry["spacing_m"]
    except KeyError as exc:
        raise ValueError(f"profile stats missing section: {exc}") from exc

    start = geometry.get("depth_start_m")
    step = spacing.get("p50") or spacing.get("mean")
    if start is None or step is None:
        raise ValueError("profile geometry has no usable depth start/spacing")
    start, step = float(start), float(step)

    valid_fraction = validity.get("valid_fraction")
    if valid_fraction is None:
        raise ValueError("profile validity has no valid_fraction")
    mean_valid = (validity.get("valid_runs") or {}).get("mean")
    mean_invalid = (validity.get("invalid_runs") or {}).get("mean")

    rng = random.Random(seed)

    if mean_valid and mean_invalid:
        p_leave_valid = 1.0 / mean_valid
        p_enter_valid = 1.0 / mean_invalid
        flags: list[bool] = []
        current = rng.random() < valid_fraction
        for _ in range(n_rows):
            flags.append(current)
            if current:
                current = rng.random() >= p_leave_valid
            else:
                current = rng.random() < p_enter_valid
    elif mean_valid:
        flags = [True] * n_rows
    elif mean_invalid:
        flags = [False] * n_rows
    else:
        flags = [rng.random() < valid_fraction for _ in range(n_rows)]

    depths = [start]
    for i in range(1, n_rows):
        if not flags[i - 1] and not flags[i]:
            depths.append(depths[-1])
        else:
            depths.append(depths[-1] + step)

    coverage = stats["joint"]["coverage_valid"]
    patterns = ("both", "swir_only", "tir_only", "neither")
    pattern_weights = [
        float(coverage["both_answered"]),
        float(coverage["swir_only"]),
        float(coverage["tir_only"]),
        float(coverage["neither_answered"]),
    ]

    swir_labels, swir_weights = _marginal_table(stats["swir"]["marginals"])
    tir_labels, tir_weights = _marginal_table(stats["tir"]["marginals"])
    swir_transitions = stats["swir"]["transitions"]
    tir_transitions = stats["tir"]["transitions"]
    joint_counts = stats["joint"]["joint_counts"]

    def pick_channel(
        labels: list[str],
        weights: list[float],
        transitions: Mapping[str, Mapping[str, int]],
        last: str | None,
    ) -> str:
        targets = transitions.get(last, {}) if last is not None else {}
        if targets:
            names = sorted(targets)
            return _weighted_choice(rng, names, [float(targets[n]) for n in names])
        return _weighted_choice(rng, labels, weights)

    samples: list[Sample] = []
    last_swir: str | None = None
    last_tir: str | None = None
    for i in range(n_rows):
        if not flags[i]:
            samples.append(Sample(depth_m=depths[i], valid=False,
                                  swir_group=None, tir_group=None))
            continue
        pattern = _weighted_choice(rng, list(patterns), pattern_weights)
        swir_group: str | None = None
        tir_group: str | None = None
        if pattern in ("both", "swir_only"):
            swir_group = pick_channel(swir_labels, swir_weights,
                                      swir_transitions, last_swir)
            last_swir = swir_group
        if pattern in ("both", "tir_only"):
            partners = joint_counts.get(swir_group, {}) if pattern == "both" else {}
            if partners:
                names = sorted(partners)
                tir_group = _weighted_choice(
                    rng, names, [float(partners[n]) for n in names])
            else:
                tir_group = pick_channel(tir_labels, tir_weights,
                                         tir_transitions, last_tir)
            last_tir = tir_group
        samples.append(Sample(depth_m=depths[i], valid=True,
                              swir_group=swir_group, tir_group=tir_group))
    return samples


def generate(profile: Profile, plan: Mapping[str, Any]) -> bytes:
    """Generate a synthetic hole CSV from a Profile and a plan dict.

    The plan carries an integer ``seed`` and a positive integer ``n_rows``;
    the returned bytes are the synthetic-dataset CSV (via
    :func:`profiles.hylogger.dataset.to_csv_bytes`).
    """
    seed, n_rows = _plan_seed_n_rows(plan)
    if not isinstance(profile, Profile):
        raise TypeError(f"profile must be a Profile, got {type(profile).__name__}")
    return to_csv_bytes(generate_samples(profile, seed, n_rows))
