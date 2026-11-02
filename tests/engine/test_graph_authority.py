"""Q-GRAPH-AUTHORITY: only the engine walks declared graphs.

The engine executes exactly the capabilities the profile manifest graph
declares -- steps, bounded loops, and the human gate -- with the
caller-supplied bound. Capabilities receive a scoped context whose
public surface carries no dispatch, runner, or peer authority, and
manifest graphs naming undeclared stages are refused at load, before
any run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from engine.core.manifest import (
    ManifestError,
    load_manifest,
    validate_manifest,
)
from engine.core.runner import EXIT_FAILED, EXIT_OK, PermissionDenied, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")


class FakeModel:
    """A planner double replaying queued plans."""

    def __init__(self, plans: list[Mapping[str, Any]]) -> None:
        self._plans = [dict(plan) for plan in plans]

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=1, output_tokens=1, calls=1),
            meta={"model": "fake"},
        )


def _write_source(path: Path) -> Path:
    csv_path = path / "sample-graph.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (path / "sample-graph-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "graph-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Graph fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _payload() -> dict[str, Any]:
    return json.loads(
        (REPO_ROOT / "profiles" / "tabular" / "profile.json").read_text(
            encoding="utf-8"
        )
    )


def test_manifest_rejects_undeclared_step() -> None:
    payload = _payload()
    payload["graph"] = [{"step": "launch_missiles"}]
    with pytest.raises(ManifestError, match="unknown capability"):
        validate_manifest(payload)


def test_manifest_rejects_undeclared_loop_members() -> None:
    payload = _payload()
    payload["graph"] = [
        {
            "loop": ["generate", "summon_help"],
            "on_fail": "repair",
            "max_attempts": "max_attempts",
        }
    ]
    with pytest.raises(ManifestError, match="non-empty body"):
        validate_manifest(payload)
    payload["graph"] = [
        {
            "loop": ["generate", "validate"],
            "on_fail": "summon_help",
            "max_attempts": "max_attempts",
        }
    ]
    with pytest.raises(ManifestError, match="on_fail"):
        validate_manifest(payload)


def test_manifest_rejects_invented_node_kind() -> None:
    payload = _payload()
    payload["graph"] = [{"dispatch_to_peer": "generate"}]
    with pytest.raises(ManifestError, match="step/loop/gate"):
        validate_manifest(payload)


def test_execute_refuses_registry_missing_a_declared_stage(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path)
    registry = {name: fn for name, fn in CAPABILITIES.items() if name != "validate"}
    code, _result = execute(
        MANIFEST,
        registry,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: FakeModel([{"seed": 1, "n_rows": 100}]),
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED


def test_engine_walks_only_declared_stages(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    called: list[str] = []
    registry: dict[str, Any] = {}
    for name, fn in CAPABILITIES.items():

        def _wrapped(ctx: Any, _fn: Any = fn, _name: str = name) -> None:
            called.append(_name)
            _fn(ctx)

        registry[name] = _wrapped

    def _sleeper(ctx: Any) -> None:
        called.append("sleeper")

    registry["sleeper"] = _sleeper
    code, _result = execute(
        MANIFEST,
        registry,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: FakeModel([{"seed": 1, "n_rows": 100}]),
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    assert "sleeper" not in called
    declared = {node["step"] for node in MANIFEST.graph if "step" in node} | {
        stage
        for node in MANIFEST.graph
        if "loop" in node
        for stage in list(node["loop"]) + [node["on_fail"]]
    }
    assert set(called) <= declared


def test_capability_context_carries_no_peer_authority(tmp_path: Path) -> None:
    """The scoped context cannot reach dispatch, peers, or the runner."""
    source = _write_source(tmp_path)
    captured: dict[str, Any] = {}
    real_describe = CAPABILITIES["describe"]

    def _probe(ctx: Any) -> None:
        captured["ctx"] = ctx
        public = [name for name in dir(ctx) if not name.startswith("_")]
        for forbidden in (
            "dispatch",
            "runner",
            "engine",
            "state",
            "manifest",
            "verdict",
            "execute",
            "claim",
            "publish",
            "coordinator",
            "review",
            "delegate",
            "spawn",
            "dispatch_store",
            "runstore",
        ):
            assert forbidden not in public, forbidden
        with pytest.raises(PermissionDenied):
            ctx.model("critic")
        with pytest.raises(PermissionDenied):
            ctx.record_checks([])
        real_describe(ctx)

    registry = dict(CAPABILITIES, describe=_probe)
    code, _result = execute(
        MANIFEST,
        registry,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: FakeModel([{"seed": 1, "n_rows": 100}]),
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    assert type(captured["ctx"]).__name__ == "RunContext"


def test_manifest_graph_matches_declared_capabilities() -> None:
    """Every graph stage resolves; every capability is reachable."""
    payload = _payload()
    staged: set[str] = set()
    for node in payload["graph"]:
        if "step" in node:
            staged.add(node["step"])
        elif "loop" in node:
            staged.update(node["loop"])
            staged.add(node["on_fail"])
    assert staged <= set(payload["capabilities"])
    assert set(payload["capabilities"]) - staged == set()
