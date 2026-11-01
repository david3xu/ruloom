"""Tests for image file intake (hashing and attribution).

Image files are source data: intake hashes every file, types it by
extension, and attributes it from its sibling manifest. No pixel is
decoded here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from profiles.hylogger.imaging.generate import make_fixture_png
from profiles.hylogger.imaging.intake import inventory_images

PALETTE = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]


def _write_images(root: Path) -> dict[str, bytes]:
    payloads = {
        "core-a.png": make_fixture_png(16, 8, PALETTE),
        "core-b.png": make_fixture_png(8, 8, PALETTE),
        "core-c.jpg": b"\xff\xd8\xff\xe0fake-jpeg-bytes",
        "notes.txt": b"not an image",
    }
    for name, payload in payloads.items():
        (root / name).write_bytes(payload)
    (root / "core-a-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "core-a",
                "licence": "CC-BY-4.0",
                "attribution": "Toy image attribution.",
            }
        ),
        encoding="utf-8",
    )
    return payloads


def test_inventory_hashes_types_and_attributes(tmp_path: Path) -> None:
    payloads = _write_images(tmp_path)
    records = inventory_images(tmp_path)
    assert [r.name for r in records] == ["core-a.png", "core-b.png", "core-c.jpg"]
    by_name = {r.name: r for r in records}
    assert by_name["core-a.png"].sha256 == hashlib.sha256(
        payloads["core-a.png"]
    ).hexdigest()
    assert by_name["core-a.png"].media_type == "image/png"
    assert by_name["core-c.jpg"].media_type == "image/jpeg"
    assert by_name["core-a.png"].licence == "CC-BY-4.0"
    assert by_name["core-a.png"].attribution == "Toy image attribution."
    assert by_name["core-a.png"].manifest == "core-a-source.json"
    assert by_name["core-b.png"].licence is None
    assert by_name["core-b.png"].attribution is None
    assert by_name["core-b.png"].manifest is None
    assert by_name["core-a.png"].n_bytes == len(payloads["core-a.png"])


def test_inventory_records_round_trip_to_dict(tmp_path: Path) -> None:
    _write_images(tmp_path)
    for record in inventory_images(tmp_path):
        payload = record.to_dict()
        assert payload["name"] == record.name
        assert payload["sha256"] == record.sha256
        assert payload["media_type"] == record.media_type


def test_inventory_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        inventory_images(tmp_path / "no-such-dir")


def test_inventory_empty_directory(tmp_path: Path) -> None:
    assert inventory_images(tmp_path) == []
