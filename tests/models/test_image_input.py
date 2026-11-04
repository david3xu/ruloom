"""Tests for generic image reading and PNG validation in models/image_input.py.

Verifies bounded reading, admitted 8-bit non-interlaced PNG subset enforcement,
scanline filter byte bounds (0..4), critical chunk rejection, contiguous IDATs,
decompression bomb mitigation, trailing data rejection, O_NOFOLLOW security,
and capability enforcement.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

from models.base import ModelCapabilities, ModelError
from models.image_input import (
    PNG_MAGIC,
    PNG_MEDIA_TYPE,
    read_validated_image,
    validate_image_bytes,
)

DEFAULT_CAPS = ModelCapabilities(
    input_modalities=("text", "image"),
    structured_output=True,
    image_media_types=("image/png",),
    max_image_bytes=4 * 1024 * 1024,
    max_image_pixels=4_000_000,
)


def _make_valid_png(width: int = 2, height: int = 2, filter_byte: int = 0) -> bytes:
    """Construct minimal valid 8-bit RGBA non-interlaced PNG bytes."""
    magic = PNG_MAGIC
    # IHDR: 8-bit (depth 8), RGBA (colour_type 6), compression 0, filter 0, interlace 0
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)

    # IDAT: RGBA is 4 bytes/pixel, 1 filter byte per row
    raw_scanlines = b"".join(
        bytes([filter_byte]) + (b"\xff\x00\x00\xff" * width) for _ in range(height)
    )
    compressed = zlib.compress(raw_scanlines)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = (
        struct.pack(">I4s", len(compressed), b"IDAT")
        + compressed
        + struct.pack(">I", idat_crc)
    )

    # IEND
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)

    return magic + ihdr + idat + iend


def test_read_valid_png(tmp_path: Path) -> None:
    path = tmp_path / "valid.png"
    png_bytes = _make_valid_png(width=4, height=3)
    path.write_bytes(png_bytes)

    payload, media_type, width, height = read_validated_image(path, DEFAULT_CAPS)
    assert payload == png_bytes
    assert media_type == PNG_MEDIA_TYPE
    assert width == 4
    assert height == 3


def test_validate_image_bytes_valid() -> None:
    png_bytes = _make_valid_png(width=1, height=1)
    payload, media_type, width, height = validate_image_bytes(png_bytes, DEFAULT_CAPS)
    assert payload == png_bytes
    assert media_type == "image/png"
    assert width == 1
    assert height == 1


def test_read_png_exceeds_max_bytes(tmp_path: Path) -> None:
    path = tmp_path / "too_big.png"
    path.write_bytes(_make_valid_png(2, 2))
    tiny_caps = ModelCapabilities(
        input_modalities=("text", "image"),
        image_media_types=("image/png",),
        max_image_bytes=20,
        max_image_pixels=1000,
    )
    with pytest.raises(ModelError, match="exceeds capability limit"):
        read_validated_image(path, tiny_caps)


def test_read_png_exceeds_max_pixels(tmp_path: Path) -> None:
    path = tmp_path / "huge_dim.png"
    path.write_bytes(_make_valid_png(20, 20))  # 400 pixels
    small_pixel_caps = ModelCapabilities(
        input_modalities=("text", "image"),
        image_media_types=("image/png",),
        max_image_bytes=100_000,
        max_image_pixels=50,  # 50 pixel limit
    )
    with pytest.raises(ModelError, match="pixel count.*exceeds capability limit"):
        read_validated_image(path, small_pixel_caps)


def test_png_zero_dimensions_rejected() -> None:
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 0, 5, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    bad_png = magic + ihdr + iend

    with pytest.raises(ModelError, match="invalid PNG dimensions"):
        validate_image_bytes(bad_png, DEFAULT_CAPS)


def test_png_invalid_magic() -> None:
    bad_bytes = b"NOT_A_PNG_FILE" + b"\x00" * 40
    with pytest.raises(ModelError, match="invalid PNG magic header"):
        validate_image_bytes(bad_bytes, DEFAULT_CAPS)


def test_png_missing_ihdr() -> None:
    magic = PNG_MAGIC
    data = b"dummy" * 10
    crc = zlib.crc32(b"IDAT" + data) & 0xFFFFFFFF
    chunk = struct.pack(">I4s", len(data), b"IDAT") + data + struct.pack(">I", crc)
    with pytest.raises(ModelError, match="first PNG chunk must be IHDR"):
        validate_image_bytes(magic + chunk, DEFAULT_CAPS)


def test_png_chunk_crc_mismatch() -> None:
    valid = _make_valid_png(2, 2)
    corrupted = valid[:16] + bytes([valid[16] ^ 0xFF]) + valid[17:]
    with pytest.raises(ModelError, match="CRC mismatch"):
        validate_image_bytes(corrupted, DEFAULT_CAPS)


def test_png_missing_iend() -> None:
    valid = _make_valid_png(2, 2)
    no_iend = valid[:-12]
    with pytest.raises(ModelError, match="PNG missing IEND chunk"):
        validate_image_bytes(no_iend, DEFAULT_CAPS)


def test_png_trailing_data_rejected() -> None:
    valid = _make_valid_png(2, 2)
    with_trailing = valid + b"EXTRA_UNEXPECTED_BYTES"
    with pytest.raises(ModelError, match="trailing unexpected data"):
        validate_image_bytes(with_trailing, DEFAULT_CAPS)


# --- Adversarial Subset & Decompression Tests ---


def test_png_indexed_palette_rejected() -> None:
    # Colour type 3 (indexed-colour) must be explicitly rejected
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    palette_png = magic + ihdr + iend

    with pytest.raises(ModelError, match="unsupported PNG colour type 3"):
        validate_image_bytes(palette_png, DEFAULT_CAPS)


def test_png_non_8bit_depth_rejected() -> None:
    # 16-bit depth (depth 16) must be rejected
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 2, 2, 16, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    bad_depth_png = magic + ihdr + iend

    with pytest.raises(ModelError, match="unsupported PNG bit depth 16"):
        validate_image_bytes(bad_depth_png, DEFAULT_CAPS)


def test_png_adam7_interlacing_rejected() -> None:
    # Interlace method 1 (Adam7) must be rejected
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 1)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    adam7_png = magic + ihdr + iend

    with pytest.raises(ModelError, match="unsupported PNG interlace method"):
        validate_image_bytes(adam7_png, DEFAULT_CAPS)


def test_png_unknown_critical_chunk_rejected() -> None:
    # Critical chunk 'CUST' (uppercase first letter) is not IHDR, IDAT, or IEND
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)

    cust_data = b"custom-critical-data"
    cust_crc = zlib.crc32(b"CUST" + cust_data) & 0xFFFFFFFF
    cust = (
        struct.pack(">I4s", len(cust_data), b"CUST")
        + cust_data
        + struct.pack(">I", cust_crc)
    )

    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    critical_png = magic + ihdr + cust + iend

    with pytest.raises(ModelError, match="unsupported critical PNG chunk 'CUST'"):
        validate_image_bytes(critical_png, DEFAULT_CAPS)


def test_png_illegal_chunk_name_rejected() -> None:
    # Chunk name with non-ASCII letter (e.g. 0x00)
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)

    bad_name = b"ID\x00T"
    bad_data = b"some-data"
    bad_crc = zlib.crc32(bad_name + bad_data) & 0xFFFFFFFF
    bad_chunk = (
        struct.pack(">I4s", len(bad_data), bad_name)
        + bad_data
        + struct.pack(">I", bad_crc)
    )
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    illegal_png = magic + ihdr + bad_chunk + iend

    with pytest.raises(ModelError, match="illegal PNG chunk name"):
        validate_image_bytes(illegal_png, DEFAULT_CAPS)


def test_png_noncontiguous_idat_rejected() -> None:
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)

    idat1_data = b"chunk1"
    idat1_crc = zlib.crc32(b"IDAT" + idat1_data) & 0xFFFFFFFF
    idat1 = (
        struct.pack(">I4s", len(idat1_data), b"IDAT")
        + idat1_data
        + struct.pack(">I", idat1_crc)
    )

    # Ancillary chunk text in between IDAT chunks
    text_data = b"comment"
    text_crc = zlib.crc32(b"tEXt" + text_data) & 0xFFFFFFFF
    text_chunk = (
        struct.pack(">I4s", len(text_data), b"tEXt")
        + text_data
        + struct.pack(">I", text_crc)
    )

    idat2_data = b"chunk2"
    idat2_crc = zlib.crc32(b"IDAT" + idat2_data) & 0xFFFFFFFF
    idat2 = (
        struct.pack(">I4s", len(idat2_data), b"IDAT")
        + idat2_data
        + struct.pack(">I", idat2_crc)
    )

    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    noncontig_png = magic + ihdr + idat1 + text_chunk + idat2 + iend

    with pytest.raises(ModelError, match="noncontiguous IDAT chunks in PNG"):
        validate_image_bytes(noncontig_png, DEFAULT_CAPS)


def test_png_invalid_scanline_filter_byte_rejected() -> None:
    # Filter byte 5 is invalid (only 0..4 permitted)
    bad_filter_png = _make_valid_png(width=2, height=2, filter_byte=5)
    with pytest.raises(ModelError, match="invalid PNG scanline filter byte 5"):
        validate_image_bytes(bad_filter_png, DEFAULT_CAPS)


def test_png_decompression_bomb_rejected() -> None:
    # Header says 1x1 (raw 5 bytes), but IDAT carries decompressed data for 50x50
    magic = PNG_MAGIC
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)

    bomb_raw = b"".join(b"\x00" + (b"\xff\x00\x00\xff" * 50) for _ in range(50))
    compressed = zlib.compress(bomb_raw)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = (
        struct.pack(">I4s", len(compressed), b"IDAT")
        + compressed
        + struct.pack(">I", idat_crc)
    )

    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    bomb_png = magic + ihdr + idat + iend

    with pytest.raises(ModelError, match="PNG decompression bomb detected"):
        validate_image_bytes(bomb_png, DEFAULT_CAPS)


def test_symlink_refused_by_nofollow(tmp_path: Path) -> None:
    real_png = tmp_path / "real.png"
    real_png.write_bytes(_make_valid_png(1, 1))

    symlink_path = tmp_path / "link.png"
    try:
        symlink_path.symlink_to(real_png)
    except OSError:
        pytest.skip("Symlinks not supported on this filesystem")

    # If O_NOFOLLOW is supported, open fails on symlink
    if hasattr(os, "O_NOFOLLOW"):
        with pytest.raises(ModelError):
            read_validated_image(symlink_path, DEFAULT_CAPS)


@pytest.mark.parametrize("fault", ["truncated", "unused"])
def test_png_compressed_stream_must_end_exactly(fault):
    png = _make_valid_png()
    length = int.from_bytes(png[33:37], "big")
    stream = png[41 : 41 + length]
    stream = stream[:-2] if fault == "truncated" else stream + b"extra compressed data"
    idat = (
        struct.pack(">I", len(stream))
        + b"IDAT"
        + stream
        + struct.pack(">I", zlib.crc32(b"IDAT" + stream) & 0xFFFFFFFF)
    )
    broken = png[:33] + idat + png[-12:]
    with pytest.raises(ModelError, match=fault):
        validate_image_bytes(broken, DEFAULT_CAPS)


def test_directory_is_not_an_image(tmp_path):
    with pytest.raises(ModelError, match="not a regular file"):
        read_validated_image(tmp_path, DEFAULT_CAPS)


def test_file_growth_cannot_exceed_bounded_descriptor_read(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    path = tmp_path / "growing.png"
    path.write_bytes(_make_valid_png())
    original = os.fstat

    def apparent_empty_file(fd):
        metadata = original(fd)
        return SimpleNamespace(st_mode=metadata.st_mode, st_size=0)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fstat", apparent_empty_file)
        with pytest.raises(ModelError, match="exceeds capability limit"):
            read_validated_image(path, replace(DEFAULT_CAPS, max_image_bytes=20))
