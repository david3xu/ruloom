"""Separate publication: an accepted release ships in a second step.

Packing (``engine.artifacts.package``) writes ``release.zip`` into the
run's own directory when the human accepts. Publication is the step
after that: copying the accepted bundle into a separate published
area where recipients fetch it, with its own durable record, its own
idempotency, and its own recovery. The two steps fail
independently -- an accepted run whose publication is interrupted is
still accepted, and re-running publication finishes the copy without
ever touching the run again.

Publication refuses loudly unless every binding checks out: the run
directory must carry the release files (``release.zip``,
``manifest.json``, ``release.sha256``); the bundle must pass
consumer verification against the run's manifest; the dispatch item
(see ``engine.core.dispatch``) must rest ``succeeded`` naming this
run with matching digests, and the publishing authority must own
that item. A foreign authority, a mismatched digest, or an
unverifiable bundle all refuse before any byte is copied.

Interruption recovery: the publication record
(``<base>/publications/<item_id>.json``) moves ``in_progress`` before
any copy starts and ``complete`` only after the destination bytes and
sidecar verify. A crash in between leaves either no destination (copy
again) or a destination whose bytes hash to the source (adopt it and
mark complete). Re-publishing a complete record whose destination
still matches is a no-op returning the same identity. Publication
never overwrites destination bytes it cannot attribute to its own
record: foreign bytes refuse loudly instead.

``download`` is the recipient side: it copies a published bundle to a
chosen path and verifies it there, returning the attested identity
(zip hash, artifact hash, member list). A bundle that fails
verification is removed, never left half-fetched.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from ..core.dispatch import AuthorityDenied, DispatchStore
from .consumer import ReleaseVerificationError, verify_release
from .hashing import sha256_bytes
from .package import (
    RELEASE_HASH_NAME,
    RELEASE_MANIFEST_NAME,
    RELEASE_ZIP_NAME,
)

#: Directory (under the store base) holding publication records.
STORE_DIR_NAME = "publications"

#: Publication record states.
IN_PROGRESS = "in_progress"
COMPLETE = "complete"


class PublicationError(Exception):
    """Publication was refused, or the destination cannot be trusted."""


def _resolve_now(now: float | None) -> float:
    """Use the injected clock in tests, the wall clock in production."""
    return time.time() if now is None else float(now)


def _atomic_write_bytes(path: Path, payload: bytes) -> Path:
    """Write bytes atomically (temporary file plus rename), synced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".tmp."
    )
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def _atomic_write_json(path: Path, record: Mapping[str, Any]) -> Path:
    """Write one JSON record atomically (temporary file plus rename)."""
    return _atomic_write_bytes(
        path,
        (json.dumps(dict(record), indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _stage_and_commit(dest_dir: Path, dest_name: str, payload: bytes) -> Path:
    """Copy payload bytes into place atomically (staged file plus rename).

    A crash before the rename leaves only a dot-tmp file beside the
    destination; a crash after leaves the complete destination. Either
    way re-publication converges without a partial ``.zip``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dest_name
    handle, tmp_name = tempfile.mkstemp(
        dir=str(dest_dir), prefix=dest_name + ".tmp."
    )
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return dest


def _remove_stale_tmps(dest_dir: Path, dest_name: str) -> None:
    """Clear staged files from an interrupted copy (same destination)."""
    try:
        entries = list(dest_dir.iterdir())
    except OSError:
        return
    for path in entries:
        if path.name.startswith(dest_name + ".tmp."):
            try:
                path.unlink()
            except OSError:
                pass


def _read_record(path: Path) -> dict[str, Any] | None:
    """Load a publication record, or None when this item never published."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


class Publisher:
    """Separate publication of accepted releases into a shared area.

    ``base`` is the same output root the runs and the dispatch store
    live under, so publication records sit beside the history they
    ship (``<base>/publications/``). ``dest_dir`` is the separate
    published area recipients read from -- a different directory by
    construction, so publishing never rewrites run history.
    """

    def __init__(self, base: Path | str, dest_dir: Path | str) -> None:
        self.base = Path(base)
        self.dest_dir = Path(dest_dir)
        self.dir = self.base / STORE_DIR_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dispatch = DispatchStore(self.base)

    def _record_path(self, item_id: str) -> Path:
        return self.dir / f"{item_id}.json"

    def publish(
        self,
        item_id: str,
        run_dir: Path | str,
        *,
        authority: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Publish one accepted release; return its attested identity.

        The call is idempotent: a complete record whose destination
        still matches returns the same identity without copying again,
        and an interrupted record resumes to the same bytes. Anything
        the record cannot attribute -- foreign destination bytes, a
        foreign authority, a digest mismatch -- refuses loudly.
        """
        at = _resolve_now(now)
        run_path = Path(run_dir)
        zip_path = run_path / RELEASE_ZIP_NAME
        manifest_path = run_path / RELEASE_MANIFEST_NAME
        if not zip_path.is_file() or not manifest_path.is_file():
            raise PublicationError(
                f"run {run_path.name!r} carries no accepted release "
                f"({RELEASE_ZIP_NAME} and {RELEASE_MANIFEST_NAME} "
                "must both exist): nothing to publish"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicationError(
                f"run {run_path.name!r} manifest is unreadable: refusing"
            ) from exc
        try:
            identity = verify_release(zip_path, manifest=manifest)
        except ReleaseVerificationError as exc:
            raise PublicationError(
                f"run {run_path.name!r} release fails consumer "
                f"verification: refusing ({exc})"
            ) from exc
        item = self.dispatch.get(item_id)
        if item.get("authority") != authority:
            raise AuthorityDenied(
                f"item {item_id!r} belongs to another authority: refusing"
            )
        if item.get("status") != "succeeded":
            raise PublicationError(
                f"item {item_id!r} is not succeeded "
                f"(status {item.get('status')!r}): only accepted work ships"
            )
        accepted = item.get("accepted_run") or {}
        if accepted.get("run_id") != run_path.name:
            raise PublicationError(
                f"item {item_id!r} names run {accepted.get('run_id')!r}, "
                f"not {run_path.name!r}: refusing"
            )
        zip_bytes = zip_path.read_bytes()
        zip_sha = sha256_bytes(zip_bytes)
        if accepted.get("zip_sha256") != zip_sha:
            raise PublicationError(
                f"run {run_path.name!r} bytes changed since the success "
                "report: refusing (never ship unreviewed bytes)"
            )
        dest_name = f"{item_id}.zip"
        dest = self.dest_dir / dest_name
        record_path = self._record_path(item_id)
        prior = _read_record(record_path)
        if (
            isinstance(prior, dict)
            and prior.get("status") == COMPLETE
            and prior.get("zip_sha256") == zip_sha
            and dest.is_file()
            and sha256_bytes(dest.read_bytes()) == zip_sha
        ):
            return {
                "item_id": item_id,
                "dest": str(dest),
                "zip_sha256": zip_sha,
                "artifact_sha256": identity["artifact_sha256"],
                "members": list(identity["members"]),
            }
        if (
            isinstance(prior, dict)
            and prior.get("status") == COMPLETE
            and prior.get("zip_sha256") != zip_sha
        ):
            raise PublicationError(
                f"item {item_id!r} already published different bytes: "
                "one item ships one release, refusing"
            )
        if dest.is_file() and sha256_bytes(dest.read_bytes()) != zip_sha:
            if prior is None:
                raise PublicationError(
                    f"destination {dest_name!r} holds bytes no record "
                    "attributes to this item: refusing (never overwrite "
                    "blindly)"
                )
            # Else: our own interrupted copy; the staged commit below
            # replaces it atomically.
        record = {
            "item_id": item_id,
            "authority": authority,
            "run_id": run_path.name,
            "zip_sha256": zip_sha,
            "artifact_sha256": identity["artifact_sha256"],
            "dest_name": dest_name,
            "status": IN_PROGRESS,
            "updated_at": at,
        }
        _atomic_write_json(record_path, record)
        if dest.is_file() and sha256_bytes(dest.read_bytes()) == zip_sha:
            pass  # Bytes already match: adopt them, skip the copy.
        else:
            _remove_stale_tmps(self.dest_dir, dest_name)
            _stage_and_commit(self.dest_dir, dest_name, zip_bytes)
        if sha256_bytes(dest.read_bytes()) != zip_sha:
            raise PublicationError(
                f"destination {dest_name!r} does not match the accepted "
                "bytes after copy: refusing"
            )
        sidecar = self.dest_dir / f"{dest_name}.sha256"
        _atomic_write_bytes(sidecar, f"{zip_sha}  {dest_name}\n".encode("utf-8"))
        record = {
            "item_id": item_id,
            "authority": authority,
            "run_id": run_path.name,
            "zip_sha256": zip_sha,
            "artifact_sha256": identity["artifact_sha256"],
            "dest_name": dest_name,
            "status": COMPLETE,
            "updated_at": at,
        }
        _atomic_write_json(record_path, record)
        return {
            "item_id": item_id,
            "dest": str(dest),
            "zip_sha256": zip_sha,
            "artifact_sha256": identity["artifact_sha256"],
            "members": list(identity["members"]),
        }

    def get(self, item_id: str) -> dict[str, Any] | None:
        """Return a copy of one publication record, or None."""
        record = _read_record(self._record_path(item_id))
        return dict(record) if record is not None else None


def download(
    zip_path: Path | str,
    out_path: Path | str,
    *,
    manifest: Any = None,
) -> dict[str, Any]:
    """Fetch a published bundle to ``out_path`` and verify it there.

    Returns the attested identity (zip hash, artifact hash, member
    list). A bundle that fails verification is removed from the
    destination, never left half-fetched.
    """
    source = Path(zip_path)
    dest = Path(out_path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise PublicationError(f"cannot read published bundle: {exc}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(payload)
        identity = verify_release(dest, manifest=manifest)
    except (ReleaseVerificationError, OSError) as exc:
        try:
            dest.unlink()
        except OSError:
            pass
        raise PublicationError(
            f"downloaded bundle fails consumer verification: {exc}"
        ) from exc
    return {
        "dest": str(dest),
        "zip_sha256": sha256_bytes(payload),
        "artifact_sha256": identity["artifact_sha256"],
        "members": list(identity["members"]),
    }
