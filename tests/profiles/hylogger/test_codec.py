"""Tests for the stdlib PNG codec used by the image extension.

The encoder must be deterministic (same rows give identical bytes) and
round-trip through the decoder; the decoder must reject anything
outside the documented subset (bad signature, truncation, other bit
depths, interlace, unknown colour types).
"""

from __future__ import annotations

import struct
import zlib

import pytest

from profiles.hylogger.imaging import codec
from profiles.hylogger.imaging.codec import decode_png, encode_png

PIXELS = [
    [(200, 30, 30), (30, 200, 30), (30, 30, 200)],
    [(220, 220, 220), (10, 10, 10), (120, 60, 200)],
]


def test_round_trip_rgb() -> None:
    payload = encode_png(3, 2, PIXELS)
    width, height, rows = decode_png(payload)
    assert (width, height) == (3, 2)
    assert rows == PIXELS


def test_encoder_is_deterministic() -> None:
    first = encode_png(3, 2, PIXELS)
    second = encode_png(3, 2, [[tuple(p) for p in row] for row in PIXELS])
    assert first == second
    assert first[:8] == codec.PNG_SIGNATURE


def test_single_pixel_round_trip() -> None:
    payload = encode_png(1, 1, [[(1, 2, 3)]])
    assert decode_png(payload) == (1, 1, [[(1, 2, 3)]])


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def _filtered_png(width: int, height: int, raws: list[bytes]) -> bytes:
    """Build a PNG whose scanlines use caller-supplied filter bytes."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        codec.PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"".join(raws)))
        + _chunk(b"IEND", b"")
    )


def _encode_filtered(
    filter_type: int, width: int, values: list[list[tuple[int, int, int]]]
) -> bytes:
    """Encode rows under one PNG filter type (tests the decoder's
    unfiltering against every filter, not just the encoder's filter 0)."""
    channels = 3
    raws: list[bytes] = []
    previous = bytes(width * channels)
    for row in values:
        raw_row = b"".join(bytes(pixel) for pixel in row)
        current = bytearray(raw_row)
        if filter_type == 1:
            for i in range(channels, len(current)):
                current[i] = (raw_row[i] - raw_row[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(len(current)):
                current[i] = (raw_row[i] - previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(len(current)):
                left = raw_row[i - channels] if i >= channels else 0
                current[i] = (raw_row[i] - ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(len(current)):
                left = raw_row[i - channels] if i >= channels else 0
                upper = previous[i]
                upper_left = previous[i - channels] if i >= channels else 0
                base = left + upper - upper_left
                closest = min(
                    (abs(base - left), left),
                    (abs(base - upper), upper),
                    (abs(base - upper_left), upper_left),
                )[1]
                current[i] = (raw_row[i] - closest) & 0xFF
        raws.append(bytes([filter_type]) + bytes(current))
        previous = raw_row
    height = len(values)
    return _filtered_png(width, height, raws)


def test_all_filter_types_decode() -> None:
    rows = [
        [(200, 30, 30), (31, 201, 29), (28, 33, 199), (219, 221, 218)],
        [(11, 9, 12), (121, 59, 201), (44, 199, 91), (250, 239, 231)],
    ]
    for filter_type in (0, 1, 2, 3, 4):
        payload = _encode_filtered(filter_type, 4, rows)
        assert decode_png(payload) == (4, 2, rows)


def test_rejects_bad_signature() -> None:
    with pytest.raises(ValueError):
        decode_png(b"not a png at all........")


def test_rejects_truncated_stream() -> None:
    payload = encode_png(2, 2, [[(1, 2, 3), (4, 5, 6)], [(7, 8, 9), (1, 1, 1)]])
    with pytest.raises(ValueError):
        decode_png(payload[:20])


def test_rejects_missing_iend() -> None:
    payload = encode_png(1, 1, [[(1, 2, 3)]])
    with pytest.raises(ValueError):
        decode_png(payload[:-12])


def test_rejects_other_bit_depth() -> None:
    header = struct.pack(">IIBBBBB", 2, 2, 1, 0, 0, 0, 0)
    payload = (
        codec.PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00\x00"))
        + _chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError):
        decode_png(payload)


def test_rejects_interlaced() -> None:
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 1)
    payload = (
        codec.PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(13)))
        + _chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError):
        decode_png(payload)


def test_encode_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        encode_png(0, 2, [[(0, 0, 0)], [(0, 0, 0)]])
    with pytest.raises(ValueError):
        encode_png(2, 2, [[(0, 0, 0)]])
    with pytest.raises(ValueError):
        encode_png(1, 1, [[(256, 0, 0)]])
    with pytest.raises(ValueError):
        encode_png(1, 1, [[(0, 0, -1)]])
