"""Verify generated-image provenance and stage registered bytes for model input.

This protects the declared engine interface against accidental source uploads.
Trusted Python capabilities run in process; it is not an OS sandbox.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models.base import ModelCapabilities, ModelError
from models.image_input import read_validated_image, validate_image_bytes

from .hashing import sha256_bytes


class ImageSecurityError(ModelError):
    """Image input failed provenance, format, or capability admission."""

    dispatched = False


@dataclass(frozen=True)
class AttachedImageInfo:
    """Registered artifact metadata, without source bytes or inline image data."""

    sha256: str
    media_type: str
    byte_size: int
    width: int
    height: int
    produced_by: str
    staged_path: Path


@contextmanager
def staged_image_attachment(
    image_path: str | Path,
    runner: Any,
    capabilities: ModelCapabilities | None,
) -> Generator[AttachedImageInfo, None, None]:
    """Admit a registered, non-source image and stage its immutable payload.

    The caller path is read through the provider's descriptor-bounded reader.
    Its bytes must match both the registered payload and artifact record. The
    transport receives a private copy of registered bytes, never that path.
    """
    if (
        not isinstance(capabilities, ModelCapabilities)
        or not capabilities.supports_image
    ):
        raise ImageSecurityError("model capabilities do not admit image input")
    path = Path(image_path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ImageSecurityError("image path contains a symbolic link: refusing")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise ImageSecurityError(
            "image input is not an accessible regular file"
        ) from None
    source = getattr(runner, "source", None)
    if source is not None and resolved == Path(source).resolve():
        raise ImageSecurityError("image path points to the primary source file")
    images_dir = getattr(runner, "images_dir", None)
    if images_dir is not None and resolved.is_relative_to(Path(images_dir).resolve()):
        raise ImageSecurityError("image path is inside the source companion directory")
    caller_bytes, media_type, width, height = read_validated_image(
        resolved, capabilities
    )
    digest = sha256_bytes(caller_bytes)
    if digest in runner.source_hashes:
        raise ImageSecurityError("image bytes match a known source file hash")
    payloads = runner.artifact_payloads
    if digest not in payloads:
        raise ImageSecurityError("image is not registered in artifact payloads")
    payload = payloads[digest]
    if type(payload) is not bytes or len(payload) > capabilities.max_image_bytes:
        raise ImageSecurityError("registered image payload has invalid type or size")
    if sha256_bytes(payload) != digest:
        raise ImageSecurityError("registered image payload digest mismatch")
    records = [
        r for r in runner.artifact_records if getattr(r, "sha256", None) == digest
    ]
    if not records:
        raise ImageSecurityError("registered image has no artifact record")
    record = records[-1]
    producer = record.produced_by
    specs = getattr(getattr(runner, "manifest", None), "capabilities", None)
    for candidate in records:
        spec = specs.get(candidate.produced_by) if isinstance(specs, Mapping) else None
        if not isinstance(spec, Mapping) or spec.get("reads_raw_source") is not False:
            raise ImageSecurityError(
                "image producer is unknown or a raw-intake capability"
            )
        if (
            candidate.media_type != media_type
            or media_type not in capabilities.image_media_types
        ):
            raise ImageSecurityError("registered image media type mismatch")
    # Validate the selected registry value, not only the caller's snapshot.
    validate_image_bytes(payload, capabilities)
    with tempfile.TemporaryDirectory(prefix="ruloom-image-input-") as directory:
        staged = Path(directory).resolve() / "generated.png"
        staged.write_bytes(payload)
        staged.chmod(0o400)
        yield AttachedImageInfo(
            sha256=digest,
            media_type=media_type,
            byte_size=len(payload),
            width=width,
            height=height,
            produced_by=producer,
            staged_path=staged,
        )
