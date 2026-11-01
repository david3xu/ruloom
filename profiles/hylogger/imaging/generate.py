"""Seeded synthetic image generation fitted to statistics.

A synthetic core-strip image is rendered by seeded code only: no
image-generation model exists anywhere in this repository. Given one
:class:`~statistics.ImageStats` (as a plain dict, so the engine state
stays JSON-safe) and an integer seed, the generator paints each strip
with its measured mean colour plus bounded uniform jitter scaled by the
measured texture. Same statistics + same seed always yields identical
PNG bytes (see :func:`synthesize`), which the determinism test pins by
SHA-256.
"""

from __future__ import annotations

import random
from typing import Any, Mapping

from . import codec
from .statistics import N_STRIPS, ImageStats


def make_fixture_png(
    width: int, height: int, strip_colours: list[tuple[int, int, int]]
) -> bytes:
    """Build a small deterministic PNG: one solid colour per strip.

    Tests use this instead of committing binary blobs. ``strip_colours``
    gives one RGB colour per strip (``N_STRIPS`` entries); each strip
    covers a contiguous row band.
    """
    if len(strip_colours) != N_STRIPS:
        raise ValueError(
            f"need {N_STRIPS} strip colours, got {len(strip_colours)}"
        )
    rows: list[list[tuple[int, int, int]]] = []
    for row_index in range(height):
        strip = min((row_index * N_STRIPS) // height, N_STRIPS - 1)
        rows.append([strip_colours[strip]] * width)
    return codec.encode_png(width, height, rows)


def _spread_for(stats: Mapping[str, Any]) -> float:
    """Jitter half-width from the measured texture (bounded, code-only)."""
    texture_h = stats.get("texture_h", {})
    texture_v = stats.get("texture_v", {})
    overall = 0.0
    count = 0
    for table in (texture_h, texture_v):
        if isinstance(table, Mapping):
            for value in table.values():
                if isinstance(value, (int, float)) and not isinstance(
                    value, bool
                ):
                    overall += float(value)
                    count += 1
    average = overall / count if count else 0.0
    return max(1.0, min(2.0 * average, 12.0))


def synthesize(stats: Mapping[str, Any], seed: int) -> bytes:
    """Render a synthetic PNG fitted to ``stats`` under ``seed``.

    ``stats`` is an :class:`ImageStats` serialisation (see
    :meth:`ImageStats.to_dict`). Pixel ``(x, y)`` takes its strip's mean
    colour plus uniform jitter in ``[-spread, +spread]`` drawn from
    ``random.Random(seed)`` in row-major order, clamped to 0..255.
    """
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError(f"seed must be an integer, got {seed!r}")
    width = int(stats["width"])
    height = int(stats["height"])
    strips = list(stats["strips"])
    if len(strips) != N_STRIPS:
        raise ValueError(
            f"statistics carry {len(strips)} strips, need {N_STRIPS}"
        )
    spread = _spread_for(stats)
    rng = random.Random(seed)
    rows: list[list[tuple[int, int, int]]] = []
    for row_index in range(height):
        strip_index = min((row_index * N_STRIPS) // height, N_STRIPS - 1)
        strip = strips[strip_index]
        base = (
            float(strip["mean_r"]),
            float(strip["mean_g"]),
            float(strip["mean_b"]),
        )
        row: list[tuple[int, int, int]] = []
        for _ in range(width):
            pixel = []
            for channel in base:
                value = channel + rng.uniform(-spread, spread)
                pixel.append(max(0, min(255, int(round(value)))))
            row.append((pixel[0], pixel[1], pixel[2]))
        rows.append(row)
    return codec.encode_png(width, height, rows)


def synthesize_from_stats(stats: ImageStats, seed: int) -> bytes:
    """Render from an :class:`ImageStats` object (same bytes as
    :func:`synthesize` over its serialisation)."""
    return synthesize(stats.to_dict(), seed)
