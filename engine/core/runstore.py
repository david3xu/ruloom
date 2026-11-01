"""Durable per-run history: one directory per execution.

Every execution gets a unique run id and its own directory under
``runs/<run-id>/``. The directory holds the run record (``run.json``),
the hash-chained event export (``events.jsonl``, plus a ``run.jsonl``
copy under the legacy name existing readers expect), one
``attempts/attempt-<n>/`` directory per loop attempt with
``plan.json``, ``artifacts.json``, ``checks.json`` and ``receipt.json``,
the human ``decision.json`` once the gate decides, the packaged
evidence for the selected attempt, and ``release.zip`` only when the
human accepts. A rejected or failed run leaves no release reference in
its own directory, and one run never overwrites another run's history:
run ids are unique per execution.

Content-addressed payload cache: ``runs/<payload-sha>/`` keeps the raw
payload bytes (``data.csv``, plus ``synthetic.png`` when the run
renders one) so readers that resolve bytes by hash keep working. Only
payload bytes live there -- plans, logs, decisions, and releases are
run-scoped and live only under ``runs/<run-id>/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .records import Attempt, Run, utc_now_iso


def _write_json(path: Path, payload: Any) -> Path:
    """Write ``payload`` as sorted, indented JSON plus a trailing newline."""
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _append_json_line(path: Path, event: Mapping[str, Any]) -> None:
    """Append one event as a single sorted JSON line."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), sort_keys=True) + "\n")


class RunStore:
    """Filesystem backing for one run's durable history.

    The directory is created eagerly at run construction -- before
    intake runs -- so even a failure in the first capability leaves a
    durable record. Every later event is appended write-through via
    :meth:`append_event` (registered as the run-log sink), and
    :meth:`update_run` marks the terminal status exactly once.
    """

    def __init__(self, out: Path | str, run: Run) -> None:
        self.run = run
        self.run_dir = Path(out) / "runs" / run.id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.legacy_log_path = self.run_dir / "run.jsonl"
        _write_json(
            self.run_dir / "run.json",
            {
                "run": run.to_dict(),
                "status": "running",
                "error": None,
                "attempts": 0,
                "artifact_sha256": None,
                "source_sha256": None,
                "decision": None,
                "finished_at": None,
            },
        )

    def append_event(self, event: Mapping[str, Any]) -> None:
        """Persist one run-log event to both log exports."""
        _append_json_line(self.events_path, event)
        _append_json_line(self.legacy_log_path, event)

    def write_attempt(
        self, attempt: Attempt, artifacts: list[dict[str, Any]]
    ) -> Path:
        """Persist one immutable attempt record under ``attempts/``."""
        attempt_dir = self.run_dir / "attempts" / f"attempt-{attempt.n}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        _write_json(attempt_dir / "plan.json", attempt.plan_ref)
        _write_json(attempt_dir / "artifacts.json", list(artifacts))
        _write_json(
            attempt_dir / "checks.json",
            [
                {
                    "check_id": result.check_id,
                    "kind": result.kind,
                    "status": result.status,
                    "measured": result.measured,
                    "threshold": result.threshold,
                    "threshold_source": result.threshold_source,
                    "artifact_sha256": result.artifact_sha256,
                    "verifier": result.verifier,
                    "attempt": result.attempt,
                    "detail": result.detail,
                    "band_position": result.band_position,
                }
                for result in attempt.check_results
            ],
        )
        _write_json(
            attempt_dir / "receipt.json",
            {
                "run_id": self.run.id,
                "attempt": attempt.n,
                "verdict": attempt.status,
                "failed": list(attempt.failed),
                "seed": attempt.seed,
                "tokens": attempt.tokens,
                "manifest_digest": attempt.manifest_digest,
                "source_sha256": attempt.source_sha256,
                "artifact_refs": list(attempt.artifact_refs),
                "finished_at": utc_now_iso(),
            },
        )
        return attempt_dir

    def write_decision(self, decision: Mapping[str, Any]) -> Path:
        """Persist the human gate decision verbatim."""
        return _write_json(self.run_dir / "decision.json", dict(decision))

    def sync_payload_cache(
        self, payload_sha: str, files: Mapping[str, bytes]
    ) -> Path:
        """Mirror payload bytes into the content-addressed cache.

        ``runs/<payload-sha>/`` carries payload bytes only (never plans,
        logs, decisions, or releases). Identical bytes written twice are
        a no-op; the per-run history is never stored here.
        """
        cache_dir = self.run_dir.parent / payload_sha
        cache_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in files.items():
            (cache_dir / name).write_bytes(bytes(payload))
        return cache_dir

    def update_run(
        self,
        status: str,
        *,
        error: str | None = None,
        attempts: int = 0,
        artifact_sha256: str | None = None,
        source_sha256: str | None = None,
        decision: Mapping[str, Any] | None = None,
    ) -> Path:
        """Rewrite ``run.json`` with the terminal status of the run."""
        return _write_json(
            self.run_dir / "run.json",
            {
                "run": self.run.to_dict(),
                "status": status,
                "error": error,
                "attempts": attempts,
                "artifact_sha256": artifact_sha256,
                "source_sha256": source_sha256,
                "decision": (
                    dict(decision) if decision is not None else None
                ),
                "finished_at": utc_now_iso(),
            },
        )
