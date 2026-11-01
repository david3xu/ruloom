"""Strict run records owned by the engine.

These are the only facts a run may assert: what was attempted, which bytes
were produced (identified by SHA-256), what each check measured against
which threshold, what the human decided, and the append-only event chain
that binds them. Profiles supply values; the engine binds identity fields
(artifact hashes, the verifier, the decision author) mechanically.

No record names any domain: checks are ``check_id`` plus ``kind``, and the
engine never interprets what a measurement means.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {str(k): _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Artifact:
    """One stored byte payload, identified by its SHA-256."""

    sha256: str
    media_type: str
    produced_by: str
    attempt: int
    name: str = ""


@dataclass(frozen=True)
class EngineCheckResult:
    """One check outcome bound to the exact artifact hash it measured."""

    check_id: str
    kind: str
    passed: bool
    measured: Any
    threshold: Any
    threshold_source: str
    artifact_sha256: str
    verifier: str
    detail: str = ""


@dataclass
class Attempt:
    """One pass through a loop body: plan, artifacts, checks, status."""

    n: int
    plan_ref: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    check_results: list[EngineCheckResult] = field(default_factory=list)
    status: str = ""

    @property
    def failed(self) -> list[str]:
        """Ids of the checks that did not pass on this attempt."""
        return [r.check_id for r in self.check_results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "plan_ref": _jsonable(self.plan_ref),
            "artifact_refs": list(self.artifact_refs),
            "check_results": [_jsonable(r) for r in self.check_results],
            "status": self.status,
        }


@dataclass
class Run:
    """Identity and bounds for one engine execution."""

    id: str
    profile_ref: str
    profile_version: int
    profile_digest: str
    request: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile_ref": self.profile_ref,
            "profile_version": self.profile_version,
            "profile_digest": self.profile_digest,
            "request": _jsonable(self.request),
            "limits": _jsonable(self.limits),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Decision:
    """A human verdict with the note recorded exactly as typed."""

    verdict: str
    note: str
    decided_by: str
    at: str = ""

    def with_time(self) -> "Decision":
        """Return a copy stamped with the current UTC time."""
        if self.at:
            return self
        return Decision(
            verdict=self.verdict,
            note=self.note,
            decided_by=self.decided_by,
            at=utc_now_iso(),
        )
