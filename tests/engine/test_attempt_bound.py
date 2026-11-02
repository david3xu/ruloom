"""Attempt-bound enforcement (B01 / IP-01): one effective bound everywhere.

The caller may declare the attempt bound top-level (``max_attempts``)
or spec-carried (``spec.limits.max_attempts``). The two declarations
must agree: a contradictory request refuses at submit. The bound is
enforced at launch (the runner refuses a spec dict contradicting the
caller bound) and at acceptance/publication (a run executed under a
larger bound than requested never ships).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from engine.artifacts.publication import PublicationError, Publisher
from engine.core.dispatch import (
    DispatchError,
    DispatchStore,
    effective_max_attempts,
)
from engine.core.manifest import load_manifest
from engine.core.runner import RunnerError, _Runner, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")

T0 = 3_000_000.0


def _store(tmp_path: Path) -> DispatchStore:
    return DispatchStore(tmp_path / "out")


def test_effective_attempt_bound_agrees_or_refuses() -> None:
    """Top-level and spec-carried attempt bounds centralise or refuse."""
    assert effective_max_attempts({"max_attempts": 3}) == 3
    assert effective_max_attempts({"spec": {"limits": {"max_attempts": 2}}}) == 2
    assert (
        effective_max_attempts(
            {"max_attempts": 3, "spec": {"limits": {"max_attempts": 3}}}
        )
        == 3
    )
    assert effective_max_attempts({}) is None
    with pytest.raises(DispatchError, match="conflicting max_attempts"):
        effective_max_attempts(
            {"max_attempts": 3, "spec": {"limits": {"max_attempts": 5}}}
        )


def test_submit_refuses_contradictory_attempt_bounds(tmp_path: Path) -> None:
    """A request declaring two attempt bounds that disagree never queues."""
    store = _store(tmp_path)
    with pytest.raises(DispatchError, match="conflicting max_attempts"):
        store.submit(
            authority="team-a",
            request={
                "profile": "tabular",
                "source": "sample-tabular.csv",
                "max_attempts": 3,
                "spec": {"limits": {"max_attempts": 5}},
            },
            now=T0,
        )
    assert store.list_items() == []


def test_runner_refuses_direct_spec_contradicting_caller_bound(
    tmp_path: Path,
) -> None:
    """Launch enforces the bound even for a pre-normalised spec dict."""
    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(RunnerError, match="max_attempts"):
        _Runner(
            manifest=MANIFEST,
            registry=dict(CAPABILITIES),
            source=source,
            max_attempts=1,
            model_factory=lambda role, config: None,
            out=tmp_path / "out",
            say=lambda message: None,
            ask=lambda prompt: "",
            spec={"limits": {"max_rows": None, "max_attempts": 5}},
        )


class _FakeModel:
    """A planner double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=1, output_tokens=1, calls=1),
            meta={"model": "fake"},
        )


def _write_source(path: Path) -> Path:
    csv_path = path / "sample-tabular.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    return csv_path


def _accepted_run(out: Path, source: Path, *, attempts: int) -> tuple[Path, Any]:
    factory = lambda role, config: _FakeModel(  # noqa: E731
        [{"seed": 1, "n_rows": 100}] * (attempts + 1)
    )
    answers = lambda prompt: (  # noqa: E731
        "accept" if prompt.startswith("Decision") else "ok"
    )
    from engine.core.runner import EXIT_OK as _OK

    code, outcome = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=attempts,
        model_factory=factory,
        out=out,
        say=lambda message: None,
        ask=answers,
    )
    assert code == _OK
    run_dir = out / "runs" / outcome["run_id"]
    assert (run_dir / "release.zip").is_file()
    return run_dir, outcome


def test_publication_refuses_run_bound_over_request(tmp_path: Path) -> None:
    """A run executed under a larger bound than requested never ships."""
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path), attempts=2)
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    request = record["run"]["request"]
    source_path = Path(request["source"])
    from engine.artifacts.hashing import sha256_bytes
    from engine.core.spec import normalize_spec

    _, spec_digest = normalize_spec(None, MANIFEST, source=source_path, max_attempts=2)
    store = DispatchStore(base)
    item = store.submit(
        authority="team-a",
        request={
            "profile": "tabular",
            "source": "sample-tabular.csv",
            "source_sha256": sha256_bytes(source_path.read_bytes()),
            "spec_digest": spec_digest,
            # Narrower than the run's executed bound of 2.
            "max_attempts": 1,
        },
        idempotency_key="attempt-narrow",
        now=T0,
    )
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    store.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=T0,
    )
    with pytest.raises(PublicationError, match="max_attempts"):
        Publisher(base, base / "published").publish(
            item["item_id"], run_dir, authority="team-a", now=T0
        )
