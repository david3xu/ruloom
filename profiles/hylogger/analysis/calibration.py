"""Calibration: derive invariant bands from calibration holes.

For each hole under ``data/calibration`` this module computes per-hole
scalar statistics (from normalised :class:`Sample` records, not from
:class:`Profile`), the within-hole split-half variation of each scalar,
and the within-hole split-half marginal TVD per channel. Invariant bands
are the observed range across calibration holes widened by the
95th-percentile within-hole split-half delta. Fraction bands are clipped
to ``[0, 1]`` and flagged vacuous when their span is ``>= 0.95``. The
``backward_steps`` band is exactly ``0``. The within-hole TVD spread is
recorded as evidence that composition is not universally checkable, so
per-run fidelity is judged against each source hole's own tolerance.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..intake.reader import Sample, read_scalar_csv
from .statistics import FRAC_ND, SMALL_ND, label_persistence, percentile

#: Licence of the source data the bands are derived from.
LICENCE = "CC-BY-4.0"
#: Verbatim attribution required by the source manifest.
ATTRIBUTION = (
    "Geological Survey of Western Australia, National Virtual Core Library "
    "(NVCL) WA node, via AuScope. Licensed CC BY 4.0."
)
#: Span at or above which a fraction band carries no information.
VACUOUS_SPAN = 0.95

#: Scalar metrics that get widened observed-range bands.
BAND_METRICS = (
    "spacing_median_m",
    "repeated_depth_fraction",
    "valid_fraction",
    "swir_answer_rate",
    "tir_answer_rate",
    "repeated_valid_fraction",
)
#: Band metrics that are fractions (clipped to [0, 1], vacuous when wide).
FRACTION_METRICS = frozenset(BAND_METRICS) - {"spacing_median_m"}


def _r(value: float) -> float:
    return round(float(value), FRAC_ND)


def scalar_summary(samples: Sequence[Sample]) -> dict[str, float | int | None]:
    """Per-hole scalar statistics for normalised samples."""
    samples = list(samples)
    n = len(samples)
    diffs = [b.depth_m - a.depth_m for a, b in zip(samples, samples[1:])]
    forward = sorted(d for d in diffs if d > 0)
    n_steps = len(diffs)
    repeated_idx: set[int] = set()
    for i, d in enumerate(diffs):
        if d == 0:
            repeated_idx.update((i, i + 1))
    n_valid = sum(1 for s in samples if s.valid)

    def answer_rate(channel: str) -> float | None:
        if not n_valid:
            return None
        n_answered = sum(
            1 for s in samples if s.valid and getattr(s, channel) is not None
        )
        return _r(n_answered / n_valid)

    repeated_valid: float | None = None
    if repeated_idx:
        repeated_valid = _r(
            sum(1 for i in repeated_idx if samples[i].valid) / len(repeated_idx)
        )
    return {
        "n": n,
        "tir_persistence": label_persistence(samples, "tir_group"),
        "spacing_median_m": (
            round(statistics.median(forward), SMALL_ND) if forward else None
        ),
        "repeated_depth_fraction": (
            _r(sum(1 for d in diffs if d == 0) / n_steps) if n_steps else None
        ),
        "backward_steps": sum(1 for d in diffs if d < 0),
        "valid_fraction": _r(n_valid / n) if n else None,
        "swir_answer_rate": answer_rate("swir_group"),
        "tir_answer_rate": answer_rate("tir_group"),
        "repeated_valid_fraction": repeated_valid,
    }


def channel_marginals(
    samples: Sequence[Sample], channel: str
) -> dict[str, float] | None:
    """Marginal fractions over valid answered samples, or ``None`` if empty."""
    counts: dict[str, int] = {}
    total = 0
    for s in samples:
        if not s.valid:
            continue
        label = getattr(s, channel)
        if label is None:
            continue
        counts[label] = counts.get(label, 0) + 1
        total += 1
    if not total:
        return None
    return {label: count / total for label, count in counts.items()}


def marginal_tvd(
    first: dict[str, float] | None, second: dict[str, float] | None
) -> float | None:
    """Total variation distance between two marginals (``None`` if either)."""
    if first is None or second is None:
        return None
    return 0.5 * sum(
        abs(first.get(label, 0.0) - second.get(label, 0.0))
        for label in set(first) | set(second)
    )


def split_halves(samples: Sequence[Sample]) -> tuple[list[Sample], list[Sample]]:
    """Split samples into first/second halves by depth order."""
    samples = list(samples)
    cut = len(samples) // 2
    return samples[:cut], samples[cut:]


@dataclass(frozen=True)
class HoleScalars:
    """Per-hole scalars plus within-hole split-half variation."""

    source_name: str
    summary: dict[str, float | int | None] = field(default_factory=dict)
    split_half_delta: dict[str, float | int | None] = field(default_factory=dict)
    split_half_tvd: dict[str, float | None] = field(default_factory=dict)


def hole_scalars(samples: Sequence[Sample], source_name: str = "") -> HoleScalars:
    """Summarise one hole and its internal split-half variation."""
    samples = list(samples)
    first, second = split_halves(samples)
    sum_all = scalar_summary(samples)
    sum_a, sum_b = scalar_summary(first), scalar_summary(second)
    deltas: dict[str, float | int | None] = {}
    for metric in BAND_METRICS + ("backward_steps",):
        va, vb = sum_a[metric], sum_b[metric]
        if va is None or vb is None:
            deltas[metric] = None
        elif metric == "backward_steps":
            deltas[metric] = abs(int(va) - int(vb))
        else:
            deltas[metric] = abs(float(va) - float(vb))
    tvd = {
        channel: marginal_tvd(
            channel_marginals(first, f"{channel}_group"),
            channel_marginals(second, f"{channel}_group"),
        )
        for channel in ("swir", "tir")
    }
    return HoleScalars(
        source_name=source_name,
        summary=sum_all,
        split_half_delta=deltas,
        split_half_tvd=tvd,
    )


@dataclass(frozen=True)
class Band:
    """Inclusive invariant band for one scalar metric."""

    name: str
    lo: float
    hi: float
    vacuous: bool = False


@dataclass(frozen=True)
class Thresholds:
    """Derived invariants plus the provenance of the calibration holes."""

    bands: dict[str, Band] = field(default_factory=dict)
    swir_vocabulary: frozenset = frozenset()
    tir_vocabulary: frozenset = frozenset()
    composition: dict = field(default_factory=dict)
    split_half_p95: dict[str, float] = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "provenance": self.provenance,
            "bands": {
                name: {
                    "lo": band.lo,
                    "hi": band.hi,
                    "vacuous": band.vacuous,
                }
                for name, band in self.bands.items()
            },
            "vocabulary": {
                "swir": sorted(self.swir_vocabulary),
                "tir": sorted(self.tir_vocabulary),
            },
            "composition": self.composition,
            "split_half_p95": self.split_half_p95,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Thresholds:
        return cls(
            bands={
                name: Band(
                    name=name,
                    lo=float(spec["lo"]),
                    hi=float(spec["hi"]),
                    vacuous=bool(spec.get("vacuous", False)),
                )
                for name, spec in payload.get("bands", {}).items()
            },
            swir_vocabulary=frozenset(payload.get("vocabulary", {}).get("swir", [])),
            tir_vocabulary=frozenset(payload.get("vocabulary", {}).get("tir", [])),
            composition=payload.get("composition", {}),
            split_half_p95={
                k: float(v) for k, v in payload.get("split_half_p95", {}).items()
            },
            provenance=payload.get("provenance", {}),
        )


def _p95(values: list[float]) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return float(vals[0])
    return float(percentile(vals, 95))


def derive_thresholds(calibration_dir: str | Path) -> Thresholds:
    """Derive bands, vocabularies and composition evidence from CSVs."""
    paths = sorted(Path(calibration_dir).glob("*-scalars.csv"))
    if not paths:
        raise ValueError(f"no calibration CSVs in {calibration_dir}")
    holes: list[HoleScalars] = []
    hole_provenance = []
    swir_vocab: set[str] = set()
    tir_vocab: set[str] = set()
    for path in paths:
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        samples = read_scalar_csv(path)
        hole = hole_scalars(samples, source_name=path.stem)
        holes.append(hole)
        hole_provenance.append(
            {
                "name": path.stem,
                "path": path.name,
                "sha256": sha256,
                "n": hole.summary["n"],
            }
        )
        for s in samples:
            if not s.valid:
                continue
            if s.swir_group is not None:
                swir_vocab.add(s.swir_group)
            if s.tir_group is not None:
                tir_vocab.add(s.tir_group)

    bands: dict[str, Band] = {}
    p95_delta: dict[str, float] = {}
    for metric in BAND_METRICS:
        values = [
            float(hole.summary[metric])
            for hole in holes
            if hole.summary[metric] is not None
        ]
        if not values:
            raise ValueError(f"no hole establishes {metric}")
        widen = _p95(
            [
                float(hole.split_half_delta[metric])
                for hole in holes
                if hole.split_half_delta[metric] is not None
            ]
        )
        p95_delta[metric] = widen
        lo, hi = min(values) - widen, max(values) + widen
        if metric in FRACTION_METRICS:
            lo, hi = max(0.0, lo), min(1.0, hi)
        bands[metric] = Band(
            name=metric,
            lo=_r(lo),
            hi=_r(hi),
            vacuous=(metric in FRACTION_METRICS and hi - lo >= VACUOUS_SPAN),
        )
    bands["backward_steps"] = Band(name="backward_steps", lo=0, hi=0)

    composition = {}
    for channel in ("swir", "tir"):
        tvds = [
            float(hole.split_half_tvd[channel])
            for hole in holes
            if hole.split_half_tvd[channel] is not None
        ]
        if not tvds:
            raise ValueError(f"no hole establishes {channel} split-half TVD")
        composition[channel] = {
            "median": _r(statistics.median(tvds)),
            "p95": _r(_p95(tvds)),
            "n_holes": len(tvds),
            "note": (
                "Within-hole split-half marginal TVD is large: composition "
                "varies within a single hole, so composition is not "
                "universally checkable. Per-run fidelity is judged against "
                "the source hole's own split-half tolerance."
            ),
        }
    return Thresholds(
        bands=bands,
        swir_vocabulary=frozenset(swir_vocab),
        tir_vocabulary=frozenset(tir_vocab),
        composition=composition,
        split_half_p95={k: _r(v) for k, v in p95_delta.items()},
        provenance={
            "licence": LICENCE,
            "attribution": ATTRIBUTION,
            "holes": hole_provenance,
        },
    )


def write_thresholds(thresholds: Thresholds, path: str | Path) -> Path:
    """Write thresholds JSON (with provenance) to ``path``."""
    out = Path(path)
    out.write_text(json.dumps(thresholds.to_dict(), indent=2, sort_keys=True) + "\n")
    return out


def load_thresholds(path: str | Path) -> Thresholds:
    """Load thresholds JSON written by :func:`write_thresholds`."""
    return Thresholds.from_dict(json.loads(Path(path).read_text()))
