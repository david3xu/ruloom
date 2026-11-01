"""Summary statistics for normalised HyLogger samples.

A :class:`Profile` holds aggregate statistics ONLY -- counts, fractions,
quantiles and run shapes. It never stores per-sample depths or labels, so
:func:`Profile.to_json` is the only source-derived content a model may
ever see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..intake.reader import Sample

#: Decimals for rounded depth summaries (start/end/span). Rounding keeps
#: exact source depth values out of the serialised profile.
DEPTH_ND = 3
#: Decimals for spacing quantiles and mean run lengths.
SMALL_ND = 6
#: Decimals for fractions and rates.
FRAC_ND = 6


def _r(value: float, nd: int) -> float:
    return round(float(value), nd)


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile of pre-sorted values (0 <= pct <= 100)."""
    if not sorted_values:
        raise ValueError("percentile of empty data")
    if pct <= 0:
        return float(sorted_values[0])
    if pct >= 100:
        return float(sorted_values[-1])
    rank = (pct / 100) * (len(sorted_values) - 1)
    low = int(rank)
    frac = rank - low
    return float(sorted_values[low] + frac * (sorted_values[low + 1] - sorted_values[low]))


def _run_lengths(flags: Sequence[bool], value: bool) -> list[int]:
    runs: list[int] = []
    current = 0
    for flag in flags:
        if flag is value:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def _describe_runs(runs: Sequence[int]) -> dict[str, Any]:
    if not runs:
        return {"count": 0, "max": 0, "mean": None}
    return {
        "count": len(runs),
        "max": int(max(runs)),
        "mean": _r(sum(runs) / len(runs), SMALL_ND),
    }


def _channel_stats(samples: Sequence[Sample], channel: str) -> dict[str, Any]:
    valid = [s for s in samples if s.valid]
    labels = [getattr(s, channel) for s in valid]
    answered = [label for label in labels if label is not None]

    marginals: dict[str, int] = {}
    for label in answered:
        marginals[label] = marginals.get(label, 0) + 1
    total_answered = len(answered)
    marginal_fractions = (
        {label: _r(count / total_answered, FRAC_ND) for label, count in marginals.items()}
        if total_answered
        else {}
    )

    pairs = [
        (a, b)
        for a, b in zip(labels, labels[1:])
        if a is not None and b is not None
    ]
    transitions: dict[str, dict[str, int]] = {}
    for a, b in pairs:
        transitions.setdefault(a, {}).setdefault(b, 0)
        transitions[a][b] += 1
    n_pairs = len(pairs)
    transition_fractions = (
        {
            a: {b: _r(c / n_pairs, FRAC_ND) for b, c in targets.items()}
            for a, targets in transitions.items()
        }
        if n_pairs
        else {}
    )
    persistence = (
        _r(sum(1 for a, b in pairs if a == b) / n_pairs, FRAC_ND) if n_pairs else None
    )

    return {
        "n_valid": len(valid),
        "n_answered": total_answered,
        "answer_rate": _r(total_answered / len(valid), FRAC_ND) if valid else None,
        "marginals": marginals,
        "marginal_fractions": marginal_fractions,
        "n_pairs": n_pairs,
        "persistence": persistence,
        "transitions": transitions,
        "transition_fractions": transition_fractions,
    }


def build_stats(samples: Sequence[Sample]) -> dict[str, Any]:
    """Build the summary-statistics dict for normalised samples."""
    samples = list(samples)
    n = len(samples)

    if n:
        depths = [s.depth_m for s in samples]
        diffs = [b - a for a, b in zip(depths, depths[1:])]
        forward = sorted(d for d in diffs if d > 0)
        if forward:
            spacing = {
                "count_forward": len(forward),
                "mean": _r(sum(forward) / len(forward), SMALL_ND),
                **{
                    f"p{p}": _r(percentile(forward, p), SMALL_ND)
                    for p in (10, 25, 50, 75, 90)
                },
            }
        else:
            spacing = {
                "count_forward": 0,
                "mean": None,
                **{f"p{p}": None for p in (10, 25, 50, 75, 90)},
            }
        geometry: dict[str, Any] = {
            "depth_start_m": _r(depths[0], DEPTH_ND),
            "depth_end_m": _r(depths[-1], DEPTH_ND),
            "depth_span_m": _r(depths[-1] - depths[0], DEPTH_ND),
            "spacing_m": spacing,
            "n_repeated_steps": sum(1 for d in diffs if d == 0),
            "n_backward_steps": sum(1 for d in diffs if d < 0),
        }
    else:
        geometry = {
            "depth_start_m": None,
            "depth_end_m": None,
            "depth_span_m": None,
            "spacing_m": {
                "count_forward": 0,
                "mean": None,
                **{f"p{p}": None for p in (10, 25, 50, 75, 90)},
            },
            "n_repeated_steps": 0,
            "n_backward_steps": 0,
        }

    flags = [s.valid for s in samples]
    n_valid = sum(flags)
    validity = {
        "n_valid": n_valid,
        "n_invalid": n - n_valid,
        "valid_fraction": _r(n_valid / n, FRAC_ND) if n else None,
        "valid_runs": _describe_runs(_run_lengths(flags, True)),
        "invalid_runs": _describe_runs(_run_lengths(flags, False)),
    }

    swir = _channel_stats(samples, "swir_group")
    tir = _channel_stats(samples, "tir_group")

    both = [
        (s.swir_group, s.tir_group)
        for s in samples
        if s.valid and s.swir_group is not None and s.tir_group is not None
    ]
    joint_counts: dict[str, dict[str, int]] = {}
    for s_label, t_label in both:
        joint_counts.setdefault(s_label, {}).setdefault(t_label, 0)
        joint_counts[s_label][t_label] += 1
    n_both = len(both)
    valid_samples = [s for s in samples if s.valid]
    coverage = {
        "both_answered": n_both,
        "swir_only": sum(
            1 for s in valid_samples if s.swir_group is not None and s.tir_group is None
        ),
        "tir_only": sum(
            1 for s in valid_samples if s.swir_group is None and s.tir_group is not None
        ),
        "neither_answered": sum(
            1 for s in valid_samples if s.swir_group is None and s.tir_group is None
        ),
    }
    joint = {
        "n_both_answered": n_both,
        "coverage_valid": coverage,
        "joint_counts": joint_counts,
        "agreement": (
            _r(sum(1 for a, b in both if a == b) / n_both, FRAC_ND) if n_both else None
        ),
    }

    return {
        "n_samples": n,
        "geometry": geometry,
        "validity": validity,
        "swir": swir,
        "tir": tir,
        "joint": joint,
    }


@dataclass(frozen=True)
class Profile:
    """Aggregate statistics for one hole plus its source identity.

    Only summary statistics are stored -- no per-sample depths or labels.
    """

    source_name: str
    source_sha256: str
    stats: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise the profile; the only source-derived content models see."""
        return json.dumps(
            {
                "source_name": self.source_name,
                "source_sha256": self.source_sha256,
                "stats": self.stats,
            },
            sort_keys=True,
        )

    @classmethod
    def from_samples(
        cls, samples: Sequence[Sample], source_name: str, source_sha256: str
    ) -> "Profile":
        """Build a profile of summary statistics for normalised samples."""
        return cls(
            source_name=source_name,
            source_sha256=source_sha256,
            stats=build_stats(samples),
        )
