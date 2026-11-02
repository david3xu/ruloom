"""Summary statistics for normalised HyLogger samples.

A :class:`Profile` holds aggregate statistics ONLY -- counts, fractions,
quantiles and run shapes. It never stores per-sample depths or labels.
The model-visible serialisation is the *disclosed* view
(:meth:`Profile.to_disclosed_json`), which withholds rare group labels;
:meth:`Profile.to_json` keeps the full aggregate record for code-side use
(checks, generation, logs) and is never handed to a model.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..intake.reader import Sample

#: Minimum source-row support for a group label to appear in the
#: model-visible view. A label seen only once is indistinguishable from an
#: identifier for that row: disclosing it would hand the model a value
#: that singles out one source sample. The disclosed view therefore
#: carries only labels shared by at least two source rows, and counts how
#: many distinct labels were withheld per channel. This is a disclosure
#: floor, not a quality threshold: it never judges a label as good or bad,
#: it only withholds rare ones. Counts and fractions over the disclosed
#: labels pass through; the code-side record (checks, seeded generation)
#: keeps working on the full aggregates.
MIN_LABEL_COUNT_FOR_MODEL_VIEW = 2

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


def label_persistence(samples: Sequence[Sample], channel: str) -> float | None:
    """Neighbouring persistence of answered labels for one channel.

    Fraction of consecutive valid-row answered pairs carrying the same
    label (``None`` when fewer than one answered pair exists). Single
    source of truth shared by :func:`build_stats` and the calibration
    per-hole scalars, so checks and profiles can never disagree.
    """
    valid = [s for s in samples if s.valid]
    labels = [getattr(s, channel) for s in valid]
    pairs = [
        (a, b)
        for a, b in zip(labels, labels[1:])
        if a is not None and b is not None
    ]
    if not pairs:
        return None
    return _r(sum(1 for a, b in pairs if a == b) / len(pairs), FRAC_ND)


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
    persistence = label_persistence(samples, channel)

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


def _disclosed_channel(section: Any) -> tuple[dict[str, Any], set[str]]:
    """Prune a channel section to labels with enough source support.

    Returns the pruned section plus the kept label set. Rare labels leave
    the marginals together with every transition touching them; fractions,
    pair counts, and persistence are recomputed over the disclosed pairs,
    and the number of withheld labels is recorded as an integer (never the
    labels themselves).
    """
    if not isinstance(section, dict):
        return {}, set()
    section = copy.deepcopy(section)
    marginals = section.get("marginals")
    if not isinstance(marginals, dict):
        marginals = {}
    kept = {
        label
        for label, count in marginals.items()
        if isinstance(count, int)
        and not isinstance(count, bool)
        and count >= MIN_LABEL_COUNT_FOR_MODEL_VIEW
    }
    section["marginals"] = {
        label: marginals[label] for label in marginals if label in kept
    }
    total = sum(section["marginals"].values())
    section["marginal_fractions"] = (
        {
            label: _r(count / total, FRAC_ND)
            for label, count in section["marginals"].items()
        }
        if total
        else {}
    )
    transitions = section.get("transitions")
    if not isinstance(transitions, dict):
        transitions = {}
    pruned: dict[str, dict[str, int]] = {}
    for source, targets in transitions.items():
        if source not in kept or not isinstance(targets, dict):
            continue
        kept_targets = {
            target: count for target, count in targets.items() if target in kept
        }
        if kept_targets:
            pruned[source] = kept_targets
    section["transitions"] = pruned
    n_pairs = sum(sum(targets.values()) for targets in pruned.values())
    section["n_pairs"] = n_pairs
    section["transition_fractions"] = (
        {
            source: {
                target: _r(count / n_pairs, FRAC_ND)
                for target, count in targets.items()
            }
            for source, targets in pruned.items()
        }
        if n_pairs
        else {}
    )
    same = sum(targets.get(label, 0) for label, targets in pruned.items())
    section["persistence"] = _r(same / n_pairs, FRAC_ND) if n_pairs else None
    section["suppressed_label_count"] = len(marginals) - len(kept)
    return section, kept


def disclosed_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    """The model-visible projection of HyLogger statistics.

    Returns a deep copy in which the ``swir``/``tir`` channels and the
    ``joint`` counts carry only labels shared by at least
    ``MIN_LABEL_COUNT_FOR_MODEL_VIEW`` source rows. Aggregate counts
    (``n_valid``, ``n_answered``, coverage) pass through untouched: they
    name no literal. The input is never mutated.
    """
    if not isinstance(stats, Mapping):
        raise TypeError(f"stats must be a mapping, got {type(stats).__name__}")
    view = copy.deepcopy(dict(stats))
    swir, swir_kept = _disclosed_channel(view.get("swir"))
    tir, tir_kept = _disclosed_channel(view.get("tir"))
    if isinstance(view.get("swir"), dict):
        view["swir"] = swir
    if isinstance(view.get("tir"), dict):
        view["tir"] = tir
    joint = view.get("joint")
    if isinstance(joint, dict):
        joint_counts = joint.get("joint_counts")
        if not isinstance(joint_counts, dict):
            joint_counts = {}
        pruned: dict[str, dict[str, int]] = {}
        for swir_label, targets in joint_counts.items():
            if swir_label not in swir_kept or not isinstance(targets, dict):
                continue
            kept_targets = {
                tir_label: count
                for tir_label, count in targets.items()
                if tir_label in tir_kept
            }
            if kept_targets:
                pruned[swir_label] = kept_targets
        joint["joint_counts"] = pruned
        total = sum(sum(targets.values()) for targets in pruned.values())
        same = sum(
            targets.get(label, 0)
            for label, targets in pruned.items()
            if label in tir_kept
        )
        joint["agreement"] = _r(same / total, FRAC_ND) if total else None
    return view


@dataclass(frozen=True)
class Profile:
    """Aggregate statistics for one hole plus its source identity.

    Only summary statistics are stored -- no per-sample depths or labels.
    """

    source_name: str
    source_sha256: str
    stats: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise the full profile (code-side record, not for models)."""
        return json.dumps(
            {
                "source_name": self.source_name,
                "source_sha256": self.source_sha256,
                "stats": self.stats,
            },
            sort_keys=True,
        )

    def disclosed_stats(self) -> dict[str, Any]:
        """The model-visible statistics: rare group labels withheld."""
        return disclosed_stats(self.stats)

    def to_disclosed_json(self) -> str:
        """Serialise the disclosed view (the only model-visible content)."""
        return json.dumps(
            {
                "source_name": self.source_name,
                "source_sha256": self.source_sha256,
                "stats": self.disclosed_stats(),
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
