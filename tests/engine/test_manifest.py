"""Tests for declarative profile manifests (engine/manifest.py)."""

from __future__ import annotations

import pytest

from engine.core.manifest import (
    ENGINE_API,
    ManifestError,
    load_manifest,
    validate_manifest,
)

def _base_manifest() -> dict:
    return {
        "id": "fake",
        "version": 1,
        "engine_api": ENGINE_API,
        "source": {"reader": "fake.reader"},
        "capabilities": {
            "intake": {"impl": "fake.intake", "reads_raw_source": True},
            "decide": {"impl": "fake.decide", "calls_model": "planner"},
            "judge": {"impl": "fake.judge", "writes_verdict": True},
        },
        "graph": [
            {"step": "intake"},
            {
                "loop": ["decide", "judge"],
                "on_fail": "decide",
                "max_attempts": "max_attempts",
            },
            {"gate": "human_review"},
        ],
        "checks": [
            {
                "id": "shape_ok",
                "kind": "constraint",
                "threshold": {"source": "literal"},
            }
        ],
        "models": {"roles": {"planner": {"model": "fake-model"}}},
        "limits": {},
    }


def test_real_manifests_load() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for profile_id in ("hylogger", "tabular"):
        manifest = load_manifest(root / "profiles" / profile_id / "profile.json")
        assert manifest.id == profile_id
        assert manifest.engine_api == ENGINE_API
        assert manifest.digest


def test_graph_uses_only_three_node_kinds() -> None:
    manifest = validate_manifest(_base_manifest())
    kinds = set()
    for node in manifest.graph:
        kinds.update(k for k in node if k in ("step", "loop", "gate"))
    assert kinds == {"step", "loop", "gate"}


def test_fourth_node_kind_rejected() -> None:
    payload = _base_manifest()
    payload["graph"] = [{"branch": "intake"}]
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_loop_without_binding_rejected() -> None:
    payload = _base_manifest()
    payload["graph"] = [
        {"loop": ["decide", "judge"], "on_fail": "decide"},
    ]
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_loop_with_numeric_default_rejected() -> None:
    payload = _base_manifest()
    payload["graph"] = [
        {
            "loop": ["decide", "judge"],
            "on_fail": "decide",
            "max_attempts": 3,
        },
    ]
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_unknown_capability_rejected() -> None:
    payload = _base_manifest()
    payload["graph"] = [{"step": "missing"}]
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_unknown_permission_rejected() -> None:
    payload = _base_manifest()
    payload["capabilities"]["intake"]["grants_admin"] = True
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_critic_policy_declarations_load() -> None:
    payload = _base_manifest()
    payload["capabilities"]["note"] = {
        "impl": "fake.note",
        "advisory": True,
        "critic_policy": "optional",
    }
    payload["capabilities"]["judge"]["critic_policy"] = "required"
    with pytest.raises(ManifestError):
        # judge is not advisory: policy declarations need the seat.
        validate_manifest(payload)
    payload["capabilities"]["judge"].pop("critic_policy")
    payload["capabilities"]["judge"]["advisory"] = True
    payload["capabilities"]["judge"]["critic_policy"] = "required"
    manifest = validate_manifest(payload)
    assert manifest.capabilities["note"]["critic_policy"] == "optional"
    assert manifest.capabilities["judge"]["critic_policy"] == "required"


def test_unknown_critic_policy_rejected() -> None:
    payload = _base_manifest()
    payload["capabilities"]["note"] = {
        "impl": "fake.note",
        "advisory": True,
        "critic_policy": "sometimes",
    }
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_critic_policy_on_non_advisory_rejected() -> None:
    payload = _base_manifest()
    payload["capabilities"]["decide"]["critic_policy"] = "optional"
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_engine_api_mismatch_rejected() -> None:
    payload = _base_manifest()
    payload["engine_api"] = ENGINE_API + 1
    with pytest.raises(ManifestError):
        validate_manifest(payload)


def test_missing_manifest_file_rejected(tmp_path) -> None:
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "nope.json")
