"""Append-only hash-chained run log.

Each event commits to the previous event's hash plus its own canonical
body, so any tampering with an earlier event breaks :meth:`RunLog.verify`.
Events record the run id, stage, status, timestamp, free-form details,
model usage, and the artifact SHA-256 when a stage produces one.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .hashing import sha256_canonical

#: Hash linked from the first event; there is no previous event to commit to.
GENESIS = "0" * 64

PathLike = str | Path


def _coerce_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {str(k): _coerce_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_jsonable(v) for v in value]
    return value


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class RunLog:
    """An append-only, hash-chained list of run events."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex
        self.events: list[dict[str, Any]] = []

    def append(
        self,
        stage: str,
        status: str,
        details: Mapping[str, Any] | None = None,
        usage: Any = None,
        artifact_sha256: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Append one event chained over the previous event hash."""
        prev_hash = self.events[-1]["event_hash"] if self.events else GENESIS
        body: dict[str, Any] = {
            "run_id": self.run_id,
            "seq": len(self.events) + 1,
            "stage": stage,
            "status": status,
            "timestamp": timestamp or utc_now_iso(),
            "details": _coerce_jsonable(dict(details or {})),
            "usage": _coerce_jsonable(usage),
            "artifact_sha256": artifact_sha256,
            "prev_hash": prev_hash,
        }
        event = {**body, "event_hash": sha256_canonical(body)}
        self.events.append(event)
        return event

    def verify(self) -> bool:
        """Recompute the chain; ``True`` only when every link is intact."""
        prev_hash = GENESIS
        for index, event in enumerate(self.events, start=1):
            if event.get("seq") != index:
                return False
            if event.get("run_id") != self.run_id:
                return False
            if event.get("prev_hash") != prev_hash:
                return False
            body = {k: v for k, v in event.items() if k != "event_hash"}
            if event.get("event_hash") != sha256_canonical(body):
                return False
            prev_hash = event["event_hash"]
        return True

    def to_json(self) -> str:
        """Serialise the event list as indented JSON."""
        return json.dumps(self.events, indent=2, sort_keys=True) + "\n"

    def to_jsonl(self) -> str:
        """Serialise the event list as JSON lines (one event per line)."""
        return "".join(
            json.dumps(event, sort_keys=True) + "\n" for event in self.events
        )

    def write_jsonl(self, path: PathLike) -> Path:
        """Write the log as JSON lines to ``path``."""
        out = Path(path)
        out.write_text(self.to_jsonl(), encoding="utf-8")
        return out

    @classmethod
    def from_jsonl(cls, payload: str) -> "RunLog":
        """Rebuild a log from JSON-lines text (chain still verifiable)."""
        events = [
            json.loads(line) for line in payload.splitlines() if line.strip()
        ]
        run_id = events[0]["run_id"] if events else None
        log = cls(run_id=run_id)
        log.events = events
        return log

    @classmethod
    def read_jsonl(cls, path: PathLike) -> "RunLog":
        """Read a log written by :meth:`write_jsonl`."""
        return cls.from_jsonl(Path(path).read_text(encoding="utf-8"))
