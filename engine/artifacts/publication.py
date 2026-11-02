"""Separate publication: an accepted release ships in a second step.

Packing (``engine.artifacts.package``) writes ``release.zip`` into the
run's own directory when the human accepts. Publication is the step
after that: copying the accepted bundle into a separate published
area where recipients fetch it, with its own durable record, its own
idempotency, and its own recovery. The two steps fail
independently -- an accepted run whose publication is interrupted is
still accepted, and re-running publication finishes the copy without
ever touching the run again.

Publication refuses loudly unless every binding checks out against
the run's persisted acceptance record (never against a
worker-supplied claim alone): the run directory must carry
``release.zip`` with a matching detached ``release.sha256``; a human
``decision.json`` verdict of ``accept`` binding the frozen evidence
digest; a ``frozen-manifest.json`` whose digest matches that binding;
a ``manifest.json`` whose members, sizes, hashes and artifact digest
match the frozen evidence; a shipped validation report reading
``pass`` and covering every persisted required check; a single
selected attempt whose receipt records ``pass`` for the artifact; and
a dispatch item (see ``engine.core.dispatch``) resting ``succeeded``
under the publishing authority, naming this run with reported
digests equal to the persisted ones and a requested profile equal to
the run's own. A foreign authority, a mismatched digest, or an
unverifiable bundle all refuse before any byte is copied.

Request-to-run binding: the dispatch request's bound identity
(profile, pinned source-content digest, pinned normalised spec
digest) must equal the identity the run's acceptance record binds
and the identity the run's own record delivers. Profile-name equality
alone never suffices; a request that pins nothing shippable refuses.
A declared row bound (``max_rows``, top-level or spec-carried) must
also cover the run's executed bound: the shipped row count must
satisfy the requested bound. A declared attempt bound
(``max_attempts``, top-level or spec-carried) must cover the run's
executed attempt bound: the run must never execute more attempts
than requested.

Single-snapshot publication: the run ZIP is read once into an
immutable in-memory snapshot staged through a private temp file; the
consumer check and the acceptance resolution both run against that
snapshot, and the copy ships from it -- the run file is never
re-read for bytes that ship. A stability guard re-checks the run
file's digest after acceptance (refusing on any change, so bytes
swapped under the gate never ship); the guard's re-read can only
refuse, never supply shippable bytes. Before the record is marked
complete the snapshot digest, the destination digest, and the
persisted accepted ZIP digest must all agree, and the returned
identity derives from the snapshot alone.

Interruption recovery: the publication record
(``<base>/publications/<item_id>.json``) moves ``in_progress`` before
any copy starts and ``complete`` only after the destination bytes and
sidecar verify. A crash in between leaves either no destination (copy
again) or a destination whose bytes hash to the source (adopt it and
mark complete). An in-progress record alone never establishes
ownership of other bytes: only a matching content digest adopts an
existing destination, in every phase. Re-publishing a complete record whose destination
still matches is a no-op returning the same identity. Once the record
is complete the destination must keep attesting the recorded bytes:
a missing, corrupt, or foreign destination refuses re-publication
instead of being silently re-copied -- recovery goes through the
authorised ``repair`` operation, which preserves any replaced bytes
beside the destination and appends the authorising decision (who,
when, why, what was found) to the record. Publication never
overwrites destination bytes it cannot attribute to its own record:
foreign bytes refuse loudly instead.

``download`` is the recipient side: it copies a published bundle to a
chosen path and verifies it there, returning the attested identity
(zip hash, artifact hash, member list). The new bytes verify in a
temporary sibling and commit atomically, so a bundle that fails
verification never touches a pre-existing destination.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..core.dispatch import (
    AuthorityDenied,
    DispatchStore,
    effective_max_attempts,
    effective_max_rows,
)
from .consumer import ReleaseVerificationError, verify_release
from .hashing import sha256_bytes
from .package import (
    FROZEN_MANIFEST_NAME,
    RELEASE_HASH_NAME,
    RELEASE_MANIFEST_NAME,
    RELEASE_ZIP_NAME,
    candidate_digest,
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
    handle, tmp_name = tempfile.mkstemp(dir=str(dest_dir), prefix=dest_name + ".tmp.")
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


#: Canonical publication item ids (mirrors the dispatch store rule):
#: letters, digits, dash or underscore, at most 64 characters. The
#: shape excludes separators, dots and absolute paths, so an id can
#: never address a record or a destination outside its own directory.
ITEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

#: The human gate decision (written by ``engine.core.runstore``).
DECISION_NAME = "decision.json"

#: The engine run record (written by ``engine.core.runstore``).
RUN_RECORD_NAME = "run.json"

#: Per-attempt gate evidence (written by ``engine.core.runstore``).
ATTEMPTS_DIR_NAME = "attempts"
RECEIPT_NAME = "receipt.json"

#: The shipped validation report inside the bundle.
REPORT_MEMBER = "validation-report.json"


def _check_item_id(item_id: Any) -> str:
    """Validate a caller-supplied publication item id, or refuse."""
    if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id):
        raise PublicationError(
            f"publication item id {item_id!r} is outside the canonical "
            "form (letters, digits, dash, underscore; at most 64): "
            "refusing"
        )
    return item_id


def _read_json_file(path: Path, *, what: str) -> dict[str, Any]:
    """Load one persisted JSON record, or refuse loudly."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(
            f"run carries no readable {what} ({path.name}): refusing"
        ) from exc
    if not isinstance(payload, Mapping):
        raise PublicationError(f"run {what} ({path.name}) is not an object: refusing")
    return dict(payload)


def _check_required_coverage(report: Any, required: Any) -> None:
    """Require a passing verdict covering every persisted required check.

    Each required id must appear exactly once in the shipped report
    results with status ``pass``. A missing, duplicated, or
    non-passing required check refuses: the report is not the
    accepted one.
    """
    if not isinstance(report, Mapping):
        raise PublicationError("shipped report is not an object: refusing")
    if report.get("verdict") != "pass":
        raise PublicationError(
            f"shipped report verdict is {report.get('verdict')!r}, not 'pass': refusing"
        )
    results = report.get("results")
    if not isinstance(results, list):
        raise PublicationError("shipped report carries no results: refusing")
    if not isinstance(required, list):
        raise PublicationError(
            "persisted acceptance declares no required checks: refusing"
        )
    for check_id in required:
        if not isinstance(check_id, str) or not check_id:
            raise PublicationError(
                "persisted acceptance declares an invalid required check: refusing"
            )
        passing = [
            entry
            for entry in results
            if isinstance(entry, Mapping)
            and entry.get("name") == check_id
            and entry.get("status") == "pass"
        ]
        if len(passing) != 1:
            raise PublicationError(
                f"required check {check_id!r} is not satisfied exactly "
                "once by the shipped report: refusing"
            )


def _capture_snapshot(zip_path: Path, run_name: str) -> bytes:
    """Read the run ZIP once into an immutable in-memory snapshot.

    This is the only read whose bytes may ship: verification, the
    acceptance resolution, and the copy all operate on this snapshot,
    never on a fresh read of the run file.
    """
    try:
        return zip_path.read_bytes()
    except OSError as exc:
        raise PublicationError(
            f"run {run_name!r} release is unreadable: refusing"
        ) from exc


def _stage_snapshot(store_dir: Path, item_id: str, snapshot: bytes) -> Path:
    """Stage the snapshot through a private temp file for verification.

    Zip-based checks need a file; the staged copy carries exactly the
    snapshot bytes (verified on return) and is unlinked by the caller.
    The name is unique per staging, so a crashed publish leaves only
    inert bytes no later publish ever reads.
    """
    handle, tmp_name = tempfile.mkstemp(
        dir=str(store_dir), prefix=f"{item_id}.snapshot."
    )
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(snapshot)
            tmp.flush()
            os.fsync(tmp.fileno())
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return Path(tmp_name)


def _verify_snapshot_stable(zip_path: Path, snapshot_digest: str) -> None:
    """Refuse when the run file changed after the snapshot capture.

    This is the tripwire at the acceptance/copy boundary: acceptance
    resolved against the snapshot, so bytes swapped under the gate
    must refuse instead of shipping the snapshot or the new bytes.
    This re-read can only refuse -- its bytes are never shipped or
    attested.
    """
    try:
        current_digest = sha256_bytes(zip_path.read_bytes())
    except OSError as exc:
        raise PublicationError(
            f"run {zip_path.parent.name!r} release is unreadable "
            f"after acceptance: refusing ({exc})"
        ) from exc
    if current_digest != snapshot_digest:
        raise PublicationError(
            f"run {zip_path.parent.name!r} release.zip changed after "
            "acceptance: refusing (never ship bytes the gate did not bind)"
        )


def _resolve_acceptance(
    run_path: Path,
    manifest: Mapping[str, Any],
    *,
    zip_bytes: bytes,
    snapshot_path: Path,
) -> dict[str, Any]:
    """Resolve the run's persisted acceptance record, or refuse.

    The worker's success report names a run; this resolves what the
    run directory itself proves: the detached hash binds the shipped
    bytes, the human decision binds the frozen digest, the frozen
    manifest matches that digest, the release manifest matches the
    frozen evidence member for member, the shipped report passes with
    full required coverage, and a single selected attempt's receipt
    records ``pass`` for the artifact. The ZIP bytes and the report
    member both come from the captured snapshot, never from a fresh
    read of the run file. Anything else is not an accepted release,
    however the worker described it.
    """
    zip_sha = sha256_bytes(zip_bytes)
    try:
        sidecar = (run_path / RELEASE_HASH_NAME).read_text(encoding="utf-8")
    except OSError as exc:
        raise PublicationError(
            f"run {run_path.name!r} carries no detached {RELEASE_HASH_NAME}: refusing"
        ) from exc
    if sidecar.split() != [zip_sha, RELEASE_ZIP_NAME]:
        raise PublicationError(
            f"run {run_path.name!r} detached {RELEASE_HASH_NAME} "
            "does not match the shipped bytes: refusing"
        )
    decision = _read_json_file(run_path / DECISION_NAME, what="decision")
    if decision.get("verdict") != "accept":
        raise PublicationError(
            f"run {run_path.name!r} was never accepted "
            f"(decision {decision.get('verdict')!r}): refusing"
        )
    binding = decision.get("frozen_manifest_sha256")
    if not isinstance(binding, str) or not binding:
        raise PublicationError(
            f"run {run_path.name!r} decision binds no frozen manifest: refusing"
        )
    frozen = _read_json_file(run_path / FROZEN_MANIFEST_NAME, what="frozen manifest")
    if frozen.get("evidence_digest") != binding:
        raise PublicationError(
            f"run {run_path.name!r} frozen evidence does not match "
            "the accepted decision: refusing"
        )
    frozen_members = frozen.get("members")
    if (
        not isinstance(frozen_members, list)
        or not frozen_members
        or not all(
            isinstance(entry, Mapping)
            and isinstance(entry.get("path"), str)
            and entry.get("path")
            for entry in frozen_members
        )
    ):
        raise PublicationError(
            f"run {run_path.name!r} frozen manifest lists no members: refusing"
        )
    if candidate_digest(list(frozen_members)) != binding:
        raise PublicationError(
            f"run {run_path.name!r} frozen evidence does not match "
            "the accepted digest: refusing"
        )
    if manifest.get("evidence_digest") != binding:
        raise PublicationError(
            f"run {run_path.name!r} release manifest does not match "
            "the accepted frozen evidence: refusing"
        )
    artifact = manifest.get("artifact_sha256")
    if not isinstance(artifact, str) or not artifact:
        raise PublicationError(
            f"run {run_path.name!r} release manifest carries no "
            "artifact digest: refusing"
        )
    if frozen.get("artifact_sha256") != artifact:
        raise PublicationError(
            f"run {run_path.name!r} release manifest artifact does "
            "not match the accepted artifact: refusing"
        )
    manifest_members = manifest.get("members")
    if not isinstance(manifest_members, list):
        raise PublicationError(
            f"run {run_path.name!r} release manifest lists no members: refusing"
        )
    manifest_by_path = {
        entry.get("path"): entry
        for entry in manifest_members
        if isinstance(entry, Mapping)
    }
    for entry in frozen_members:
        other = manifest_by_path.get(entry.get("path"))
        if not isinstance(other, Mapping):
            raise PublicationError(
                f"accepted member {entry.get('path')!r} is absent "
                "from the release manifest: refusing"
            )
        for field in ("size", "sha256", "attempt"):
            if other.get(field) != entry.get(field):
                raise PublicationError(
                    f"accepted member {entry.get('path')!r} does not "
                    "match the release manifest: refusing"
                )
    acceptance = manifest.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise PublicationError(
            f"run {run_path.name!r} carries no persisted acceptance record: refusing"
        )
    if acceptance.get("verdict") != "pass" or list(acceptance.get("failed") or []):
        raise PublicationError(
            f"run {run_path.name!r} persisted acceptance is not a "
            "passing gate: refusing"
        )
    try:
        with zipfile.ZipFile(snapshot_path) as archive:
            report_raw = archive.read(REPORT_MEMBER)
    except (OSError, KeyError, RuntimeError) as exc:
        raise PublicationError(
            f"run {run_path.name!r} bundle carries no readable "
            f"{REPORT_MEMBER}: refusing ({exc})"
        ) from exc
    try:
        shipped_report = json.loads(report_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(
            f"run {run_path.name!r} shipped report is not valid JSON: refusing ({exc})"
        ) from exc
    _check_required_coverage(shipped_report, acceptance.get("required_checks"))
    attempts = {entry.get("attempt") for entry in frozen_members}
    if len(attempts) != 1:
        raise PublicationError(
            f"run {run_path.name!r} accepted members span several attempts: refusing"
        )
    attempt_no = next(iter(attempts))
    receipt = _read_json_file(
        run_path / ATTEMPTS_DIR_NAME / f"attempt-{attempt_no}" / RECEIPT_NAME,
        what="attempt receipt",
    )
    if receipt.get("verdict") != "pass":
        raise PublicationError(
            f"run {run_path.name!r} selected attempt did not pass its gate: refusing"
        )
    refs = receipt.get("artifact_refs")
    if not isinstance(refs, list) or artifact not in refs:
        raise PublicationError(
            f"run {run_path.name!r} selected attempt does not bind "
            "the accepted artifact: refusing"
        )
    run_record = _read_json_file(run_path / RUN_RECORD_NAME, what="run record")
    run_meta = run_record.get("run")
    if not isinstance(run_meta, Mapping):
        raise PublicationError(
            f"run {run_path.name!r} run record carries no run: refusing"
        )
    profile = run_meta.get("profile_ref")
    if not isinstance(profile, str) or not profile:
        raise PublicationError(
            f"run {run_path.name!r} run record names no profile: refusing"
        )
    source_sha = run_record.get("source_sha256")
    if not isinstance(source_sha, str) or not source_sha:
        raise PublicationError(
            f"run {run_path.name!r} run record carries no measured "
            "source digest: refusing"
        )
    run_request = run_meta.get("request")
    if not isinstance(run_request, Mapping):
        raise PublicationError(
            f"run {run_path.name!r} run record carries no request: refusing"
        )
    spec_digest = run_request.get("spec_digest")
    if not isinstance(spec_digest, str) or not spec_digest:
        raise PublicationError(
            f"run {run_path.name!r} run record carries no spec digest: refusing"
        )
    max_rows: int | None = None
    max_attempts: int | None = None
    run_spec = run_request.get("spec")
    if isinstance(run_spec, Mapping):
        limits = run_spec.get("limits")
        if isinstance(limits, Mapping) and "max_rows" in limits:
            bound = limits.get("max_rows")
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 1:
                raise PublicationError(
                    f"run {run_path.name!r} run record carries an "
                    "unusable row bound: refusing"
                )
            max_rows = bound
        if isinstance(limits, Mapping) and "max_attempts" in limits:
            attempt_bound = limits.get("max_attempts")
            if (
                isinstance(attempt_bound, bool)
                or not isinstance(attempt_bound, int)
                or attempt_bound < 1
            ):
                raise PublicationError(
                    f"run {run_path.name!r} run record carries an "
                    "unusable attempt bound: refusing"
                )
            max_attempts = attempt_bound
    acceptance_bound = acceptance.get("request_identity")
    if not isinstance(acceptance_bound, Mapping):
        raise PublicationError(
            f"run {run_path.name!r} acceptance binds no request "
            "identity: refusing (only bound runs ship)"
        )
    return {
        "zip_sha256": zip_sha,
        "artifact_sha256": artifact,
        "run_profile": profile,
        "source_sha256": source_sha,
        "spec_digest": spec_digest,
        "max_rows": max_rows,
        "max_attempts": max_attempts,
        "request_identity": {
            "profile": acceptance_bound.get("profile"),
            "source_sha256": acceptance_bound.get("source_sha256"),
            "spec_digest": acceptance_bound.get("spec_digest"),
        },
    }


def _check_request_binding(
    item_id: str, item: Mapping[str, Any], acceptance: Mapping[str, Any]
) -> None:
    """Require persisted == acceptance-bound == delivered identities.

    The dispatch item persists the bound identity at submit; the run's
    acceptance block binds the identity the run was launched for; the
    run record delivers the measured values. All three must agree on
    profile, source-content digest, and normalised spec digest --
    profile-name equality alone never suffices. The coarse checks run
    first (profile, source, row and attempt scope) so a refused bound
    names the bound; the exact spec-digest identity runs last. A
    declared row bound must also cover the run's executed bound, so
    the shipped row count satisfies the requested bound; a declared
    attempt bound must cover the run's executed attempt bound, so the
    run never executes more attempts than requested.
    """
    persisted = item.get("request_identity")
    if not isinstance(persisted, Mapping):
        raise PublicationError(
            f"item {item_id!r} binds no request identity: refusing "
            "(only bound requests ship)"
        )
    bound = acceptance.get("request_identity")
    if not isinstance(bound, Mapping):
        raise PublicationError(
            "run acceptance binds no request identity: refusing (only bound runs ship)"
        )
    for field in ("profile", "source_sha256"):
        wanted = persisted.get(field)
        accepted = bound.get(field)
        if wanted != accepted:
            raise PublicationError(
                f"item {item_id!r} request binding {field!r} "
                f"({wanted!r}) does not match the accepted binding "
                f"({accepted!r}): refusing"
            )
    for field, delivered_key in (
        ("profile", "run_profile"),
        ("source_sha256", "source_sha256"),
    ):
        accepted = bound.get(field)
        delivered = acceptance.get(delivered_key)
        if accepted != delivered:
            raise PublicationError(
                f"accepted request binding {field!r} ({accepted!r}) "
                "does not match the delivered run "
                f"({delivered!r}): refusing (the run changed after "
                "acceptance)"
            )
    wanted_rows = effective_max_rows(
        item.get("request") if isinstance(item.get("request"), Mapping) else {}
    )
    if wanted_rows is not None:
        delivered_rows = acceptance.get("max_rows")
        if (
            not isinstance(delivered_rows, int)
            or isinstance(delivered_rows, bool)
            or delivered_rows > wanted_rows
        ):
            raise PublicationError(
                f"item {item_id!r} requests max_rows {wanted_rows}, "
                f"but the accepted run allows {delivered_rows}: "
                "refusing (the shipped row count must satisfy the "
                "requested bound)"
            )
    wanted_attempts = effective_max_attempts(
        item.get("request") if isinstance(item.get("request"), Mapping) else {}
    )
    if wanted_attempts is not None:
        delivered_attempts = acceptance.get("max_attempts")
        if (
            not isinstance(delivered_attempts, int)
            or isinstance(delivered_attempts, bool)
            or delivered_attempts > wanted_attempts
        ):
            raise PublicationError(
                f"item {item_id!r} requests max_attempts {wanted_attempts}, "
                f"but the accepted run allows {delivered_attempts}: "
                "refusing (the run must not execute more attempts than "
                "requested)"
            )
    wanted_spec = persisted.get("spec_digest")
    accepted_spec = bound.get("spec_digest")
    if wanted_spec != accepted_spec:
        raise PublicationError(
            f"item {item_id!r} request binding 'spec_digest' "
            f"({wanted_spec!r}) does not match the accepted binding "
            f"({accepted_spec!r}): refusing"
        )
    if accepted_spec != acceptance.get("spec_digest"):
        raise PublicationError(
            "accepted request binding 'spec_digest' "
            f"({accepted_spec!r}) does not match the delivered run "
            f"({acceptance.get('spec_digest')!r}): refusing (the run "
            "changed after acceptance)"
        )


def _capture_verified_snapshot(
    store_dir: Path, clean_id: str, run_path: Path
) -> tuple[bytes, str, dict[str, Any], dict[str, Any], Path]:
    """Capture the run ZIP and resolve its acceptance, or refuse.

    Reads the run ZIP once into an immutable snapshot, stages it
    through a private temp file for the consumer check, and resolves
    the run's persisted acceptance record against that snapshot with
    the stability guard applied. Returns the snapshot bytes, their
    digest, the consumer identity, the acceptance record, and the
    staged snapshot path; the caller owns the staged path and must
    unlink it. Shared by publication and authorised repair, so both
    ship only bytes the gate bound.
    """
    zip_path = run_path / RELEASE_ZIP_NAME
    manifest_path = run_path / RELEASE_MANIFEST_NAME
    if not zip_path.is_file() or not manifest_path.is_file():
        raise PublicationError(
            f"run {run_path.name!r} carries no accepted release "
            f"({RELEASE_ZIP_NAME} and {RELEASE_MANIFEST_NAME} "
            "must both exist): nothing to publish"
        )
    manifest = _read_json_file(manifest_path, what="release manifest")
    snapshot = _capture_snapshot(zip_path, run_path.name)
    snapshot_digest = sha256_bytes(snapshot)
    snapshot_path = _stage_snapshot(store_dir, clean_id, snapshot)
    try:
        try:
            identity = verify_release(snapshot_path, manifest=manifest)
        except ReleaseVerificationError as exc:
            raise PublicationError(
                f"run {run_path.name!r} release fails consumer "
                f"verification: refusing ({exc})"
            ) from exc
        if identity["zip_sha256"] != snapshot_digest:
            raise PublicationError(
                f"run {run_path.name!r} snapshot changed during verification: refusing"
            )
        acceptance = _resolve_acceptance(
            run_path,
            manifest,
            zip_bytes=snapshot,
            snapshot_path=snapshot_path,
        )
        _verify_snapshot_stable(zip_path, snapshot_digest)
    except BaseException:
        try:
            snapshot_path.unlink()
        except OSError:
            pass
        raise
    return snapshot, snapshot_digest, dict(identity), acceptance, snapshot_path


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
        """The record path for one id, contained inside the store."""
        clean = _check_item_id(item_id)
        path = self.dir / f"{clean}.json"
        if path.resolve().parent != self.dir.resolve():
            raise PublicationError(
                f"publication item id {item_id!r} resolves outside its store: refusing"
            )
        return path

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

        The worker's success report is evidence, not authority: the
        run id it names resolves the run's persisted acceptance
        record, and the reported digests must equal the persisted
        ones. A claim pointing at unaccepted, altered, or
        foreign-profile bytes refuses even over a live lease.

        The run ZIP is read once into an immutable snapshot; the
        consumer check, the acceptance resolution, and the copy all
        operate on that snapshot, and the returned identity derives
        from it alone. Bytes swapped into the run file after
        acceptance refuse at the stability guard and never ship.
        """
        at = _resolve_now(now)
        clean_id = _check_item_id(item_id)
        run_path = Path(run_dir)
        snapshot, snapshot_digest, identity, acceptance, snapshot_path = (
            _capture_verified_snapshot(self.dir, clean_id, run_path)
        )
        try:
            return self._publish_snapshot(
                clean_id,
                run_path,
                identity,
                acceptance,
                snapshot,
                snapshot_digest,
                authority=authority,
                now=at,
            )
        finally:
            try:
                snapshot_path.unlink()
            except OSError:
                pass

    def _require_shippable(
        self,
        clean_id: str,
        run_path: Path,
        acceptance: Mapping[str, Any],
        authority: str,
    ) -> dict[str, Any]:
        """Require the dispatch item to authorise this shipment, or refuse.

        The item must rest ``succeeded`` under the calling authority,
        name this run, and report the persisted accepted digests; the
        requested profile and request identity must bind the accepted
        and delivered values. Shared by publication and authorised
        repair: neither restores bytes the gate never bound.
        """
        item = self.dispatch.get(clean_id)
        if item.get("authority") != authority:
            raise AuthorityDenied(
                f"item {clean_id!r} belongs to another authority: refusing"
            )
        if item.get("status") != "succeeded":
            raise PublicationError(
                f"item {clean_id!r} is not succeeded "
                f"(status {item.get('status')!r}): only accepted work ships"
            )
        accepted = item.get("accepted_run") or {}
        if accepted.get("run_id") != run_path.name:
            raise PublicationError(
                f"item {clean_id!r} names run {accepted.get('run_id')!r}, "
                f"not {run_path.name!r}: refusing"
            )
        if accepted.get("zip_sha256") != acceptance["zip_sha256"]:
            raise PublicationError(
                f"item {clean_id!r} reported zip digest does not match "
                "the persisted accepted digest: refusing (never trust "
                "a worker claim over the acceptance record)"
            )
        if accepted.get("artifact_sha256") != acceptance["artifact_sha256"]:
            raise PublicationError(
                f"item {clean_id!r} reported artifact digest does not "
                "match the persisted accepted artifact: refusing "
                "(never ship a digest the gate never bound)"
            )
        request = item.get("request")
        wanted = request.get("profile") if isinstance(request, Mapping) else None
        if isinstance(wanted, str) and wanted and wanted != acceptance["run_profile"]:
            raise PublicationError(
                f"item {clean_id!r} requests profile {wanted!r}, but "
                f"the accepted run is profile "
                f"{acceptance['run_profile']!r}: refusing"
            )
        _check_request_binding(clean_id, item, acceptance)
        return dict(item)

    def _publish_snapshot(
        self,
        clean_id: str,
        run_path: Path,
        identity: Mapping[str, Any],
        acceptance: Mapping[str, Any],
        snapshot: bytes,
        snapshot_digest: str,
        *,
        authority: str,
        now: float,
    ) -> dict[str, Any]:
        """Ship one verified snapshot after all guards have passed."""
        at = now
        self._require_shippable(clean_id, run_path, acceptance, authority)
        dest_name = f"{clean_id}.zip"
        dest = self.dest_dir / dest_name
        if dest.resolve().parent != self.dest_dir.resolve():
            raise PublicationError(
                f"destination {dest_name!r} resolves outside the "
                "published area: refusing"
            )
        record_path = self._record_path(clean_id)
        prior = _read_record(record_path)
        if (
            isinstance(prior, dict)
            and prior.get("status") == COMPLETE
            and prior.get("zip_sha256") == snapshot_digest
            and dest.is_file()
            and sha256_bytes(dest.read_bytes()) == snapshot_digest
        ):
            return {
                "item_id": clean_id,
                "dest": str(dest),
                "zip_sha256": snapshot_digest,
                "artifact_sha256": identity["artifact_sha256"],
                "members": list(identity["members"]),
            }
        if (
            isinstance(prior, dict)
            and prior.get("status") == COMPLETE
            and prior.get("zip_sha256") != snapshot_digest
        ):
            raise PublicationError(
                f"item {clean_id!r} already published different bytes: "
                "one item ships one release, refusing"
            )
        if (
            isinstance(prior, dict)
            and prior.get("status") == COMPLETE
            and prior.get("zip_sha256") == snapshot_digest
            and (
                not dest.is_file() or sha256_bytes(dest.read_bytes()) != snapshot_digest
            )
        ):
            raise PublicationError(
                f"item {clean_id!r} is already published, but the "
                "destination no longer attests the recorded bytes: "
                "refusing (recover with the authorised repair "
                "operation, never by re-publishing)"
            )
        if dest.is_file() and sha256_bytes(dest.read_bytes()) != snapshot_digest:
            # Ownership needs a matching content digest in every phase:
            # an in-progress record alone never establishes ownership
            # of other bytes. A differing destination is never adopted
            # as our own interrupted copy -- an interrupted copy leaves
            # only staged tmp files beside the destination (cleared
            # below), never a foreign destination. Refuse loudly; a
            # complete record's diverged destination names the
            # authorised repair path above.
            raise PublicationError(
                f"destination {dest_name!r} holds bytes no record "
                "attributes to this item: refusing (never overwrite "
                "blindly)"
            )
        record = {
            "item_id": clean_id,
            "authority": authority,
            "run_id": run_path.name,
            "zip_sha256": snapshot_digest,
            "artifact_sha256": identity["artifact_sha256"],
            "dest_name": dest_name,
            "status": IN_PROGRESS,
            "updated_at": at,
        }
        _atomic_write_json(record_path, record)
        if dest.is_file() and sha256_bytes(dest.read_bytes()) == snapshot_digest:
            pass  # Bytes already match: adopt them, skip the copy.
        else:
            _remove_stale_tmps(self.dest_dir, dest_name)
            _stage_and_commit(self.dest_dir, dest_name, snapshot)
        if sha256_bytes(dest.read_bytes()) != snapshot_digest:
            raise PublicationError(
                f"destination {dest_name!r} does not match the accepted "
                "bytes after copy: refusing"
            )
        sidecar = self.dest_dir / f"{dest_name}.sha256"
        _atomic_write_bytes(sidecar, f"{snapshot_digest}  {dest_name}\n".encode())
        record = {
            "item_id": clean_id,
            "authority": authority,
            "run_id": run_path.name,
            "zip_sha256": snapshot_digest,
            "artifact_sha256": identity["artifact_sha256"],
            "dest_name": dest_name,
            "status": COMPLETE,
            "updated_at": at,
        }
        _atomic_write_json(record_path, record)
        return {
            "item_id": clean_id,
            "dest": str(dest),
            "zip_sha256": snapshot_digest,
            "artifact_sha256": identity["artifact_sha256"],
            "members": list(identity["members"]),
        }

    def repair(
        self,
        item_id: str,
        run_dir: Path | str,
        *,
        authority: str,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Restore a complete publication's destination, recording why.

        Plain re-publication refuses once the destination stops
        attesting the recorded bytes; this is the only operation that
        restores them. It re-runs every acceptance guard, restores
        exactly the recorded bytes (never a new release -- one item
        ships one release), preserves any replaced bytes beside the
        destination under a digest-derived name instead of dropping
        them, and appends the authorising decision to the record. A
        destination that still matches needs no repair and returns
        the same identity without recording anything.
        """
        at = _resolve_now(now)
        clean_id = _check_item_id(item_id)
        if not isinstance(reason, str) or not reason.strip():
            raise PublicationError(
                "repair restores published bytes under a recorded "
                "decision: refusing without a reason"
            )
        run_path = Path(run_dir)
        snapshot, snapshot_digest, identity, acceptance, snapshot_path = (
            _capture_verified_snapshot(self.dir, clean_id, run_path)
        )
        try:
            return self._repair_snapshot(
                clean_id,
                run_path,
                identity,
                acceptance,
                snapshot,
                snapshot_digest,
                authority=authority,
                reason=reason,
                now=at,
            )
        finally:
            try:
                snapshot_path.unlink()
            except OSError:
                pass

    def _repair_snapshot(
        self,
        clean_id: str,
        run_path: Path,
        identity: Mapping[str, Any],
        acceptance: Mapping[str, Any],
        snapshot: bytes,
        snapshot_digest: str,
        *,
        authority: str,
        reason: str,
        now: float,
    ) -> dict[str, Any]:
        """Restore one verified snapshot over a diverged destination."""
        at = now
        self._require_shippable(clean_id, run_path, acceptance, authority)
        record_path = self._record_path(clean_id)
        prior = _read_record(record_path)
        if not isinstance(prior, dict) or prior.get("status") != COMPLETE:
            raise PublicationError(
                f"item {clean_id!r} carries no complete publication: "
                "refusing (publish first; repair only restores)"
            )
        if prior.get("zip_sha256") != snapshot_digest:
            raise PublicationError(
                f"item {clean_id!r} already published different bytes: "
                "one item ships one release, refusing"
            )
        dest_name = prior.get("dest_name")
        if not isinstance(dest_name, str) or not dest_name:
            raise PublicationError(
                f"item {clean_id!r} complete record names no destination: refusing"
            )
        dest = self.dest_dir / dest_name
        if dest.resolve().parent != self.dest_dir.resolve():
            raise PublicationError(
                f"destination {dest_name!r} resolves outside the "
                "published area: refusing"
            )
        if dest.is_file() and sha256_bytes(dest.read_bytes()) == snapshot_digest:
            return {
                "item_id": clean_id,
                "dest": str(dest),
                "zip_sha256": snapshot_digest,
                "artifact_sha256": identity["artifact_sha256"],
                "members": list(identity["members"]),
            }
        found: str
        preserved: str | None = None
        if dest.is_file():
            found = sha256_bytes(dest.read_bytes())
            preserved_path = self.dest_dir / f"{dest_name}.replaced.{found}"
            if preserved_path.is_file():
                if sha256_bytes(preserved_path.read_bytes()) != found:
                    raise PublicationError(
                        f"repair quarantine {preserved_path.name!r} holds "
                        "unexpected bytes: refusing"
                    )
                dest.unlink()
            else:
                dest.rename(preserved_path)
            preserved = preserved_path.name
        else:
            found = "absent"
        _remove_stale_tmps(self.dest_dir, dest_name)
        _stage_and_commit(self.dest_dir, dest_name, snapshot)
        if sha256_bytes(dest.read_bytes()) != snapshot_digest:
            raise PublicationError(
                f"destination {dest_name!r} does not match the accepted "
                "bytes after repair: refusing"
            )
        sidecar = self.dest_dir / f"{dest_name}.sha256"
        _atomic_write_bytes(sidecar, f"{snapshot_digest}  {dest_name}\n".encode())
        previous = prior.get("repairs", [])
        if not isinstance(previous, list):
            raise PublicationError(
                f"item {clean_id!r} publication record carries a "
                "malformed repair history: refusing"
            )
        record = {
            "item_id": clean_id,
            "authority": prior.get("authority"),
            "run_id": prior.get("run_id"),
            "zip_sha256": snapshot_digest,
            "artifact_sha256": identity["artifact_sha256"],
            "dest_name": dest_name,
            "status": COMPLETE,
            "updated_at": at,
            "repairs": [
                *previous,
                {
                    "at": at,
                    "authority": authority,
                    "reason": reason,
                    "found": found,
                    "restored": snapshot_digest,
                    "preserved": preserved,
                },
            ],
        }
        _atomic_write_json(record_path, record)
        return {
            "item_id": clean_id,
            "dest": str(dest),
            "zip_sha256": snapshot_digest,
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
    list). The download is atomic: the new bytes are verified in a
    unique temporary sibling inside the destination directory, the
    pre-existing destination (if any) is left untouched on every
    failure path, and the verified bytes commit via rename only.

    Replace policy (documented): a verified download overwrites the
    destination; an unverified download never touches it, so a good
    file already at the destination survives byte-identical. When
    source and destination resolve to the same file, the file is
    verified in place and never deleted: success returns its
    identity, failure refuses with the bytes left where they were.
    """
    source = Path(zip_path)
    dest = Path(out_path)
    try:
        same = source.resolve() == dest.resolve()
    except OSError:
        same = False
    if same:
        try:
            identity = verify_release(dest, manifest=manifest)
        except ReleaseVerificationError as exc:
            raise PublicationError(
                f"downloaded bundle fails consumer verification: {exc}"
            ) from exc
        return {
            "dest": str(dest),
            "zip_sha256": sha256_bytes(dest.read_bytes()),
            "artifact_sha256": identity["artifact_sha256"],
            "members": list(identity["members"]),
        }
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise PublicationError(f"cannot read published bundle: {exc}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent), prefix=dest.name + ".tmp."
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            with os.fdopen(handle, "wb") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            identity = verify_release(tmp_path, manifest=manifest)
        except (ReleaseVerificationError, OSError) as exc:
            raise PublicationError(
                f"downloaded bundle fails consumer verification: {exc}"
            ) from exc
        os.replace(tmp_path, dest)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return {
        "dest": str(dest),
        "zip_sha256": sha256_bytes(payload),
        "artifact_sha256": identity["artifact_sha256"],
        "members": list(identity["members"]),
    }
