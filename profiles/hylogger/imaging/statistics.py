"""Code-only image statistics (no model sees pixels).

For each inventoried image this module computes, with the standard
library only:

* per-channel colour histograms (``N_BINS`` bins per channel),
* per-channel mean colour,
* simple texture measures: mean absolute horizontal and vertical
  neighbour differences per channel,
* per-strip summaries: the rows split into ``N_STRIPS`` strips, each
  with its mean colour (the depth-strip analogue for core imagery).

PNG decode choice: only PNG is decoded, via :mod:`codec` (stdlib
``zlib``/``struct``), which supports the 8-bit non-interlaced RGB
flavour our own encoder writes plus greyscale/RGBA equivalents. JPEG
has no stdlib decoder, so JPEG records are inventoried at intake but
carry no statistics here (reported as skipped, never failing); real
NVCL core imagery arrives as TSG packages and must be exported to PNG
before this stage (see README).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import codec

#: Bins per colour channel for the histograms.
N_BINS = 8

#: Bin width over the 0..255 range.
_BIN_WIDTH = 256 // N_BINS

#: Row groups per image for the strip summaries.
N_STRIPS = 4


@dataclass
class ImageStats:
    """Deterministic statistics for one PNG image."""

    name: str
    sha256: str
    width: int
    height: int
    histograms: dict[str, list[int]]
    means: dict[str, float]
    texture_h: dict[str, float]
    texture_v: dict[str, float]
    strips: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "histograms": {
                channel: list(self.histograms[channel])
                for channel in ("r", "g", "b")
            },
            "means": dict(self.means),
            "texture_h": dict(self.texture_h),
            "texture_v": dict(self.texture_v),
            "strips": [dict(strip) for strip in self.strips],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImageStats":
        return cls(
            name=str(data["name"]),
            sha256=str(data["sha256"]),
            width=int(data["width"]),
            height=int(data["height"]),
            histograms={
                channel: [int(v) for v in data["histograms"][channel]]
                for channel in ("r", "g", "b")
            },
            means={channel: float(data["means"][channel]) for channel in "rgb"},
            texture_h={
                channel: float(data["texture_h"][channel]) for channel in "rgb"
            },
            texture_v={
                channel: float(data["texture_v"][channel]) for channel in "rgb"
            },
            strips=[dict(strip) for strip in data.get("strips", [])],
        )


def _bin(value: int) -> int:
    """Histogram bin for one 0..255 channel value."""
    return min(value // _BIN_WIDTH, N_BINS - 1)


def compute_statistics(
    payload: bytes, name: str, media_type: str = "image/png"
) -> ImageStats:
    """Compute :class:`ImageStats` for one PNG payload (code only)."""
    if media_type != "image/png":
        raise ValueError(
            f"statistics support PNG only, got {media_type} for {name}"
        )
    width, height, rows = codec.decode_png(payload)
    histograms = {channel: [0] * N_BINS for channel in "rgb"}
    totals = {"r": 0, "g": 0, "b": 0}
    for row in rows:
        for red, green, blue in row:
            histograms["r"][_bin(red)] += 1
            histograms["g"][_bin(green)] += 1
            histograms["b"][_bin(blue)] += 1
            totals["r"] += red
            totals["g"] += green
            totals["b"] += blue
    n_pixels = width * height
    means = {channel: totals[channel] / n_pixels for channel in "rgb"}
    # Strip steps are structure (captured by the strip means below), not
    # texture: vertical pairs crossing a strip boundary are excluded so
    # the texture measures within-strip variation only.
    boundaries = {
        ((index + 1) * height) // N_STRIPS for index in range(N_STRIPS - 1)
    }
    horizontal = {"r": 0, "g": 0, "b": 0}
    for row in rows:
        for left, right in zip(row, row[1:]):
            for channel, first, second in zip("rgb", left, right):
                horizontal[channel] += abs(first - second)
    vertical = {"r": 0, "g": 0, "b": 0}
    v_count = 0
    for row_index, (upper, lower) in enumerate(zip(rows, rows[1:])):
        if row_index + 1 in boundaries:
            continue
        v_count += width
        for first, second in zip(upper, lower):
            for channel, one, two in zip("rgb", first, second):
                vertical[channel] += abs(one - two)
    h_count = height * max(width - 1, 1)
    v_count = v_count or 1
    texture_h = {channel: horizontal[channel] / h_count for channel in "rgb"}
    texture_v = {channel: vertical[channel] / v_count for channel in "rgb"}
    strips: list[dict[str, Any]] = []
    for index in range(N_STRIPS):
        row_lo = (index * height) // N_STRIPS
        row_hi = ((index + 1) * height) // N_STRIPS
        strip_pixels = [
            pixel for row in rows[row_lo:row_hi] for pixel in row
        ]
        count = len(strip_pixels) or 1
        strips.append(
            {
                "index": index,
                "row_lo": row_lo,
                "row_hi": row_hi,
                "n_rows": row_hi - row_lo,
                "mean_r": sum(p[0] for p in strip_pixels) / count,
                "mean_g": sum(p[1] for p in strip_pixels) / count,
                "mean_b": sum(p[2] for p in strip_pixels) / count,
            }
        )
    return ImageStats(
        name=name,
        sha256=hashlib.sha256(payload).hexdigest(),
        width=width,
        height=height,
        histograms=histograms,
        means=means,
        texture_h=texture_h,
        texture_v=texture_v,
        strips=strips,
    )


def summarize_for_prompt(stats: ImageStats) -> dict[str, Any]:
    """Round statistics to a JSON-safe summary (the only image-derived
    content a model ever sees; raw pixels never leave this module)."""
    summary = stats.to_dict()
    summary["means"] = {k: round(v, 3) for k, v in stats.means.items()}
    summary["texture_h"] = {k: round(v, 3) for k, v in stats.texture_h.items()}
    summary["texture_v"] = {k: round(v, 3) for k, v in stats.texture_v.items()}
    summary["strips"] = [
        {
            "index": strip["index"],
            "row_lo": strip["row_lo"],
            "row_hi": strip["row_hi"],
            "mean_r": round(strip["mean_r"], 3),
            "mean_g": round(strip["mean_g"], 3),
            "mean_b": round(strip["mean_b"], 3),
        }
        for strip in stats.strips
    ]
    return summary


def read_statistics(images_dir: str | Path) -> tuple[list[ImageStats], list[str]]:
    """Compute statistics for every PNG under ``images_dir``.

    Returns ``(stats, skipped)`` where ``skipped`` names the JPEG (or
    otherwise non-PNG) files that intake inventoried but that carry no
    statistics. Raises :class:`ValueError` when the directory is missing.
    """
    from .intake import inventory_images

    root = Path(images_dir)
    if not root.is_dir():
        raise ValueError(f"images directory not found: {root}")
    stats: list[ImageStats] = []
    skipped: list[str] = []
    for record in inventory_images(root):
        if record.media_type != "image/png":
            skipped.append(record.name)
            continue
        stats.append(
            compute_statistics(
                (root / record.name).read_bytes(),
                record.name,
                record.media_type,
            )
        )
    return stats, skipped
