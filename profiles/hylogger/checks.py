"""Deterministic checks of artifacts against calibrated thresholds.

Every check returns a :class:`CheckResult` with status ``pass``, ``fail``
or ``skip``. A skip (vacuous band, missing source, unestablished
tolerance) is recorded as a skip, never as a pass. The report verdict is
``pass`` only when no check fails.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Sequence

from .calibration import (
    Thresholds,
    channel_marginals,
    hole_scalars,
    marginal_tvd,
)
from .dataset import read_csv_bytes, to_csv_bytes
from .reader import Sample

PASS, FAIL, SKIP = "pass", "fail", "skip"


@dataclass(frozen=True)
class CheckResult:
    """One named check outcome; ``status`` is pass, fail or skip."""

    name: str
    status: str
    detail: str


@dataclass
class CheckReport:
    """All check outcomes for one artifact, bound to its sha256."""

    artifact_name: str
    artifact_sha256: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """``pass`` only when no check fails (skips permitted, shown)."""
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
                {"name": r.name, "status": r.status, "detail": r.detail}
                for r in self.results
            ],
        }


def _band_check(name: str, value: float | None, lo: float, hi: float) -> CheckResult:
    if value is None:
        return CheckResult(name, SKIP, "no data to compare")
    if lo <= value <= hi:
        return CheckResult(name, PASS, f"{value} in [{lo}, {hi}]")
    return CheckResult(name, FAIL, f"{value} outside [{lo}, {hi}]")


def run_checks(
    samples: Sequence[Sample],
    thresholds: Thresholds,
    *,
    source_samples: Sequence[Sample] | None = None,
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
        if source_samples is None:
            add(CheckResult(name, SKIP, "no source samples provided"))
            continue
        source = list(source_samples)
        first, second = source[: len(source) // 2], source[len(source) // 2 :]
        tolerance = marginal_tvd(
            channel_marginals(first, f"{channel}_group"),
            channel_marginals(second, f"{channel}_group"),
        )
        if tolerance is None:
            add(CheckResult(name, SKIP,
                            "source cannot establish split-half tolerance"))
            continue
        distance = marginal_tvd(
            channel_marginals(samples, f"{channel}_group"),
            channel_marginals(source, f"{channel}_group"),
        )
        if distance is None:
            if channel_marginals(source, f"{channel}_group") is None:
                add(CheckResult(name, PASS, "neither artifact nor source answers"))
            else:
                add(CheckResult(name, FAIL, "artifact never answers, source does"))
        elif distance <= tolerance:
            add(CheckResult(name, PASS, f"TVD {distance:.6f} <= {tolerance:.6f}"))
        else:
            add(CheckResult(name, FAIL, f"TVD {distance:.6f} > {tolerance:.6f}"))
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
