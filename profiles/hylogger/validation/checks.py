"""Deterministic checks of artifacts against calibrated thresholds.

Every check returns a :class:`CheckResult` with status ``pass``, ``fail``
or ``skip``. A skip (vacuous band, missing source, unestablished
tolerance) is recorded as a skip, never as a pass. The report verdict is
``pass`` only when no check fails.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..analysis.calibration import (
    Thresholds,
    channel_marginals,
    hole_scalars,
    marginal_tvd,
    scalar_summary,
)
from ..analysis.statistics import build_stats
from ..intake.reader import Sample
from .dataset import read_csv_bytes, to_csv_bytes

PASS, FAIL, SKIP = "pass", "fail", "skip"

#: A split-half TVD tolerance at the mathematical maximum carries no
#: information: every marginal is within 1.0 of every other, so no
#: composition can fail. Fidelity against such a source is reported
#: unsupported (skip), never a pass. Measured across the 43 real holes
#: the largest split-half TVD is 0.895, so this skip never fires on
#: real sources -- only on pathological fixtures.
TVD_VACUOUS_TOLERANCE = 1.0

#: Slack for the requested-extent bound below. Synthetic depths
#: accumulate ``start + k * step`` in float arithmetic, so the final
#: depth can sit a few ULPs above the exact requested end; 1e-6 m is
#: orders of magnitude below any real spacing step while covering the
#: worst-case accumulation over the manifest row maximum.
END_FLOAT_EPS_M = 1e-6

#: p95 of the within-hole split-half ``|delta|`` of TIR neighbouring
#: persistence across the 38 frozen calibration holes (the
#: ``thresholds.json`` provenance set), computed 2026-11-02 as
#: 0.155469. A synthetic holds its source hole's TIR run structure
#: when its persistence sits within the hole's own split-half delta
#: plus this cross-hole allowance (plus a finite-sample term). The
#: derivation is recorded in ``docs/quality/scorecard-cp02-addendum.md``;
#: ``tests/profiles/hylogger/test_calibration.py`` recomputes the value
#: from ``data/calibration`` whenever the source cache is present.
TIR_PERSISTENCE_P95 = 0.155469


def _source_labels(samples: Sequence[Sample], channel: str) -> frozenset:
    """Distinct answered labels on valid rows."""
    key = f"{channel}_group"
    return frozenset(
        getattr(s, key) for s in samples if s.valid and getattr(s, key) is not None
    )


def _invalid_label_rate(samples: Sequence[Sample]) -> float | None:
    """Fraction of invalid rows carrying any group label.

    ``None`` when the artifact has no invalid rows (nothing to judge).
    """
    invalid = [s for s in samples if not s.valid]
    if not invalid:
        return None
    labelled = sum(
        1 for s in invalid if s.swir_group is not None or s.tir_group is not None
    )
    return labelled / len(invalid)


def source_fidelity_reference(
    source_samples: Sequence[Sample],
) -> dict[str, dict[str, Any]]:
    """Precompute the source-side fidelity references for the checks.

    The engine hands only statistics views to capabilities without raw
    access, so the granted profiling step computes these once from the
    source samples: per-channel full-source marginals plus the
    split-half tolerance, the source answer rate with its split-half
    delta, the source answered-label set, and the source
    invalid-label rate. The numbers are identical to the ones the
    checks would compute inline from the same samples.
    """
    source = list(source_samples)
    first, second = source[: len(source) // 2], source[len(source) // 2 :]
    hole = hole_scalars(source)
    persist_all = scalar_summary(source)["tir_persistence"]
    persist_first = scalar_summary(first)["tir_persistence"]
    persist_second = scalar_summary(second)["tir_persistence"]
    if persist_first is None or persist_second is None or persist_all is None:
        persist_tolerance = None
    else:
        persist_tolerance = abs(float(persist_first) - float(persist_second))
    reference: dict[str, dict[str, Any]] = {}
    for channel in ("swir", "tir"):
        key = f"{channel}_group"
        tolerance = marginal_tvd(
            channel_marginals(first, key),
            channel_marginals(second, key),
        )
        reference[channel] = {
            "marginals": channel_marginals(source, key),
            "tolerance": tolerance,
            "answer_rate": hole.summary[f"{channel}_answer_rate"],
            "answer_rate_tolerance": hole.split_half_delta[f"{channel}_answer_rate"],
            "labels": sorted(_source_labels(source, channel)),
            "persistence": persist_all,
            "persistence_tolerance": persist_tolerance,
        }
    reference["invalid_label_rate"] = _invalid_label_rate(source)
    return reference


def band_position(value: float, lo: float, hi: float) -> float:
    """Fractional position of ``value`` across the band ``[lo, hi]``.

    Inside the band the value maps to ``[0.0, 1.0]`` (``lo`` -> ``0.0``,
    midpoint -> ``0.5``, ``hi`` -> ``1.0``). Outside, the result is the
    signed distance in band-widths: negative below ``lo``, above ``1.0``
    past ``hi``. A zero-width band reports ``0.0`` on the line and a unit
    step off it. Pass/fail semantics never read this value.
    """
    width = hi - lo
    if width == 0:
        if value == lo:
            return 0.0
        return 1.0 if value > hi else -1.0
    return (value - lo) / width


@dataclass(frozen=True)
class CheckResult:
    """One named check outcome; ``status`` is pass, fail or skip.

    ``band_position`` is the measured value's fractional position across
    its band (``None`` for checks with no band: schema, vocabulary,
    skips). It rides alongside pass/fail and never changes it.
    """

    name: str
    status: str
    detail: str
    band_position: float | None = None


@dataclass
class CheckReport:
    """All check outcomes for one artifact, bound to its sha256."""

    artifact_name: str
    artifact_sha256: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """``pass`` only when no check fails (skips permitted, shown).

        ``band_position`` never participates: the verdict reads status
        only, exactly as before.
        """
        return FAIL if any(r.status == FAIL for r in self.results) else PASS

    @property
    def failed(self) -> list[str]:
        return [r.name for r in self.results if r.status == FAIL]

    @property
    def skipped(self) -> list[str]:
        return [r.name for r in self.results if r.status == SKIP]

    def to_dict(self) -> dict:
        return {
            "artifact_name": self.artifact_name,
            "artifact_sha256": self.artifact_sha256,
            "verdict": self.verdict,
            "results": [
                {
                    "name": r.name,
                    "status": r.status,
                    "detail": r.detail,
                    "band_position": r.band_position,
                }
                for r in self.results
            ],
        }


def _band_check(name: str, value: float | None, lo: float, hi: float) -> CheckResult:
    if value is None:
        return CheckResult(name, SKIP, "no data to compare")
    position = band_position(value, lo, hi)
    if lo <= value <= hi:
        return CheckResult(name, PASS, f"{value} in [{lo}, {hi}]", position)
    return CheckResult(name, FAIL, f"{value} outside [{lo}, {hi}]", position)


def _source_channel(
    reference: Mapping[str, Any] | None, channel: str
) -> Mapping[str, Any] | None:
    """The per-channel source reference mapping, or ``None`` when absent."""
    if not isinstance(reference, Mapping):
        return None
    ref = reference.get(channel)
    return ref if isinstance(ref, Mapping) else None


def _expected_number(expected: Mapping[str, Any] | None, key: str) -> float | None:
    """A finite requested-geometry number, or ``None`` when not plannable."""
    if not isinstance(expected, Mapping):
        return None
    value = expected.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def run_checks(
    samples: Sequence[Sample],
    thresholds: Thresholds,
    *,
    source_samples: Sequence[Sample] | None = None,
    source_reference: Mapping[str, Any] | None = None,
    expected_geometry: Mapping[str, Any] | None = None,
    expected_valid_count: int | None = None,
    artifact_name: str = "",
    artifact_sha256: str = "",
) -> CheckReport:
    """Run schema, invariant-band and fidelity checks on samples.

    ``source_reference`` (or, failing that, ``source_samples``) carries
    the source hole's own answer rates, label sets and split-half
    tolerances; without either, the source-relative checks report
    ``skip`` (unsupported), never a pass. ``expected_geometry``
    optionally binds the requested plan geometry (``n_rows``,
    ``start_m``, ``step_m``); without it the plan-binding checks skip.
    """
    samples = list(samples)
    sha = artifact_sha256 or hashlib.sha256(to_csv_bytes(samples)).hexdigest()
    report = CheckReport(artifact_name=artifact_name, artifact_sha256=sha)
    add = report.results.append

    # The schema check rides with the report itself, so the shipped
    # validation report and the engine-bound check results agree on
    # exactly one passing ``csv_schema``: successful parsing is the
    # schema evidence, and both readers consume this same list.
    add(CheckResult("csv_schema", PASS, f"{len(samples)} rows, header ok"))

    if not samples:
        add(CheckResult("has_rows", FAIL, "empty artifact"))
        return report
    add(CheckResult("has_rows", PASS, f"{len(samples)} rows"))

    if source_reference is None and source_samples is not None:
        source_reference = source_fidelity_reference(source_samples)

    observed = hole_scalars(samples).summary
    bands = thresholds.bands

    backward = int(observed["backward_steps"])
    if backward == 0:
        add(CheckResult("backward_steps", PASS, "0 backward steps"))
    else:
        add(CheckResult("backward_steps", FAIL, f"{backward} backward steps (band 0)"))
    diffs = [b.depth_m - a.depth_m for a, b in zip(samples, samples[1:])]
    if all(d >= 0 for d in diffs):
        add(CheckResult("depth_non_decreasing", PASS, "depths never decrease"))
    else:
        add(CheckResult("depth_non_decreasing", FAIL, "depth decreases between rows"))

    n_non_finite = sum(1 for s in samples if not math.isfinite(s.depth_m))
    if n_non_finite == 0:
        add(CheckResult("depth_finite", PASS, "all depths finite"))
    else:
        add(CheckResult("depth_finite", FAIL, f"{n_non_finite} non-finite depths"))

    n_negative = sum(1 for s in samples if math.isfinite(s.depth_m) and s.depth_m < 0)
    if n_negative == 0:
        add(
            CheckResult(
                "depth_nonnegative",
                PASS,
                "all depths use the documented nonnegative convention",
            )
        )
    else:
        add(
            CheckResult(
                "depth_nonnegative",
                FAIL,
                f"{n_negative} negative depths (documented convention: "
                "StartDepth metres are never negative)",
            )
        )

    add(
        _band_check(
            "spacing_median",
            observed["spacing_median_m"],
            bands["spacing_median_m"].lo,
            bands["spacing_median_m"].hi,
        )
    )
    add(
        _band_check(
            "repeated_depth_fraction",
            observed["repeated_depth_fraction"],
            bands["repeated_depth_fraction"].lo,
            bands["repeated_depth_fraction"].hi,
        )
    )
    if expected_valid_count is not None:
        val_frac = observed["valid_fraction"]
        lo = bands["valid_fraction"].lo
        hi = bands["valid_fraction"].hi
        actual_valid = sum(1 for s in samples if s.valid)
        if (
            isinstance(expected_valid_count, bool)
            or not isinstance(expected_valid_count, int)
            or expected_valid_count < 0
            or val_frac is None
            or not math.isfinite(val_frac)
        ):
            add(
                CheckResult(
                    "valid_fraction",
                    FAIL,
                    f"invalid valid_fraction or fitted target: val_frac={val_frac}, target={expected_valid_count}",
                )
            )
        else:
            pos = band_position(val_frac, lo, hi)
            in_band = lo <= val_frac <= hi
            count_match = actual_valid == expected_valid_count
            if count_match and in_band:
                add(
                    CheckResult(
                        "valid_fraction",
                        PASS,
                        f"{val_frac} in [{lo}, {hi}] (actual count {actual_valid} == fitted target {expected_valid_count})",
                        pos,
                    )
                )
            elif not count_match and in_band:
                add(
                    CheckResult(
                        "valid_fraction",
                        FAIL,
                        f"{val_frac} in [{lo}, {hi}], but actual count {actual_valid} != fitted target {expected_valid_count}",
                        pos,
                    )
                )
            else:
                add(
                    CheckResult(
                        "valid_fraction",
                        FAIL,
                        f"{val_frac} outside [{lo}, {hi}] (actual count {actual_valid}, fitted target {expected_valid_count})",
                        pos,
                    )
                )
    else:
        add(
            _band_check(
                "valid_fraction",
                observed["valid_fraction"],
                bands["valid_fraction"].lo,
                bands["valid_fraction"].hi,
            )
        )

    n_repeated = sum(1 for d in diffs if d == 0)
    if n_repeated == 0:
        add(CheckResult("repeated_depths_masked_out", PASS, "no repeated depths"))
    else:
        add(
            _band_check(
                "repeated_depths_masked_out",
                observed["repeated_valid_fraction"],
                bands["repeated_valid_fraction"].lo,
                bands["repeated_valid_fraction"].hi,
            )
        )

    artifact_invalid_rate = _invalid_label_rate(samples)
    if artifact_invalid_rate is None:
        add(CheckResult("invalid_label_rate", PASS, "no invalid rows"))
    elif not isinstance(source_reference, Mapping):
        add(CheckResult("invalid_label_rate", SKIP, "no source samples provided"))
    else:
        source_rate = source_reference.get("invalid_label_rate")
        if not isinstance(source_rate, (int, float)) or isinstance(source_rate, bool):
            add(
                CheckResult(
                    "invalid_label_rate",
                    SKIP,
                    "source cannot establish invalid-label rate",
                )
            )
        elif artifact_invalid_rate <= float(source_rate):
            add(
                CheckResult(
                    "invalid_label_rate",
                    PASS,
                    f"invalid-label rate {artifact_invalid_rate:.6f} <= source "
                    f"{float(source_rate):.6f}",
                )
            )
        else:
            add(
                CheckResult(
                    "invalid_label_rate",
                    FAIL,
                    f"invalid-label rate {artifact_invalid_rate:.6f} > source "
                    f"{float(source_rate):.6f}",
                )
            )

    for channel in ("swir", "tir"):
        band = bands[f"{channel}_answer_rate"]
        if band.vacuous:
            add(
                CheckResult(
                    f"{channel}_answer_rate",
                    SKIP,
                    f"band vacuous [{band.lo}, {band.hi}]",
                )
            )
        else:
            add(
                _band_check(
                    f"{channel}_answer_rate",
                    observed[f"{channel}_answer_rate"],
                    band.lo,
                    band.hi,
                )
            )

    for channel in ("swir", "tir"):
        name = f"{channel}_answer_fidelity"
        ref = _source_channel(source_reference, channel)
        if ref is None:
            add(CheckResult(name, SKIP, "no source samples provided"))
            continue
        source_rate = ref.get("answer_rate")
        delta = ref.get("answer_rate_tolerance")
        if (
            not isinstance(source_rate, (int, float))
            or isinstance(source_rate, bool)
            or not isinstance(delta, (int, float))
            or isinstance(delta, bool)
        ):
            add(
                CheckResult(name, SKIP, "source cannot establish answer-rate tolerance")
            )
            continue
        # Tolerance is three measured terms: the source hole's own
        # split-half delta (its spatial heterogeneity), the cross-hole
        # p95 widening from the frozen calibration, and a finite-sample
        # allowance for the artifact. A Bernoulli rate over n valid rows
        # has standard error at most 0.5/sqrt(n), so 2/sqrt(n) covers
        # four sigma of sampling noise: without it, single-digit-row
        # runs fail on sampling luck (drift 0.29 at n=7) while the
        # audit's severe removal (drift 0.99 at n=1200) still fails
        # with a factor-two margin.
        n_valid = sum(1 for s in samples if s.valid)
        tolerance = (
            abs(float(delta))
            + thresholds.split_half_p95.get(f"{channel}_answer_rate", 0.0)
            + (2.0 / math.sqrt(n_valid) if n_valid else float("inf"))
        )
        observed_rate = observed[f"{channel}_answer_rate"]
        if observed_rate is None:
            add(CheckResult(name, SKIP, "no valid rows to compare"))
            continue
        drift = abs(float(observed_rate) - float(source_rate))
        position = band_position(drift, 0.0, tolerance)
        if drift <= tolerance:
            add(
                CheckResult(
                    name,
                    PASS,
                    f"answer-rate drift {drift:.6f} <= {tolerance:.6f}",
                    position,
                )
            )
        else:
            add(
                CheckResult(
                    name,
                    FAIL,
                    f"answer-rate drift {drift:.6f} > {tolerance:.6f}",
                    position,
                )
            )

    labels = {
        channel: {
            getattr(s, channel) for s in samples if getattr(s, channel) is not None
        }
        for channel in ("swir_group", "tir_group")
    }
    for channel, vocab in (
        ("swir", thresholds.swir_vocabulary),
        ("tir", thresholds.tir_vocabulary),
    ):
        unknown = sorted(labels[f"{channel}_group"] - set(vocab))
        if unknown:
            shown = ", ".join(unknown[:5])
            add(
                CheckResult(
                    f"{channel}_vocabulary", FAIL, f"unknown {channel} groups: {shown}"
                )
            )
        else:
            add(
                CheckResult(
                    f"{channel}_vocabulary",
                    PASS,
                    f"{len(labels[f'{channel}_group'])} {channel} groups in vocabulary",
                )
            )

    for channel in ("swir", "tir"):
        name = f"{channel}_source_vocabulary"
        ref = _source_channel(source_reference, channel)
        artifact_labels = _source_labels(samples, channel)
        if ref is None:
            add(CheckResult(name, SKIP, "no source samples provided"))
        else:
            raw_labels = ref.get("labels")
            if not isinstance(raw_labels, list) or any(
                not isinstance(label, str) for label in raw_labels
            ):
                add(CheckResult(name, SKIP, "source cannot establish label support"))
            else:
                source_labels = set(raw_labels)
                foreign = sorted(artifact_labels - source_labels)
                if not foreign:
                    add(
                        CheckResult(
                            name,
                            PASS,
                            f"{len(artifact_labels)} {channel} groups answered "
                            "by the source",
                        )
                    )
                else:
                    shown = ", ".join(foreign[:5])
                    add(
                        CheckResult(
                            name,
                            FAIL,
                            f"foreign {channel} groups the source never "
                            f"answers: {shown}",
                        )
                    )
        name = f"{channel}_label_richness"
        ref = _source_channel(source_reference, channel)
        if ref is None:
            add(CheckResult(name, SKIP, "no source samples provided"))
        else:
            raw_labels = ref.get("labels")
            if not isinstance(raw_labels, list) or any(
                not isinstance(label, str) for label in raw_labels
            ):
                add(CheckResult(name, SKIP, "source cannot establish label support"))
            elif len(artifact_labels) == 1 and len(set(raw_labels)) >= 2:
                only = sorted(artifact_labels)[0]
                add(
                    CheckResult(
                        name,
                        FAIL,
                        f"vocabulary collapse: only {only!r} answers while the "
                        f"source answers {len(set(raw_labels))} {channel} groups",
                    )
                )
            else:
                add(
                    CheckResult(
                        name, PASS, f"{len(artifact_labels)} {channel} groups answer"
                    )
                )

    for channel in ("swir", "tir"):
        name = f"{channel}_fidelity"
        # Statistics view computed by the granted profiling step from
        # the same source samples (or, failing that, built inline from
        # source_samples above); the numbers are identical either way.
        # No raw source reaches this capability.
        channel_ref = _source_channel(source_reference, channel)
        if channel_ref is None:
            add(CheckResult(name, SKIP, "no source samples provided"))
            continue
        source_marginals = channel_ref.get("marginals")
        tolerance = channel_ref.get("tolerance")
        if tolerance is None:
            add(CheckResult(name, SKIP, "source cannot establish split-half tolerance"))
            continue
        if tolerance >= TVD_VACUOUS_TOLERANCE:
            add(
                CheckResult(
                    name,
                    SKIP,
                    f"source split-half tolerance {tolerance:.6f} is vacuous: "
                    "composition unfalsifiable for this source",
                )
            )
            continue
        distance = marginal_tvd(
            channel_marginals(samples, f"{channel}_group"),
            source_marginals,
        )
        if distance is None:
            if source_marginals is None:
                add(CheckResult(name, PASS, "neither artifact nor source answers"))
            else:
                add(CheckResult(name, FAIL, "artifact never answers, source does"))
        elif distance <= tolerance:
            add(
                CheckResult(
                    name,
                    PASS,
                    f"TVD {distance:.6f} <= {tolerance:.6f}",
                    band_position(distance, 0.0, tolerance),
                )
            )
        else:
            add(
                CheckResult(
                    name,
                    FAIL,
                    f"TVD {distance:.6f} > {tolerance:.6f}",
                    band_position(distance, 0.0, tolerance),
                )
            )

    tir_ref = _source_channel(source_reference, "tir")
    if tir_ref is None:
        add(CheckResult("tir_persistence_fidelity", SKIP, "no source samples provided"))
    else:
        source_persist = tir_ref.get("persistence")
        persist_delta = tir_ref.get("persistence_tolerance")
        synth_stats = build_stats(samples)
        synth_persist = synth_stats["tir"]["persistence"]
        n_pairs = synth_stats["tir"]["n_pairs"]
        if (
            not isinstance(source_persist, (int, float))
            or isinstance(source_persist, bool)
            or not isinstance(persist_delta, (int, float))
            or isinstance(persist_delta, bool)
        ):
            add(
                CheckResult(
                    "tir_persistence_fidelity",
                    SKIP,
                    "source cannot establish persistence tolerance",
                )
            )
        elif synth_persist is None or not n_pairs:
            add(
                CheckResult(
                    "tir_persistence_fidelity",
                    SKIP,
                    "artifact answers too few TIR pairs to compare",
                )
            )
        else:
            # Tolerance mirrors the answer-fidelity pattern: the source
            # hole's own split-half delta (its spatial heterogeneity),
            # the cross-hole p95 widening from the frozen calibration,
            # and a finite-sample allowance over the artifact's answered
            # pairs. The re-audit's joint-conditioned draw drifts ~0.25
            # below source, failing with margin; run-preserving draws
            # sit an order of magnitude inside.
            tolerance = (
                abs(float(persist_delta))
                + TIR_PERSISTENCE_P95
                + (2.0 / math.sqrt(n_pairs))
            )
            drift = abs(float(synth_persist) - float(source_persist))
            position = band_position(drift, 0.0, tolerance)
            if drift <= tolerance:
                add(
                    CheckResult(
                        "tir_persistence_fidelity",
                        PASS,
                        f"TIR persistence drift {drift:.6f} <= {tolerance:.6f}",
                        position,
                    )
                )
            else:
                add(
                    CheckResult(
                        "tir_persistence_fidelity",
                        FAIL,
                        f"TIR persistence drift {drift:.6f} > {tolerance:.6f}",
                        position,
                    )
                )

    n_expected = _expected_number(expected_geometry, "n_rows")
    n_int = (
        int(n_expected)
        if n_expected is not None and n_expected.is_integer() and n_expected >= 1
        else None
    )
    if n_int is None:
        add(CheckResult("row_count_match", SKIP, "no requested row count"))
    elif len(samples) == n_int:
        add(CheckResult("row_count_match", PASS, f"{len(samples)} rows as requested"))
    else:
        add(
            CheckResult(
                "row_count_match", FAIL, f"{len(samples)} rows, requested {n_int}"
            )
        )

    start_expected = _expected_number(expected_geometry, "start_m")
    if start_expected is None:
        add(CheckResult("depth_start_match", SKIP, "no requested start"))
    elif samples[0].depth_m == start_expected:
        add(
            CheckResult(
                "depth_start_match", PASS, f"start {samples[0].depth_m} as requested"
            )
        )
    else:
        add(
            CheckResult(
                "depth_start_match",
                FAIL,
                f"start {samples[0].depth_m}, requested {start_expected}",
            )
        )

    step_expected = _expected_number(expected_geometry, "step_m")
    if n_int is None or start_expected is None or step_expected is None:
        add(CheckResult("depth_end_within_requested", SKIP, "no requested geometry"))
    else:
        bound = start_expected + step_expected * (n_int - 1) + END_FLOAT_EPS_M
        end = samples[-1].depth_m
        if end <= bound:
            add(
                CheckResult(
                    "depth_end_within_requested",
                    PASS,
                    f"end {end} within requested extent {bound}",
                )
            )
        else:
            add(
                CheckResult(
                    "depth_end_within_requested",
                    FAIL,
                    f"end {end} beyond requested extent {bound}",
                )
            )

    if n_int is None or start_expected is None or step_expected is None:
        add(CheckResult("spacing_step_match", SKIP, "no requested geometry"))
        add(CheckResult("depth_span_match", SKIP, "no requested geometry"))
    else:
        # Two-sided plan fidelity (A04): the median forward spacing must
        # reproduce the requested plan step, and the end depth must equal
        # the requested span after allowing exactly the artifact's own
        # masked repeats. The step tolerance is the frozen calibration
        # split-half p95 (no new band, thresholds.json untouched); the
        # span tolerance is float accumulation slack only. Uniform
        # contraction (or any rescaling) fails both; unmasked extension
        # fails the span alongside the one-sided extent bound above.
        median_spacing = observed["spacing_median_m"]
        if median_spacing is None:
            add(
                CheckResult("spacing_step_match", SKIP, "no forward spacing to compare")
            )
        else:
            step_tolerance = (
                abs(thresholds.split_half_p95.get("spacing_median_m", 0.0))
                + END_FLOAT_EPS_M
            )
            step_drift = abs(float(median_spacing) - step_expected)
            position = band_position(step_drift, 0.0, step_tolerance)
            if step_drift <= step_tolerance:
                add(
                    CheckResult(
                        "spacing_step_match",
                        PASS,
                        f"median spacing {median_spacing} matches requested "
                        f"step {step_expected} (drift {step_drift:.6f} <= "
                        f"{step_tolerance:.6f})",
                        position,
                    )
                )
            else:
                add(
                    CheckResult(
                        "spacing_step_match",
                        FAIL,
                        f"median spacing {median_spacing} outside requested "
                        f"step {step_expected} (drift {step_drift:.6f} > "
                        f"{step_tolerance:.6f})",
                        position,
                    )
                )
        n_repeated = sum(1 for d in diffs if d == 0)
        expected_end = start_expected + step_expected * (n_int - 1 - n_repeated)
        span_drift = abs(samples[-1].depth_m - expected_end)
        if span_drift <= END_FLOAT_EPS_M:
            add(
                CheckResult(
                    "depth_span_match",
                    PASS,
                    f"end {samples[-1].depth_m} matches requested span "
                    f"{expected_end} with {n_repeated} masked repeats",
                    band_position(span_drift, 0.0, END_FLOAT_EPS_M),
                )
            )
        else:
            add(
                CheckResult(
                    "depth_span_match",
                    FAIL,
                    f"end {samples[-1].depth_m} outside requested span "
                    f"{expected_end} with {n_repeated} masked repeats "
                    f"(drift {span_drift:.6f})",
                    band_position(span_drift, 0.0, END_FLOAT_EPS_M),
                )
            )
    return report


def check_artifact_csv(
    payload: bytes | str,
    thresholds: Thresholds,
    *,
    source_samples: Sequence[Sample] | None = None,
    artifact_name: str = "",
) -> CheckReport:
    """Parse a synthetic-CSV artifact (schema check) then run all checks."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    sha = hashlib.sha256(raw).hexdigest()
    try:
        samples = read_csv_bytes(raw)
    except ValueError as exc:
        return CheckReport(
            artifact_name=artifact_name,
            artifact_sha256=sha,
            results=[CheckResult("csv_schema", FAIL, str(exc))],
        )
    # ``run_checks`` already records the passing ``csv_schema`` first,
    # so the parsed artifact carries it exactly once here as well.
    return run_checks(
        samples,
        thresholds,
        source_samples=source_samples,
        artifact_name=artifact_name,
        artifact_sha256=sha,
    )
