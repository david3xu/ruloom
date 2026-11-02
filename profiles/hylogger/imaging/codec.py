"""Minimal PNG codec (standard library only).

The encoder writes one deterministic PNG flavour: 8-bit truecolour
(colour type 2), non-interlaced, one IDAT chunk, filter byte 0 on every
scanline. The decoder reads that flavour plus its close equivalents as
produced by standard encoders: 8-bit, non-interlaced, colour types 0
(greyscale), 2 (RGB) and 6 (RGBA); all five filter types are unfiltered.
Anything else (other bit depths, interlace, unknown colour types, a
truncated stream) raises :class:`ValueError`.

Fixture imagery for tests is produced by :func:`encode_png`, so no binary
image blob is ever committed; real NVCL core imagery is documented as the
download source in the README (availability unverified locally).
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_IHDR = b"IHDR"
_IDAT = b"IDAT"
_IEND = b"IEND"


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, type, payload, CRC over type+payload."""
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def encode_png(
    width: int, height: int, rows: list[list[tuple[int, int, int]]]
) -> bytes:
    """Encode RGB rows to deterministic PNG bytes.

    Same ``(width, height, rows)`` always yields identical bytes: the
    header is fixed, scanlines use filter 0, and compression has no
    timestamp or encoder tag to vary.
    """
    if not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("width and height must be integers")
    if width < 1 or height < 1:
        raise ValueError(f"dimensions must be positive, got {width}x{height}")
    if len(rows) != height:
        raise ValueError(f"expected {height} rows, got {len(rows)}")
    raw = bytearray()
    for index, row in enumerate(rows):
        if len(row) != width:
            raise ValueError(f"row {index}: expected {width} pixels, got {len(row)}")
        raw.append(0)
        for pixel in row:
            red, green, blue = pixel
            for value in (red, green, blue):
                if not isinstance(value, int) or value < 0 or value > 255:
                    raise ValueError(
                        f"pixel values must be ints in 0..255, got {pixel!r}"
                    )
            raw += bytes((red, green, blue))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(_IHDR, header)
        + _chunk(_IDAT, zlib.compress(bytes(raw)))
        + _chunk(_IEND, b"")
    )


def _paeth(a: int, b: int, c: int) -> int:
    """PNG Paeth predictor over the three neighbouring bytes."""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(filter_type: int, scanline: bytes, previous: bytes, stride: int) -> bytes:
    """Reverse one PNG scanline filter; all five types are supported."""
    current = bytearray(scanline)
    if filter_type == 0:
        return bytes(current)
    if filter_type == 1:
        for i in range(stride, len(current)):
            current[i] = (current[i] + current[i - stride]) & 0xFF
        return bytes(current)
    if filter_type == 2:
        for i in range(len(current)):
            current[i] = (current[i] + previous[i]) & 0xFF
        return bytes(current)
    if filter_type == 3:
        for i in range(len(current)):
            left = current[i - stride] if i >= stride else 0
            current[i] = (current[i] + ((left + previous[i]) >> 1)) & 0xFF
        return bytes(current)
    if filter_type == 4:
        for i in range(len(current)):
            left = current[i - stride] if i >= stride else 0
            upper = previous[i]
            upper_left = previous[i - stride] if i >= stride else 0
            current[i] = (current[i] + _paeth(left, upper, upper_left)) & 0xFF
        return bytes(current)
    raise ValueError(f"unknown PNG filter type {filter_type}")


def decode_png(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """Decode PNG bytes to ``(width, height, rows)`` of RGB tuples.

    Accepts the documented subset only (8-bit, non-interlaced, colour
    types 0/2/6); alpha channels are dropped to RGB. Raises
    :class:`ValueError` on anything outside that subset.
    """
    if not isinstance(data, (bytes, bytearray)) or bytes(data[:8]) != PNG_SIGNATURE:
        raise ValueError("not a PNG stream (bad signature)")
    offset = 8
    width = height = 0
    colour_type = -1
    seen_header = False
    idat_parts: list[bytes] = []
    ended = False
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("truncated PNG chunk header")
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunk_type = bytes(data[offset + 4 : offset + 8])
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            raise ValueError("truncated PNG chunk payload")
        payload = bytes(data[start:end])
        offset = end + 4
        if chunk_type == _IHDR:
            if seen_header:
                raise ValueError("duplicate IHDR chunk")
            if length != 13:
                raise ValueError("malformed IHDR chunk")
            width, height, bit_depth, colour_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if bit_depth != 8:
                raise ValueError(f"unsupported PNG bit depth {bit_depth} (8-bit only)")
            if colour_type not in (0, 2, 6):
                raise ValueError(
                    f"unsupported PNG colour type {colour_type} "
                    "(greyscale/RGB/RGBA only)"
                )
            if interlace != 0:
                raise ValueError("interlaced PNG is not supported")
            if width < 1 or height < 1:
                raise ValueError("PNG dimensions must be positive")
            seen_header = True
        elif chunk_type == _IDAT:
            if not seen_header:
                raise ValueError("IDAT chunk before IHDR")
            idat_parts.append(payload)
        elif chunk_type == _IEND:
            ended = True
            break
    if not seen_header:
        raise ValueError("PNG stream has no IHDR chunk")
    if not ended:
        raise ValueError("PNG stream has no IEND chunk")
    try:
        raw = zlib.decompress(b"".join(idat_parts))
    except zlib.error as exc:
        raise ValueError(f"PNG IDAT stream does not decompress: {exc}") from exc
    channels = {0: 1, 2: 3, 6: 4}[colour_type]
    stride = width * channels
    rows: list[list[tuple[int, int, int]]] = []
    previous = bytes(stride)
    cursor = 0
    for _ in range(height):
        if cursor + 1 + stride > len(raw):
            raise ValueError("PNG scanline data is short for its dimensions")
        filter_type = raw[cursor]
        cursor += 1
        scanline = _unfilter(
            filter_type, bytes(raw[cursor : cursor + stride]), previous, channels
        )
        cursor += stride
        previous = scanline
        row: list[tuple[int, int, int]] = []
        for pixel in range(width):
            base = pixel * channels
            if channels == 1:
                grey = scanline[base]
                row.append((grey, grey, grey))
            elif channels == 3:
                row.append((scanline[base], scanline[base + 1], scanline[base + 2]))
            else:
                row.append((scanline[base], scanline[base + 1], scanline[base + 2]))
        rows.append(row)
    return width, height, rows
