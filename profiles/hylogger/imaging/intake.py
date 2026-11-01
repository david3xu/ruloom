"""Image file intake: hash and attribute source imagery.

Image files are source data under the four rules, so intake treats them
exactly like scalar sources: every file is hashed (SHA-256 over its
bytes), typed by extension (``image/png`` or ``image/jpeg``), and
attributed from a sibling ``<stem>-source.json`` manifest carrying
``licence``/``attribution`` (the NVCL CC-BY-4.0 pattern). No pixel is
decoded here and nothing reaches a model; statistics are computed later
by code only (see :mod:`statistics`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

#: Extensions intake accepts, mapped to their artifact media type.
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


@dataclass(frozen=True)
class ImageRecord:
    """One inventoried image file: identity, type, and attribution."""

    name: str
    sha256: str
    media_type: str
    n_bytes: int
    licence: str | None
    attribution: str | None
    manifest: str | None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "n_bytes": self.n_bytes,
            "licence": self.licence,
            "attribution": self.attribution,
            "manifest": self.manifest,
        }


def _sibling_manifest(path: Path) -> Path | None:
    """Sibling ``<stem>-source.json`` manifest for an image file."""
    candidate = path.with_name(path.stem + "-source.json")
    return candidate if candidate.is_file() else None


def inventory_images(images_dir: str | Path) -> list[ImageRecord]:
    """Hash and attribute every PNG/JPEG file directly under ``images_dir``.

    Files are visited in sorted name order so the inventory is
    deterministic. Unknown extensions are ignored (scalar sources and
    manifests live elsewhere). Raises :class:`ValueError` when the
    directory does not exist.
    """
    root = Path(images_dir)
    if not root.is_dir():
        raise ValueError(f"images directory not found: {root}")
    records: list[ImageRecord] = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        media_type = MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            continue
        payload = path.read_bytes()
        licence: str | None = None
        attribution: str | None = None
        manifest_name: str | None = None
        manifest = _sibling_manifest(path)
        if manifest is not None:
            record = json.loads(manifest.read_text(encoding="utf-8"))
            licence = record.get("licence")
            attribution = record.get("attribution")
            manifest_name = manifest.name
        records.append(
            ImageRecord(
                name=path.name,
                sha256=hashlib.sha256(payload).hexdigest(),
                media_type=media_type,
                n_bytes=len(payload),
                licence=licence,
                attribution=attribution,
                manifest=manifest_name,
            )
        )
    return records
