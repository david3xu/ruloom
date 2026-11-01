"""Content addressing: SHA-256 over bytes and canonical JSON objects."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_bytes(payload: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(payload).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    """Stable canonical encoding of a JSON-like object.

    Keys sorted, no insignificant whitespace, UTF-8. The same object
    always encodes to the same bytes regardless of dict insertion order.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_canonical(value: Any) -> str:
    """Hex SHA-256 of the canonical encoding of a JSON-like object."""
    return sha256_bytes(canonical_bytes(value))
