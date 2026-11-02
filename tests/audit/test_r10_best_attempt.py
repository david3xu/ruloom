"""Audit counterexample fixture: R10 best-attempt evidence names another plan.

Restoring the best attempt used to restore its artifact and verdict
but not its plan, so the packager wrote the last attempted plan.
Every attempt is now an immutable record binding the manifest digest,
the source hash, the complete resolved plan, the seed, the artifact
manifest, the check results, and the resource use; selecting a best
attempt selects the whole record, and packaging writes the selected
attempt's own plan, seed, and artifact together.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")


class FakeModel:
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
    (path / "sample-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "r10-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _write_thresholds(path: Path, tolerance: float) -> Path:
    thresholds = path / "strict.json"
    thresholds.write_text(
        json.dumps({"mean_relative_tolerance": tolerance}), encoding="utf-8"
    )
    return thresholds


def _run(
    tmp_path: Path,
    plans: Sequence[Mapping[str, Any]],
    answers: Sequence[str],
    tolerance: float,
    monkeypatch: Any,
) -> tuple[int, dict[str, Any]]:
    source = _write_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path, tolerance)))
    queue = list(answers)
    client = FakeModel(plans)
    return execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=len(plans),
        model_factory=lambda role, config: client,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: queue.pop(0),
    )


def _packaged_triple(artifact: Path) -> tuple[dict[str, Any], int, int]:
    """The packaged (plan, seed, data-row) triple of a run directory."""
    packaged = json.loads((artifact / "plan.json").read_text(encoding="utf-8"))
    seed = int((artifact / "seed.txt").read_text(encoding="utf-8").strip())
    data_rows = (
        len((artifact / "data.csv").read_text(encoding="utf-8").splitlines()) - 1
    )
    return packaged, seed, data_rows


def test_best_attempt_packages_its_own_plan(tmp_path: Path, monkeypatch: Any) -> None:
    """Tied failing attempts package the first (best) attempt's triple."""
    code, result = _run(
        tmp_path,
        [{"seed": 11, "n_rows": 4}, {"seed": 22, "n_rows": 8}],
        ["accept", "note"],
        tolerance=0.0,
        monkeypatch=monkeypatch,
    )
    assert code == EXIT_FAILED
    assert result["attempts"] == 2
    artifact = Path(str(result["artifact_dir"]))
    packaged, seed, data_rows = _packaged_triple(artifact)
    assert packaged["n_rows"] == data_rows == 4
    assert packaged["seed"] == seed == 11


def test_first_attempt_success_packages_its_own_plan(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A passing first attempt is the best attempt: package its triple."""
    code, result = _run(
        tmp_path,
        [{"seed": 2, "n_rows": 4}],
        ["accept", "first accepted"],
        tolerance=0.05,
        monkeypatch=monkeypatch,
    )
    assert code == EXIT_OK
    assert result["status"] == "released"
    assert result["attempts"] == 1
    artifact = Path(str(result["artifact_dir"]))
    packaged, seed, data_rows = _packaged_triple(artifact)
    assert packaged["n_rows"] == data_rows == 4
    assert packaged["seed"] == seed == 2
    assert (artifact / "release.zip").is_file()


def test_final_success_packages_the_last_plan(tmp_path: Path, monkeypatch: Any) -> None:
    """Fail-then-pass packages the final (best) attempt's triple."""
    code, result = _run(
        tmp_path,
        [{"seed": 11, "n_rows": 4}, {"seed": 7, "n_rows": 6}],
        ["accept", "repaired run accepted"],
        tolerance=0.05,
        monkeypatch=monkeypatch,
    )
    assert code == EXIT_OK
    assert result["status"] == "released"
    assert result["attempts"] == 2
    artifact = Path(str(result["artifact_dir"]))
    packaged, seed, data_rows = _packaged_triple(artifact)
    assert packaged["n_rows"] == data_rows == 6
    assert packaged["seed"] == seed == 7
    assert (artifact / "release.zip").is_file()
