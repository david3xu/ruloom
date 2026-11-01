"""Deterministic checks of artifacts against calibrated thresholds.

Every check returns a :class:`CheckResult` with status ``pass``, ``fail``
or ``skip``. A skip (vacuous band, missing source, unestablished
tolerance) is recorded as a skip, never as a pass. The report verdict is
``pass`` only when no check fails.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..analysis.calibration import (
    Thresholds,
    channel_marginals,
    hole_scalars,
    marginal_tvd,
)
from .dataset import read_csv_bytes, to_csv_bytes
from ..intake.reader import Sample

PASS, FAIL, SKIP = "pass", "fail", "skip"


def source_fidelity_reference(
    source_samples: Sequence[Sample],
) -> dict[str, dict[str, Any]]:
    """Precompute the source-side fidelity references for the checks.

    The engine hands only statistics views to capabilities without raw
    access, so the granted profiling step computes these once from the
    source samples: per-channel full-source marginals plus the
    split-half tolerance. The numbers are identical to the ones the
    checks would compute inline from the same samples.
    """
    source = list(source_samples)
    first, second = source[: len(source) // 2], source[len(source) // 2 :]
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
        }
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


def run_checks(
    samples: Sequence[Sample],
    thresholds: Thresholds,
    *,
    source_samples: Sequence[Sample] | None = None,
    source_reference: Mapping[str, Any] | None = None,
    artifact_name: str = "",
    artifact_sha256: str = "",
) -> CheckReport:
    """Run schema, invariant-band and fidelity checks on samples."""
    samples = list(samples)
    sha = artifact_sha256 or hashlib.sha256(to_csv_bytes(samples)).hexdigest()
    report = CheckReport(artifact_name=artifact_name, artifact_sha256=sha)
    add = report.results.append

    if not samples:
        add(CheckResult("has_rows", FAIL, "empty artifact"))
        return report
    add(CheckResult("has_rows", PASS, f"{len(samples)} rows"))

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
        add(
            CheckResult(
                "depth_non_decreasing", FAIL, "depth decreases between rows"
            )
        )

    add(_band_check("spacing_median", observed["spacing_median_m"],
                    bands["spacing_median_m"].lo, bands["spacing_median_m"].hi))
    add(_band_check("repeated_depth_fraction", observed["repeated_depth_fraction"],
                    bands["repeated_depth_fraction"].lo,
                    bands["repeated_depth_fraction"].hi))
    add(_band_check("valid_fraction", observed["valid_fraction"],
                    bands["valid_fraction"].lo, bands["valid_fraction"].hi))

    n_repeated = sum(1 for d in diffs if d == 0)
    if n_repeated == 0:
        add(CheckResult("repeated_depths_masked_out", PASS, "no repeated depths"))
    else:
        add(_band_check("repeated_depths_masked_out",
                        observed["repeated_valid_fraction"],
                        bands["repeated_valid_fraction"].lo,
                        bands["repeated_valid_fraction"].hi))

    for channel in ("swir", "tir"):
        band = bands[f"{channel}_answer_rate"]
        if band.vacuous:
            add(CheckResult(f"{channel}_answer_rate", SKIP,
                            f"band vacuous [{band.lo}, {band.hi}]"))
        else:
            add(_band_check(f"{channel}_answer_rate", observed[f"{channel}_answer_rate"],
                            band.lo, band.hi))

    labels = {
        channel: {getattr(s, channel) for s in samples if getattr(s, channel) is not None}
        for channel in ("swir_group", "tir_group")
    }
    for channel, vocab in (
        ("swir", thresholds.swir_vocabulary),
        ("tir", thresholds.tir_vocabulary),
    ):
        unknown = sorted(labels[f"{channel}_group"] - set(vocab))
        if unknown:
            shown = ", ".join(unknown[:5])
            add(CheckResult(f"{channel}_vocabulary", FAIL,
                            f"unknown {channel} groups: {shown}"))
        else:
            add(CheckResult(f"{channel}_vocabulary", PASS,
                            f"{len(labels[f'{channel}_group'])} {channel} groups in vocabulary"))

    for channel in ("swir", "tir"):
        name = f"{channel}_fidelity"
        if source_reference is not None:
            # Statistics view computed by the granted profiling step from
            # the same source samples; the numbers below are identical to
            # the inline path. No raw source reaches this capability.
            channel_ref = source_reference.get(channel)
            if not isinstance(channel_ref, Mapping):
                add(CheckResult(name, SKIP, "no source samples provided"))
                continue
            source_marginals = channel_ref.get("marginals")
            tolerance = channel_ref.get("tolerance")
        else:
            if source_samples is None:
                add(CheckResult(name, SKIP, "no source samples provided"))
                continue
            source = list(source_samples)
            first, second = (
                source[: len(source) // 2], source[len(source) // 2 :]
            )
            tolerance = marginal_tvd(
                channel_marginals(first, f"{channel}_group"),
                channel_marginals(second, f"{channel}_group"),
            )
            source_marginals = channel_marginals(source, f"{channel}_group")
        if tolerance is None:
            add(CheckResult(name, SKIP,
                            "source cannot establish split-half tolerance"))
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
            add(CheckResult(
                name, PASS, f"TVD {distance:.6f} <= {tolerance:.6f}",
                band_position(distance, 0.0, tolerance),
            ))
        else:
            add(CheckResult(
                name, FAIL, f"TVD {distance:.6f} > {tolerance:.6f}",
                band_position(distance, 0.0, tolerance),
            ))
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
    report = run_checks(
        samples,
        thresholds,
        source_samples=source_samples,
        artifact_name=artifact_name,
        artifact_sha256=sha,
    )
    report.results.insert(
        0, CheckResult("csv_schema", PASS, f"{len(samples)} rows, header ok")
    )
    return report
