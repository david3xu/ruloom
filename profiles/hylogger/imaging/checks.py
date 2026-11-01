"""Deterministic image checks: synthetic PNGs against source statistics.

Three checks, all computed by code over artifact bytes (never by a
model), reusing the profile :class:`CheckResult`/`CheckReport` contract:

* ``image_histogram_distance`` — mean total-variation distance across
  the RGB histograms of the source reference and the synthetic image,
  inside the literal band ``[0, HISTOGRAM_DISTANCE_MAX]`` (threshold
  source ``literal``; the constant below states the value);
* ``image_dimensions`` — synthetic width/height equal the reference
  (threshold source ``literal``);
* ``image_strip_alignment`` — synthetic strip (depth-count) coverage
  equals the reference strip count (threshold source ``literal``).

The report verdict is ``pass`` only when no check fails; the engine
binds the report to the synthetic artifact hash when the
``image_validate`` capability records these results.
"""

from __future__ import annotations

import hashlib
from typing import Mapping

from ..validation.checks import CheckReport, CheckResult
from .statistics import ImageStats, compute_statistics

PASS, FAIL = "pass", "fail"

#: Literal histogram-distance band: the synthetic image passes when its
#: mean channel TVD from the source reference is at most this value
#: (threshold source "literal", stated here and in the manifest entry).
HISTOGRAM_DISTANCE_MAX = 0.35


def histogram_distance(
    reference: Mapping[str, list[int] | list[float]],
    candidate: Mapping[str, list[int] | list[float]],
    n_pixels_reference: int,
    n_pixels_candidate: int,
) -> float:
    """Mean total-variation distance across the R/G/B histograms.

    Each channel contributes half its L1 distance between the normalised
    histograms; the three contributions are averaged, so the result lies
    in ``[0, 1]`` with 0 for identical distributions.
    """
    total = 0.0
    for channel in ("r", "g", "b"):
        ref = [float(v) / n_pixels_reference for v in reference[channel]]
        cand = [float(v) / n_pixels_candidate for v in candidate[channel]]
        total += sum(abs(a - b) for a, b in zip(ref, cand)) / 2.0
    return total / 3.0


def _pixel_count(stats: ImageStats) -> int:
    return stats.width * stats.height


def run_image_checks(
    reference: ImageStats,
    candidate_png: bytes,
    *,
    artifact_name: str = "",
    artifact_sha256: str = "",
) -> CheckReport:
    """Compare one synthetic PNG against the source reference statistics."""
    sha = artifact_sha256 or hashlib.sha256(candidate_png).hexdigest()
    candidate = compute_statistics(candidate_png, artifact_name or "synthetic")
    report = CheckReport(artifact_name=artifact_name, artifact_sha256=sha)
    add = report.results.append

    distance = histogram_distance(
        reference.histograms,
        candidate.histograms,
        _pixel_count(reference),
        _pixel_count(candidate),
    )
    if distance <= HISTOGRAM_DISTANCE_MAX:
        add(
            CheckResult(
                "image_histogram_distance",
                PASS,
                f"{distance:.6f} in [0, {HISTOGRAM_DISTANCE_MAX}] "
                "(threshold source: literal)",
            )
        )
    else:
        add(
            CheckResult(
                "image_histogram_distance",
                FAIL,
                f"{distance:.6f} outside [0, {HISTOGRAM_DISTANCE_MAX}] "
                "(threshold source: literal)",
            )
        )

    if (
        candidate.width == reference.width
        and candidate.height == reference.height
    ):
        add(
            CheckResult(
                "image_dimensions",
                PASS,
                f"{candidate.width}x{candidate.height} matches reference "
                "(threshold source: literal)",
            )
        )
    else:
        add(
            CheckResult(
                "image_dimensions",
                FAIL,
                f"{candidate.width}x{candidate.height} != reference "
                f"{reference.width}x{reference.height} "
                "(threshold source: literal)",
            )
        )

    if len(candidate.strips) == len(reference.strips):
        add(
            CheckResult(
                "image_strip_alignment",
                PASS,
                f"{len(candidate.strips)} strips match reference depth count "
                "(threshold source: literal)",
            )
        )
    else:
        add(
            CheckResult(
                "image_strip_alignment",
                FAIL,
                f"{len(candidate.strips)} strips != reference "
                f"{len(reference.strips)} "
                "(threshold source: literal)",
            )
        )
    return report


def check_names() -> list[str]:
    """Check ids this module reports (mirrors the manifest entries)."""
    return [
        "image_histogram_distance",
        "image_dimensions",
        "image_strip_alignment",
    ]


__all__ = [
    "HISTOGRAM_DISTANCE_MAX",
    "check_names",
    "histogram_distance",
    "run_image_checks",
]
