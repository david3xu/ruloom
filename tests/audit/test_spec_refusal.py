"""Audit regression: GenerationSpec refusals stop before any model call.

Every unsupported request -- an unknown strategy, an unsupported spec
version, rows over the declared limit, imaging requested where the
profile declares no imaging output, an unknown output modality, or a
spec naming a different profile -- is refused during spec
normalisation, before any model client is built and before any
generation runs. Each refusal leaves the same durable terminal
history a failed run would: a run directory with a hash-chained event
export and a ``run.json`` carrying status ``"refused"``.

All model contact is faked with a counting factory; no test here
touches the network. The source file never needs to exist: the
refusal precedes the first source read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from engine.audit.runlog import RunLog
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_FAILED, execute
from profiles.tabular import capabilities as tab_capabilities

REPO_ROOT = Path(__file__).resolve().parents[2]
TABULAR_MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")
TABULAR_STRATEGY = TABULAR_MANIFEST.generation["default_strategy"]


class CountingFactory:
    """A model factory double recording every client it builds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def for_role(self, role: str, config: Mapping[str, Any]) -> Any:
        self.calls.append((role, dict(config)))
        raise AssertionError(f"no model client should be built for {role!r}")


def _refused_run_dir(out: Path) -> Path:
    run_dirs = [
        child for child in (out / "runs").iterdir() if (child / "run.json").is_file()
    ]
    assert len(run_dirs) == 1
    return run_dirs[0]


def _run_with_spec(
    tmp_path: Path, overlay: Mapping[str, Any]
) -> tuple[int, dict[str, Any], CountingFactory]:
    factory = CountingFactory()
    out = tmp_path / "out"
    code, outcome = execute(
        TABULAR_MANIFEST,
        tab_capabilities.CAPABILITIES,
        source=tmp_path / "missing-source.csv",
        max_attempts=2,
        model_factory=factory.for_role,
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
        spec=dict(overlay),
    )
    return code, outcome, factory


def _assert_durable_refusal(
    out: Path, outcome: dict[str, Any], factory: CountingFactory, fragment: str
) -> Path:
    assert outcome["status"] == "refused"
    assert outcome["run_id"] is not None
    assert factory.calls == []
    run_dir = _refused_run_dir(out)
    assert run_dir.name == outcome["run_id"]
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert fragment in (record["error"] or "")
    log = RunLog.read_jsonl(run_dir / "run.jsonl")
    assert log.verify()
    stages = [(event["stage"], event["status"]) for event in log.events]
    assert ("spec", "refused") in stages
    return run_dir


def test_unknown_strategy_refused_before_model(tmp_path: Path) -> None:
    code, outcome, factory = _run_with_spec(tmp_path, {"strategy": "no-such-strategy"})
    assert code == EXIT_FAILED
    _assert_durable_refusal(tmp_path / "out", outcome, factory, "unknown strategy")


def test_unsupported_spec_version_refused_before_model(tmp_path: Path) -> None:
    code, outcome, factory = _run_with_spec(tmp_path, {"spec_version": 999})
    assert code == EXIT_FAILED
    _assert_durable_refusal(
        tmp_path / "out", outcome, factory, "unsupported spec version"
    )


def test_rows_over_limit_refused_before_model(tmp_path: Path) -> None:
    manifest_max = TABULAR_MANIFEST.limits["max_rows"]
    code, outcome, factory = _run_with_spec(
        tmp_path, {"limits": {"max_rows": manifest_max + 1, "max_attempts": 2}}
    )
    assert code == EXIT_FAILED
    _assert_durable_refusal(tmp_path / "out", outcome, factory, "exceed manifest limit")


def test_imaging_where_unsupported_refused_before_model(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    factory = CountingFactory()
    out = tmp_path / "out"
    code, outcome = execute(
        TABULAR_MANIFEST,
        tab_capabilities.CAPABILITIES,
        source=tmp_path / "missing-source.csv",
        max_attempts=2,
        model_factory=factory.for_role,
        out=out,
        images_dir=images,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    _assert_durable_refusal(tmp_path / "out", outcome, factory, "declares ['scalar']")


def test_unknown_output_modality_refused_before_model(tmp_path: Path) -> None:
    code, outcome, factory = _run_with_spec(tmp_path, {"outputs": ["scalar", "video"]})
    assert code == EXIT_FAILED
    _assert_durable_refusal(
        tmp_path / "out", outcome, factory, "unknown output modality"
    )


def test_spec_profile_mismatch_refused_before_model(tmp_path: Path) -> None:
    code, outcome, factory = _run_with_spec(tmp_path, {"profile": "no-such-profile"})
    assert code == EXIT_FAILED
    _assert_durable_refusal(
        tmp_path / "out", outcome, factory, "does not match run profile"
    )


def test_spec_attempt_bound_mismatch_refused_before_model(tmp_path: Path) -> None:
    code, outcome, factory = _run_with_spec(tmp_path, {"limits": {"max_attempts": 5}})
    assert code == EXIT_FAILED
    _assert_durable_refusal(
        tmp_path / "out", outcome, factory, "contradicts the caller bound"
    )


def test_matching_spec_passes_normalisation(tmp_path: Path) -> None:
    """A spec restating the defaults normalises to the same digest."""
    from engine.core.spec import normalize_spec

    source = tmp_path / "missing-source.csv"
    first, first_digest = normalize_spec(
        None, TABULAR_MANIFEST, source=source, max_attempts=2
    )
    overlay = {
        "profile": "tabular",
        "outputs": ["scalar"],
        "strategy": TABULAR_STRATEGY,
        "limits": {"max_rows": 200000, "max_attempts": 2},
    }
    second, second_digest = normalize_spec(
        overlay, TABULAR_MANIFEST, source=source, max_attempts=2
    )
    assert first["profile"] == second["profile"] == "tabular"
    assert first_digest == second_digest


def test_source_pin_mismatch_refused_at_intake(tmp_path: Path) -> None:
    """A spec pin naming other bytes fails intake before any model call."""
    csv_path = tmp_path / "orders-tabular.csv"
    csv_path.write_text("id,value\n1,2\n", encoding="utf-8")
    factory = CountingFactory()
    out = tmp_path / "out"
    code, outcome = execute(
        TABULAR_MANIFEST,
        tab_capabilities.CAPABILITIES,
        source=csv_path,
        max_attempts=2,
        model_factory=factory.for_role,
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
        spec={"source": {"sha256": "0" * 64}},
    )
    assert code == EXIT_FAILED
    assert outcome["status"] == "error"
    assert factory.calls == []
    run_dirs = [child for child in out.rglob("run.json") if child.parent.name != "out"]
    assert len(run_dirs) == 1
    record = json.loads(run_dirs[0].read_text(encoding="utf-8"))
    assert record["status"] == "error"
    assert "spec pins source sha256" in (record["error"] or "")
