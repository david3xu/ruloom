"""Profile registry: locate manifests without naming any domain in engine code.

The engine resolves a profile id to a directory and a manifest document;
capability code is loaded by the composition root from the matching
``capabilities`` module. New profiles arrive as new directories, never as
engine edits.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Profile used when the caller does not name one (compatibility path).
DEFAULT_PROFILE_ID = "hylogger"

#: Manifest file present in every profile directory.
MANIFEST_FILENAME = "profile.json"


def profiles_root() -> Path:
    """Directory containing one subdirectory per profile."""
    return Path(__file__).resolve().parent


def profile_dir(profile_id: str) -> Path:
    """Directory for ``profile_id``; rejects anything but a plain name."""
    if not profile_id or not profile_id.replace("_", "").isalnum():
        raise ValueError(f"invalid profile id: {profile_id!r}")
    candidate = profiles_root() / profile_id
    if not candidate.is_dir():
        raise ValueError(f"unknown profile: {profile_id!r}")
    return candidate


def manifest_path(profile_id: str) -> Path:
    """Manifest document for ``profile_id``."""
    path = profile_dir(profile_id) / MANIFEST_FILENAME
    if not path.is_file():
        raise ValueError(
            f"profile {profile_id!r} has no {MANIFEST_FILENAME}"
        )
    return path


def manifest_dict(profile_id: str) -> dict:
    """Decoded manifest document for ``profile_id``."""
    return json.loads(manifest_path(profile_id).read_text(encoding="utf-8"))


def available_profiles() -> list[str]:
    """Ids of all profiles that ship a manifest."""
    root = profiles_root()
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and not path.name.startswith("_")
        and (path / MANIFEST_FILENAME).is_file()
    )


__all__ = [
    "DEFAULT_PROFILE_ID",
    "MANIFEST_FILENAME",
    "available_profiles",
    "manifest_dict",
    "manifest_path",
    "profile_dir",
    "profiles_root",
]
