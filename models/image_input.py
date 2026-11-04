"""Generic validated image reading and format verification for Ruloom.

Admitted attachment PNG subset:
- Strictly 8-bit non-interlaced grayscale (type 0), RGB (type 2), grayscale+alpha (type 4),
  or RGBA (type 6).
- Interlace method must be 0 (no Adam7).
- Compression method must be 0 (deflate).
- Filter method must be 0 (standard adaptive filtering).
- Scanline filter bytes must be strictly within 0..4 (None, Sub, Up, Average, Paeth).
- Indexed-colour / palette (type 3), non-8-bit depths (1, 2, 4, 16), and Adam7 interlacing
  are explicitly rejected.
- Chunk names must consist of 4 ASCII letters.
- Unknown critical chunks (uppercase first letter other than IHDR, IDAT, IEND) are rejected.
- IDAT chunks must be contiguous.
- Bounded zlib decompression enforces exact decoded length without unbounded flush,
  and rejects decompression bombs, unconsumed tail data, truncation, or trailing unused data.
- File access uses O_NOFOLLOW and descriptor-bounded reads; the engine separately
  verifies provenance and stages the registered payload. This is not OS confinement.
"""

from __future__ import annotations

import os
import stat
import struct
import zlib
from pathlib import Path

from .base import ModelCapabilities, ModelError

#: Standard PNG magic signature (8 bytes).
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Media type for PNG imagery.
PNG_MEDIA_TYPE = "image/png"

#: Admitted colour types for 8-bit non-interlaced PNG imagery.
#: Maps colour_type -> number of channels (bytes per pixel at bit depth 8).
_ADMITTED_COLOUR_TYPES: dict[int, int] = {
    0: 1,  # 8-bit Grayscale
    2: 3,  # 8-bit RGB
    4: 2,  # 8-bit Grayscale with alpha
    6: 4,  # 8-bit RGBA
}

#: Known critical chunk identifiers admitted in this subset.
_ADMITTED_CRITICAL_CHUNKS = frozenset({b"IHDR", b"IDAT", b"IEND"})


def _is_ascii_letter(byte_val: int) -> bool:
    """True when byte_val is an ASCII uppercase or lowercase letter."""
    return (65 <= byte_val <= 90) or (97 <= byte_val <= 122)


def validate_image_bytes(
    data: bytes,
    capabilities: ModelCapabilities,
) -> tuple[bytes, str, int, int]:
    """Validate in-memory PNG image bytes against model capabilities and admitted subset.

    Validates PNG magic, IHDR parameters, chunk layout, CRCs, dimension bounds,
    byte limits, contiguous IDAT chunks, known critical chunks, bounded decompression,
    and row filter bytes.

    Returns ``(payload, media_type, width, height)``.
    Raises :class:`ModelError` on any violation.
    """
    if not capabilities.structured_output:
        raise ModelError(
            "image validation requires capabilities.structured_output True"
        )
    if not capabilities.supports_image:
        raise ModelError(
            f"model capabilities do not support image input "
            f"(modalities={capabilities.input_modalities}, types={capabilities.image_media_types})"
        )

    limit_bytes = capabilities.max_image_bytes
    if len(data) > limit_bytes:
        raise ModelError(
            f"image payload ({len(data)} bytes) exceeds capability limit ({limit_bytes} bytes)"
        )
    if len(data) < 45:
        # Minimum valid PNG: magic (8) + IHDR (25) + IDAT (at least 12) + IEND (12)
        raise ModelError(f"image payload too short for a valid PNG ({len(data)} bytes)")

    if data[:8] != PNG_MAGIC:
        raise ModelError("invalid PNG magic header")

    offset = 8
    total_len = len(data)
    ihdr_seen = False
    iend_seen = False
    idat_started = False
    idat_finished = False
    idat_chunks: list[bytes] = []

    width = 0
    height = 0
    colour_type = 0

    while offset < total_len:
        if offset + 8 > total_len:
            raise ModelError("truncated PNG chunk header")

        length, chunk_type = struct.unpack(">I4s", data[offset : offset + 8])
        if length > 0x7FFFFFFF:
            raise ModelError("invalid PNG chunk length exceeding 2GB limit")

        # Legal chunk name validation: 4 ASCII letters
        if not all(_is_ascii_letter(b) for b in chunk_type):
            raise ModelError(
                f"illegal PNG chunk name {chunk_type!r}: must consist of 4 ASCII letters"
            )
        if chunk_type[2] & 0x20:
            raise ModelError("invalid reserved bit in PNG chunk name")

        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4

        if crc_end > total_len:
            raise ModelError(
                f"PNG chunk {chunk_type.decode('ascii', 'replace')!r} extends beyond payload (truncated)"
            )

        chunk_data = data[data_start:data_end]
        (expected_crc,) = struct.unpack(">I", data[data_end:crc_end])

        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ModelError(
                f"PNG chunk {chunk_type.decode('ascii', 'replace')!r} CRC mismatch: "
                f"expected {expected_crc:#010x}, calculated {actual_crc:#010x}"
            )

        # Check for unknown critical chunks (bit 5 of first byte is 0 -> uppercase)
        is_critical = (chunk_type[0] & 0x20) == 0
        if is_critical and chunk_type not in _ADMITTED_CRITICAL_CHUNKS:
            raise ModelError(
                f"unsupported critical PNG chunk {chunk_type.decode('ascii', 'replace')!r}; "
                f"admitted subset permits IHDR, IDAT, and IEND only"
            )

        if not ihdr_seen:
            if chunk_type != b"IHDR":
                raise ModelError("first PNG chunk must be IHDR")
            if length != 13:
                raise ModelError(f"IHDR chunk must be exactly 13 bytes, got {length}")

            (
                width,
                height,
                bit_depth,
                colour_type,
                comp,
                filt,
                interlace_method,
            ) = struct.unpack(">IIBBBBB", chunk_data)

            if width <= 0 or height <= 0:
                raise ModelError(f"invalid PNG dimensions ({width}x{height})")

            pixels = width * height
            if pixels > capabilities.max_image_pixels:
                raise ModelError(
                    f"PNG pixel count ({pixels}) exceeds capability limit ({capabilities.max_image_pixels})"
                )

            # Restrict to admitted subset: 8-bit non-interlaced, compression 0, filter 0
            if bit_depth != 8:
                raise ModelError(
                    f"unsupported PNG bit depth {bit_depth}; admitted subset requires 8-bit depth"
                )
            if colour_type not in _ADMITTED_COLOUR_TYPES:
                raise ModelError(
                    f"unsupported PNG colour type {colour_type}; admitted subset permits 8-bit "
                    f"grayscale (0), RGB (2), grayscale+alpha (4), and RGBA (6) only (indexed palette rejected)"
                )
            if comp != 0:
                raise ModelError(
                    f"unsupported PNG compression method ({comp}); must be 0"
                )
            if filt != 0:
                raise ModelError(f"unsupported PNG filter method ({filt}); must be 0")
            if interlace_method != 0:
                raise ModelError(
                    f"unsupported PNG interlace method ({interlace_method}); admitted subset requires 0 (no Adam7)"
                )

            ihdr_seen = True
        else:
            if chunk_type == b"IHDR":
                raise ModelError("duplicate IHDR chunk in PNG")
            elif chunk_type == b"IDAT":
                if iend_seen:
                    raise ModelError("unexpected IDAT chunk after IEND")
                if idat_finished:
                    raise ModelError("noncontiguous IDAT chunks in PNG")
                idat_started = True
                idat_chunks.append(chunk_data)
            elif chunk_type == b"IEND":
                if length != 0:
                    raise ModelError(f"IEND chunk must have length 0, got {length}")
                if idat_started:
                    idat_finished = True
                iend_seen = True
                offset = crc_end
                if offset != total_len:
                    raise ModelError(
                        f"trailing unexpected data after PNG IEND chunk ({total_len - offset} extra bytes)"
                    )
                break
            else:
                # Ancillary chunk
                if idat_started:
                    idat_finished = True

        offset = crc_end

    if not iend_seen:
        raise ModelError("PNG missing IEND chunk (truncated)")
    if not idat_chunks:
        raise ModelError("PNG contains no IDAT image data chunks")

    # Bounded decompression and scanline filter verification
    channels = _ADMITTED_COLOUR_TYPES[colour_type]
    row_bytes = width * channels
    row_stride = 1 + row_bytes
    expected_raw_bytes = height * row_stride

    compressed_stream = b"".join(idat_chunks)
    decompressor = zlib.decompressobj()
    try:
        # Bounded decompress without unbounded flush
        decompressed = decompressor.decompress(
            compressed_stream, max_length=expected_raw_bytes + 1
        )
    except zlib.error as exc:
        raise ModelError(f"malformed PNG zlib data in IDAT chunks: {exc}") from exc

    if len(decompressed) > expected_raw_bytes:
        raise ModelError(
            f"PNG decompression bomb detected: output exceeded expected {expected_raw_bytes} bytes"
        )
    if len(decompressed) != expected_raw_bytes:
        raise ModelError(
            f"PNG decompressed raw data size mismatch: got {len(decompressed)}, expected {expected_raw_bytes}"
        )
    if decompressor.unconsumed_tail:
        raise ModelError("PNG IDAT contains unconsumed compressed stream data")
    if not decompressor.eof:
        raise ModelError("PNG IDAT compressed stream is truncated (EOF not reached)")
    if decompressor.unused_data:
        raise ModelError("unexpected trailing unused compressed data in IDAT stream")

    # Validate scanline filter bytes: each row starts with a filter byte in 0..4
    for row_idx in range(height):
        filter_byte = decompressed[row_idx * row_stride]
        if filter_byte < 0 or filter_byte > 4:
            raise ModelError(
                f"invalid PNG scanline filter byte {filter_byte} at row {row_idx} (must be 0..4)"
            )

    return data, PNG_MEDIA_TYPE, width, height


def read_validated_image(
    image_path: str | Path,
    capabilities: ModelCapabilities,
) -> tuple[bytes, str, int, int]:
    """Read an image file bounded before allocation and validate format and boundaries.

    Uses O_NOFOLLOW for the final path component and fstat for a bounded
    regular-file descriptor read. The engine owns provenance checks and staging.

    Returns ``(payload, media_type, width, height)``.
    Raises :class:`ModelError` on missing file, read errors, or validation failures.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        fd = os.open(str(image_path), flags)
    except FileNotFoundError:
        raise ModelError(f"image file not found: {image_path}") from None
    except OSError as exc:
        raise ModelError(f"cannot open image file {image_path}: {exc}") from exc

    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise ModelError(f"image path is not a regular file: {image_path}")

        limit_bytes = capabilities.max_image_bytes
        if stat_result.st_size > limit_bytes:
            raise ModelError(
                f"image file size ({stat_result.st_size} bytes) exceeds capability limit ({limit_bytes} bytes)"
            )

        # Bounded read loop
        chunks: list[bytes] = []
        total_read = 0
        while total_read <= limit_bytes:
            buf = os.read(fd, min(65536, limit_bytes - total_read + 1))
            if not buf:
                break
            chunks.append(buf)
            total_read += len(buf)

        if total_read > limit_bytes:
            raise ModelError(
                f"image payload ({total_read} bytes) exceeds capability limit ({limit_bytes} bytes)"
            )
        payload = b"".join(chunks)
    except OSError as exc:
        raise ModelError(f"error reading image file {image_path}: {exc}") from exc
    finally:
        os.close(fd)

    return validate_image_bytes(payload, capabilities)
