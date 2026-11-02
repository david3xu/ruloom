"""Durable dispatch: submit once, lease to one worker, fence the stale.

A dispatch item is a durable record of work the engine should do --
which profile to run, over which source, under which caller-supplied
bounds -- held in a local store beside the per-run history
(``<base>/dispatch/``). The launcher process may die at any point;
the records survive on disk, so a fresh launcher re-reads them and
continues: expired leases return to the queue, and identical
submissions collapse onto the first record instead of duplicating
work.

Concurrency contract (local store): within one launcher process every
read-modify-write runs under a per-store lock, so two threads racing
to claim the same item produce exactly one winner. Across processes
every record write is atomic (temporary file plus rename), so a crash
mid-write leaves the previous record intact; two *live* launchers
writing at once are out of scope -- the recovery story covers a dead
launcher replaced by a fresh one, never two writers at once.

Lease and fencing: ``claim`` grants the item to one worker with a
monotonic fencing token and an expiry. Every lease-holder operation
(heartbeat, success/failure report) must present the current token;
anything stale -- a previous lease, a superseded worker, a cancelled
item -- is refused with :class:`FencingError`, so a worker that lost
its lease can never complete, fail, or extend work it no longer owns.
Each new claim bumps the token, and cancellation bumps it too, which
fences off a worker still running after cancel.

Authority: the submitting authority owns the item. Cancellation and
publication (see ``engine.artifacts.publication``) require that
authority; any other authority is refused with
:class:`AuthorityDenied`. Retries are bounded: each reported failure
and each expired lease (via the expired-claim reclaim or the recovery
sweep) consumes one retry from ``max_retries``; the launch count rides
in ``attempts`` separately from the failure count. Until the budget is
spent the item returns to the queue, then it rests terminal as
``exhausted`` (expiry exhaustion records
``retries_exhausted_by_expiry``) and is never claimed again.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..artifacts.hashing import sha256_bytes, sha256_canonical

#: Directory (under the store base) holding the dispatch records.
STORE_DIR_NAME = "dispatch"

#: One JSON record per submitted item.
ITEMS_DIR_NAME = "items"

#: Idempotency-key index: one tiny file per key, created exclusively.
KEYS_DIR_NAME = "keys"

#: Canonical submitter-supplied item ids: start alphanumerical, then
#: letters, digits, dash or underscore, at most 64 characters. The
#: shape excludes separators, dots and absolute paths, so an id can
#: never address a record outside the store's own items directory.
ITEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def check_item_id(item_id: Any) -> str:
    """Validate a caller-supplied item id, or refuse loudly.

    Every public entry point taking an item id runs this before any
    I/O. Anything outside the canonical shape -- separators,
    backslashes, dots, absolute paths, empty or overlong values --
    is refused instead of being interpolated into a record path.
    """
    if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id):
        raise DispatchError(
            f"dispatch item id {item_id!r} is outside the canonical "
            "form (letters, digits, dash, underscore; at most 64): "
            "refusing"
        )
    return item_id


def _request_digest(request: Any) -> str:
    """Canonical digest of a submit request (collision comparison)."""
    if not isinstance(request, Mapping):
        return sha256_canonical({"__invalid__": repr(request)})
    return sha256_canonical(dict(request))


#: Top-level fields a submit request may declare. Anything else is
#: refused at submit: publication binds every declared bound, so an
#: unknown field could never be enforced and must never be silently
#: kept.
SUPPORTED_REQUEST_KEYS = (
    "profile",
    "source",
    "source_sha256",
    "spec",
    "spec_digest",
    "max_attempts",
    "max_rows",
)

#: Lowercase hex digests only (mirrors the consumer checksum rule).
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _check_pin(value: Any, *, field: str) -> str | None:
    """Validate an optional 64-hex digest pin, or refuse loudly."""
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise DispatchError(
            f"request {field} must be a 64-character lowercase hex "
            f"digest, got {value!r}: refusing"
        )
    return value


def _check_bound(value: Any, *, field: str) -> int | None:
    """Validate an optional positive-integer caller bound, or refuse."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DispatchError(
            f"request {field} must be a positive integer, got "
            f"{value!r}: refusing"
        )
    return value


def effective_max_rows(request: Mapping[str, Any]) -> int | None:
    """The row bound a request declares, top-level or spec-carried.

    A request may carry its row bound directly (``max_rows``) or inside
    a GenerationSpec overlay (``spec.limits.max_rows``). Conflicting
    declarations are refused: two bounds that disagree can never both
    be enforced, so neither is silently picked.
    """
    top = request.get("max_rows")
    nested: Any = None
    spec = request.get("spec")
    if spec is not None:
        if not isinstance(spec, Mapping):
            raise DispatchError(
                f"request 'spec' must be a mapping, got {spec!r}: refusing"
            )
        limits = spec.get("limits")
        if limits is not None:
            if not isinstance(limits, Mapping):
                raise DispatchError(
                    f"request spec 'limits' must be a mapping, got "
                    f"{limits!r}: refusing"
                )
            if "max_rows" in limits:
                nested = _check_bound(
                    limits.get("max_rows"), field="spec 'limits.max_rows'"
                )
    if top is not None and nested is not None and top != nested:
        raise DispatchError(
            f"request declares conflicting max_rows bounds ({top!r} "
            f"and {nested!r}): refusing"
        )
    return top if top is not None else nested


def _normalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the declared request and project its bound identity.

    Every top-level field must be supported (see
    ``SUPPORTED_REQUEST_KEYS``); pins must be 64-hex digests and bounds
    positive integers. The bound identity is the triple publication
    enforces: the profile, the pinned source-content digest, and the
    pinned normalised spec digest. Unpinned components persist as None
    and can never match a real run's measured values, so a request
    that pins nothing shippable refuses at publication instead of
    shipping on profile-name equality alone.
    """
    for key in request:
        if key not in SUPPORTED_REQUEST_KEYS:
            raise DispatchError(
                f"request declares unsupported request field {key!r}: "
                "refusing (never silently unenforce a bound)"
            )
    profile = request.get("profile")
    if profile is not None and (
        not isinstance(profile, str) or not profile
    ):
        raise DispatchError(
            "request 'profile' must be a non-empty string: refusing"
        )
    source = request.get("source")
    if source is not None and (
        not isinstance(source, str) or not source
    ):
        raise DispatchError(
            "request 'source' must be a non-empty string: refusing"
        )
    spec = request.get("spec")
    if spec is not None and not isinstance(spec, Mapping):
        raise DispatchError(
            f"request 'spec' must be a mapping, got {spec!r}: refusing"
        )
    _check_bound(request.get("max_attempts"), field="'max_attempts'")
    _check_bound(request.get("max_rows"), field="'max_rows'")
    effective_max_rows(request)
    return {
        "profile": profile,
        "source_sha256": _check_pin(
            request.get("source_sha256"), field="'source_sha256'"
        ),
        "spec_digest": _check_pin(
            request.get("spec_digest"), field="'spec_digest'"
        ),
    }


#: Item states. ``queued`` is actionable; ``claimed`` is leased to one
#: worker; ``succeeded``, ``exhausted`` and ``cancelled`` are terminal.
QUEUED = "queued"
CLAIMED = "claimed"
SUCCEEDED = "succeeded"
EXHAUSTED = "exhausted"
CANCELLED = "cancelled"

#: Exhaustion reason recorded when expiry consumes the retry budget.
#: Both the expired-claim reclaim path and the recovery sweep count
#: an expired lease against ``max_retries``; once spent the item rests
#: terminal as ``exhausted`` with this reason instead of requeueing,
#: so repeated crashes cannot trigger unbounded generation cost.
EXHAUSTED_BY_EXPIRY = "retries_exhausted_by_expiry"

TERMINAL_STATUSES = (SUCCEEDED, EXHAUSTED, CANCELLED)


class DispatchError(Exception):
    """The dispatch request was invalid or the item is unusable."""


class ItemNotFound(DispatchError):
    """No dispatch item with that id exists in this store."""


class ClaimConflict(DispatchError):
    """The item cannot be claimed now (held live, or terminal)."""


class FencingError(DispatchError):
    """The presented lease token is stale: the caller owns nothing."""


class AuthorityDenied(DispatchError):
    """A foreign authority reached for an item it does not own."""


_locks_guard = threading.Lock()
_store_locks: dict[str, threading.Lock] = {}


def _store_lock(root: Path) -> threading.Lock:
    """The single in-process lock for one store directory."""
    key = str(root)
    with _locks_guard:
        lock = _store_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _store_locks[key] = lock
        return lock


def _resolve_now(now: float | None) -> float:
    """Use the injected clock in tests, the wall clock in production."""
    return time.time() if now is None else float(now)


def _atomic_write_json(path: Path, record: Mapping[str, Any]) -> Path:
    """Write one JSON record atomically (temporary file plus rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(dict(record), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
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


def _copy_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy through JSON, so callers never alias store state."""
    return json.loads(json.dumps(dict(record)))


def _key_path(keys_dir: Path, idempotency_key: str) -> Path:
    """The index file for one idempotency key (keys hash to file names)."""
    return keys_dir / (sha256_bytes(idempotency_key.encode("utf-8")) + ".json")


class DispatchStore:
    """Filesystem backing for durable dispatch records.

    ``base`` is the same output root the runs live under, so the
    dispatch store sits beside the per-run history it coordinates
    (``<base>/dispatch/``). Every mutation rewrites one small JSON
    record atomically and appends a history entry naming the
    transition, so the record on disk always tells how the item got
    where it is.
    """

    def __init__(self, base: Path | str) -> None:
        self.base = Path(base)
        self.dir = self.base / STORE_DIR_NAME
        self.items_dir = self.dir / ITEMS_DIR_NAME
        self.keys_dir = self.dir / KEYS_DIR_NAME
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.keys_dir.mkdir(parents=True, exist_ok=True)

    def _item_path(self, item_id: str) -> Path:
        """The record path for one id, contained inside the store."""
        clean = check_item_id(item_id)
        path = self.items_dir / f"{clean}.json"
        if path.resolve().parent != self.items_dir.resolve():
            raise DispatchError(
                f"dispatch item id {item_id!r} resolves outside "
                "its store: refusing"
            )
        return path

    def _read(self, item_id: str) -> dict[str, Any]:
        """Load one record, or raise :class:`ItemNotFound`."""
        check_item_id(item_id)
        try:
            return json.loads(self._item_path(item_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ItemNotFound(f"no dispatch item {item_id!r} in this store") from exc

    def _write(
        self, record: Mapping[str, Any], *, event: str, now: float, **detail: Any
    ) -> dict[str, Any]:
        """Append history, stamp, persist, and return a copy."""
        stored = _copy_record(record)
        stored["updated_at"] = now
        history = stored.get("history")
        if not isinstance(history, list):
            history = []
            stored["history"] = history
        history.append({"at": now, "event": event, **detail})
        _atomic_write_json(self._item_path(str(stored["item_id"])), stored)
        return _copy_record(stored)

    # -- submission --------------------------------------------------------

    def submit(
        self,
        *,
        authority: str,
        request: Mapping[str, Any],
        idempotency_key: str | None = None,
        max_retries: int = 0,
        item_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record one unit of work, idempotently.

        With ``idempotency_key`` set, a second submit carrying the same
        key returns the first submit's record unchanged -- no duplicate
        item is created. A key first seen under another authority is
        refused, so one authority cannot squat or adopt another's key.
        ``max_retries`` bounds how many reported failures *and*
        expired leases return the item to the queue before it rests
        ``exhausted`` (expiry consumes the same budget; the launch
        count persists separately in ``attempts``).

        An explicit ``item_id`` is create-only and collision-safe: the
        first submit creates the item, and a later submit naming the
        same id returns the existing record unchanged -- same owning
        authority and same request -- without touching its history or
        fencing epoch. A changed request or a foreign authority is
        refused loudly, so no resubmission can rewrite another
        owner's history or reset its fencing.

        The declared request is validated (supported fields only, pins
        as 64-hex digests, bounds as positive integers) and its bound
        identity -- profile, pinned source-content digest, pinned
        normalised spec digest -- is persisted as ``request_identity``.
        Publication later requires that identity to match the run's
        accepted and delivered identities, so a request that pins
        nothing shippable can never ship on profile-name equality
        alone.
        """
        if not isinstance(authority, str) or not authority.strip():
            raise DispatchError("submit requires a non-empty authority")
        if not isinstance(request, Mapping) or not dict(request):
            raise DispatchError("submit requires a non-empty request mapping")
        if (
            not isinstance(max_retries, int)
            or isinstance(max_retries, bool)
            or max_retries < 0
        ):
            raise DispatchError("max_retries must be a non-negative integer")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key
        ):
            raise DispatchError("idempotency_key must be a non-empty string")
        if item_id is not None:
            check_item_id(item_id)
        at = _resolve_now(now)
        identity = _normalize_request(request)
        with _store_lock(self.dir):
            if idempotency_key is not None:
                return self._submit_keyed(
                    authority=authority,
                    request=request,
                    request_identity=identity,
                    idempotency_key=idempotency_key,
                    max_retries=max_retries,
                    item_id=item_id,
                    now=at,
                )
            if item_id is not None:
                return self._submit_explicit(
                    authority=authority,
                    request=request,
                    request_identity=identity,
                    item_id=item_id,
                    max_retries=max_retries,
                    now=at,
                )
            return self._write(
                {
                    "item_id": uuid.uuid4().hex,
                    "authority": authority,
                    "request": dict(request),
                    "request_identity": dict(identity),
                    "idempotency_key": None,
                    "status": QUEUED,
                    "retries_used": 0,
                    "attempts": 0,
                    "max_retries": max_retries,
                    "lease": None,
                    "fencing": 0,
                    "accepted_run": None,
                    "error": None,
                    "history": [],
                    "created_at": at,
                    "updated_at": at,
                },
                event="submitted",
                now=at,
            )

    def _adopt_existing(
        self, authority: str, request: Mapping[str, Any], item_id: str
    ) -> dict[str, Any]:
        """Return the immutable record for a colliding explicit id.

        The stored authority and request digest must both match the
        caller; anything else is a foreign or changed resubmission and
        is refused. The record is returned unchanged: no history
        event is appended and the fencing epoch never moves.
        """
        try:
            existing = json.loads(
                self._item_path(item_id).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise DispatchError(
                f"dispatch item {item_id!r} is recorded but "
                "unreadable: refusing (never duplicate blindly)"
            ) from exc
        if (
            not isinstance(existing, Mapping)
            or existing.get("item_id") != item_id
        ):
            raise DispatchError(
                f"dispatch item {item_id!r} carries a corrupt "
                "record: refusing"
            )
        if existing.get("authority") != authority:
            raise AuthorityDenied(
                f"dispatch item {item_id!r} belongs to another "
                "authority: refusing"
            )
        if _request_digest(existing.get("request")) != _request_digest(request):
            raise DispatchError(
                f"dispatch item {item_id!r} already exists with a "
                "different request: refusing (explicit ids are "
                "create-only)"
            )
        return _copy_record(existing)

    def _submit_explicit(
        self,
        *,
        authority: str,
        request: Mapping[str, Any],
        request_identity: Mapping[str, Any],
        item_id: str,
        max_retries: int,
        now: float,
    ) -> dict[str, Any]:
        """Create one explicitly named item, or adopt it unchanged."""
        if self._item_path(item_id).is_file():
            return self._adopt_existing(authority, request, item_id)
        return self._write(
            {
                "item_id": item_id,
                "authority": authority,
                "request": dict(request),
                "request_identity": dict(request_identity),
                "idempotency_key": None,
                "status": QUEUED,
                "retries_used": 0,
                    "attempts": 0,
                "max_retries": max_retries,
                "lease": None,
                "fencing": 0,
                "accepted_run": None,
                "error": None,
                "history": [],
                "created_at": now,
                "updated_at": now,
            },
            event="submitted",
            now=now,
        )

    def _submit_keyed(
        self,
        *,
        authority: str,
        request: Mapping[str, Any],
        request_identity: Mapping[str, Any],
        idempotency_key: str,
        max_retries: int,
        item_id: str | None,
        now: float,
    ) -> dict[str, Any]:
        """Submit guarded by an exclusively created key file (one winner)."""
        key_path = _key_path(self.keys_dir, idempotency_key)
        try:
            with open(key_path, "x", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "item_id": item_id or uuid.uuid4().hex,
                            "authority": authority,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        except FileExistsError:
            try:
                seen = json.loads(key_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DispatchError(
                    f"idempotency key {idempotency_key!r} is recorded "
                    "but unreadable: refusing (never duplicate blindly)"
                ) from exc
            if (
                not isinstance(seen, Mapping)
                or seen.get("authority") != authority
            ):
                raise AuthorityDenied(
                    f"idempotency key {idempotency_key!r} belongs to "
                    "another authority: refusing"
                )
            stored_id = seen.get("item_id")
            check_item_id(stored_id)
            try:
                return self._read(stored_id)
            except ItemNotFound:
                pass  # The first submit died before writing its item.
            return self._write(
                {
                    "item_id": stored_id,
                    "authority": authority,
                    "request": dict(request),
                    "request_identity": dict(request_identity),
                    "idempotency_key": idempotency_key,
                    "status": QUEUED,
                    "retries_used": 0,
                    "attempts": 0,
                    "max_retries": max_retries,
                    "lease": None,
                    "fencing": 0,
                    "accepted_run": None,
                    "error": None,
                    "history": [],
                    "created_at": now,
                    "updated_at": now,
                },
                event="submitted",
                now=now,
            )
        seen = json.loads(key_path.read_text(encoding="utf-8"))
        chosen = str(seen["item_id"])
        if item_id is not None and self._item_path(chosen).is_file():
            # A fresh key naming an existing item: the explicit-id
            # collision rule applies (same authority and request adopt
            # it unchanged; anything else refuses).
            return self._adopt_existing(authority, request, chosen)
        return self._write(
            {
                "item_id": chosen,
                "authority": authority,
                "request": dict(request),
                "request_identity": dict(request_identity),
                "idempotency_key": idempotency_key,
                "status": QUEUED,
                "retries_used": 0,
                    "attempts": 0,
                "max_retries": max_retries,
                "lease": None,
                "fencing": 0,
                "accepted_run": None,
                "error": None,
                "history": [],
                "created_at": now,
                "updated_at": now,
            },
            event="submitted",
            now=now,
        )

    # -- claiming and leases ------------------------------------------------

    def claim(
        self,
        item_id: str,
        *,
        worker: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Lease one queued item to one worker (single winner).

        A live lease blocks every other claimant with
        :class:`ClaimConflict`. An expired lease consumes one retry
        from the budget: while ``retries_used`` stays within
        ``max_retries`` the fresh claim wins, the token moves on, the
        launch count (``attempts``) grows by one, and the history
        records the recovery, so a dead launcher's work resumes
        instead of wedging. Once the budget is spent the item rests
        terminal as ``exhausted`` with reason
        ``retries_exhausted_by_expiry`` and the reclaim refuses with
        :class:`ClaimConflict`. Terminal items are never claimable.
        """
        if not isinstance(worker, str) or not worker.strip():
            raise DispatchError("claim requires a non-empty worker id")
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise DispatchError("lease_seconds must be a positive number")
        at = _resolve_now(now)
        with _store_lock(self.dir):
            record = self._read(item_id)
            status = record.get("status")
            if status in TERMINAL_STATUSES:
                raise ClaimConflict(
                    f"item {item_id!r} is terminal ({status}): nothing to claim"
                )
            if status == CLAIMED:
                lease = record.get("lease") or {}
                if at < float(lease.get("expires_at", 0.0)):
                    raise ClaimConflict(
                        f"item {item_id!r} is leased to "
                        f"{lease.get('worker')!r} until "
                        f"{lease.get('expires_at')}: refusing"
                    )
                record["lease"] = None
                record["retries_used"] = int(record.get("retries_used", 0)) + 1
                if int(record["retries_used"]) > int(record.get("max_retries", 0)):
                    record["status"] = EXHAUSTED
                    record["error"] = EXHAUSTED_BY_EXPIRY
                    self._write(
                        record,
                        event="exhausted",
                        now=at,
                        worker=lease.get("worker"),
                        reason=EXHAUSTED_BY_EXPIRY,
                    )
                    raise ClaimConflict(
                        f"item {item_id!r} exhausted its retry budget "
                        f"through expiry ({EXHAUSTED_BY_EXPIRY}): "
                        "nothing to claim"
                    )
                record = self._write(
                    record,
                    event="lease_expired",
                    now=at,
                    worker=lease.get("worker"),
                )
            elif status != QUEUED:
                raise DispatchError(
                    f"item {item_id!r} carries unknown status "
                    f"{status!r}: refusing"
                )
            token = int(record.get("fencing", 0)) + 1
            record["fencing"] = token
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["status"] = CLAIMED
            record["lease"] = {
                "worker": worker,
                "token": token,
                "expires_at": at + float(lease_seconds),
            }
            return self._write(record, event="claimed", now=at, worker=worker)

    def _live_lease(
        self, record: Mapping[str, Any], *, worker: str, token: Any, now: float
    ) -> dict[str, Any]:
        """Return the lease when it is live and owned, else refuse loudly."""
        if record.get("status") != CLAIMED:
            raise FencingError(
                f"item {record.get('item_id')!r} is not leased "
                f"(status {record.get('status')!r}): the caller owns nothing"
            )
        lease = record.get("lease")
        if not isinstance(lease, Mapping):
            raise FencingError(
                f"item {record.get('item_id')!r} carries no lease: refusing"
            )
        if lease.get("worker") != worker or lease.get("token") != token:
            raise FencingError(
                f"stale lease for item {record.get('item_id')!r}: a newer "
                "claim or a cancellation moved the token on"
            )
        if now >= float(lease.get("expires_at", 0.0)):
            raise FencingError(
                f"lease for item {record.get('item_id')!r} expired: "
                "the work was re-dispatched"
            )
        return dict(lease)

    def heartbeat(
        self,
        item_id: str,
        *,
        worker: str,
        token: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Extend a live lease; anything stale is refused."""
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise DispatchError("lease_seconds must be a positive number")
        at = _resolve_now(now)
        with _store_lock(self.dir):
            record = self._read(item_id)
            self._live_lease(record, worker=worker, token=token, now=at)
            lease = dict(record["lease"])
            lease["expires_at"] = at + float(lease_seconds)
            record["lease"] = lease
            return self._write(record, event="heartbeat", now=at, worker=worker)

    # -- execution reports ---------------------------------------------------

    def report_success(
        self,
        item_id: str,
        *,
        worker: str,
        token: int,
        accepted_run: Mapping[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Close a leased item as succeeded with its accepted run binding.

        ``accepted_run`` names the run the worker finished
        (``run_id``, ``artifact_sha256``, ``zip_sha256``); publication
        later re-checks those digests against the run directory, so a
        report pointing at the wrong bytes can never ship.
        """
        if not isinstance(accepted_run, Mapping):
            raise DispatchError("report_success requires an accepted_run mapping")
        for field in ("run_id", "artifact_sha256", "zip_sha256"):
            value = accepted_run.get(field)
            if not isinstance(value, str) or not value:
                raise DispatchError(
                    f"accepted_run must carry a non-empty {field!r}"
                )
        at = _resolve_now(now)
        with _store_lock(self.dir):
            record = self._read(item_id)
            self._live_lease(record, worker=worker, token=token, now=at)
            record["status"] = SUCCEEDED
            record["lease"] = None
            record["accepted_run"] = dict(accepted_run)
            record["error"] = None
            return self._write(record, event="succeeded", now=at, worker=worker)

    def report_failure(
        self,
        item_id: str,
        *,
        worker: str,
        token: int,
        error: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return a leased item to the queue, or rest it when spent.

        Each failure spends one retry: while ``retries_used`` stays
        within ``max_retries`` the item returns to ``queued`` for a
        fresh claim; once the bound is spent the item rests terminal
        as ``exhausted`` and is never claimed again. The token moves
        on either way, so the reporting worker owns nothing further.
        """
        at = _resolve_now(now)
        with _store_lock(self.dir):
            record = self._read(item_id)
            self._live_lease(record, worker=worker, token=token, now=at)
            record["lease"] = None
            record["retries_used"] = int(record.get("retries_used", 0)) + 1
            record["error"] = str(error)
            if int(record["retries_used"]) > int(record.get("max_retries", 0)):
                record["status"] = EXHAUSTED
                return self._write(record, event="exhausted", now=at, worker=worker)
            record["status"] = QUEUED
            return self._write(record, event="requeued", now=at, worker=worker)

    # -- cancellation and recovery --------------------------------------------

    def cancel(
        self, item_id: str, *, authority: str, now: float | None = None
    ) -> dict[str, Any]:
        """Cancel an actionable item; the owning authority only.

        Cancelling a leased item clears the lease and moves the token
        on, so a worker still running after cancel finds its next
        report fenced off. Cancelling an already-cancelled item is a
        no-op returning the record; reaching for a terminal item, or
        for another authority's item, is refused loudly.
        """
        at = _resolve_now(now)
        with _store_lock(self.dir):
            record = self._read(item_id)
            if record.get("authority") != authority:
                raise AuthorityDenied(
                    f"item {item_id!r} belongs to another authority: refusing"
                )
            status = record.get("status")
            if status == CANCELLED:
                return _copy_record(record)
            if status in (SUCCEEDED, EXHAUSTED):
                raise DispatchError(
                    f"item {item_id!r} is terminal ({status}): "
                    "nothing to cancel"
                )
            if status not in (QUEUED, CLAIMED):
                raise DispatchError(
                    f"item {item_id!r} carries unknown status "
                    f"{status!r}: refusing"
                )
            record["status"] = CANCELLED
            record["lease"] = None
            record["fencing"] = int(record.get("fencing", 0)) + 1
            return self._write(record, event="cancelled", now=at)

    def recover(
        self, item_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """Return an expired lease to the queue (launcher-outage sweep).

        A fresh launcher runs this over claimed items after an outage:
        leases whose time has passed consume one retry from the
        budget and go back to ``queued`` with the token untouched
        (the next claim moves it on and counts the launch in
        ``attempts``). Once the budget is spent the item rests
        terminal as ``exhausted`` with reason
        ``retries_exhausted_by_expiry`` instead of requeueing.
        Anything else -- live leases, actionable or terminal items --
        is returned unchanged, so a sweep is safe to run blindly over
        the whole store.
        """
        at = _resolve_now(now)
        with _store_lock(self.dir):
            record = self._read(item_id)
            if record.get("status") != CLAIMED:
                return _copy_record(record)
            lease = record.get("lease") or {}
            if at < float(lease.get("expires_at", 0.0)):
                return _copy_record(record)
            record["lease"] = None
            record["retries_used"] = int(record.get("retries_used", 0)) + 1
            if int(record["retries_used"]) > int(record.get("max_retries", 0)):
                record["status"] = EXHAUSTED
                record["error"] = EXHAUSTED_BY_EXPIRY
                return self._write(
                    record,
                    event="exhausted",
                    now=at,
                    worker=lease.get("worker"),
                    reason=EXHAUSTED_BY_EXPIRY,
                )
            record["status"] = QUEUED
            return self._write(
                record, event="recovered", now=at, worker=lease.get("worker")
            )

    # -- reads ------------------------------------------------------------------

    def get(self, item_id: str) -> dict[str, Any]:
        """Return a copy of one record (callers never alias store state)."""
        with _store_lock(self.dir):
            return _copy_record(self._read(item_id))

    def list_items(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """Every record, optionally filtered to one status, id-sorted."""
        with _store_lock(self.dir):
            out: list[dict[str, Any]] = []
            for path in sorted(self.items_dir.glob("*.json")):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(record, Mapping):
                    continue
                if status is not None and record.get("status") != status:
                    continue
                out.append(_copy_record(record))
            return out
