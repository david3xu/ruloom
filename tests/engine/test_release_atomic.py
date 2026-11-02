"""Atomic release: a failure mid-packaging leaves no partial release.

The ZIP is built in a temporary file and renamed into place, so an
injected packaging failure leaves no ``release.zip`` (and no detached
hash or manifest). The human decision is already recorded, the run
state stays consistent, and a rerun can complete. Identical frozen
inputs always yield identical ZIP bytes.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from engine.artifacts.package import write_release_zip
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
                "dataset_name": "atomic-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _run_dirs(out: Path) -> list[Path]:
    return sorted(c for c in (out / "runs").iterdir() if (c / "run.json").is_file())


def _run_record(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def test_packaging_failure_leaves_no_release_but_keeps_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path)
    out = tmp_path / "out"

    def boom(artifact_dir: Path, files: Mapping[str, bytes]) -> Any:
        raise RuntimeError("injected packaging failure")

    monkeypatch.setattr("engine.artifacts.package.write_release_zip", boom)
    code, result = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: FakeModel([{"seed": 1, "n_rows": 100}]),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept" if prompt.startswith("Decision") else "ok",
    )
    assert code == EXIT_FAILED
    assert result["status"] == "error"
    (run_dir,) = _run_dirs(out)
    # The human decision was recorded before packaging ran.
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    assert decision["verdict"] == "accept"
    assert decision["frozen_manifest_sha256"]
    # No partial release survived the injected failure.
    assert not (run_dir / "release.zip").exists()
    assert not (run_dir / "release.sha256").exists()
    assert not (run_dir / "manifest.json").exists()
    assert list(run_dir.glob("release.zip.tmp.*")) == []
    record = _run_record(run_dir)
    assert record["status"] == "error"
    assert record["decision"]["verdict"] == "accept"


def test_rerun_after_failure_can_complete(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    out = tmp_path / "out"
    factory = lambda role, config: FakeModel([{"seed": 1, "n_rows": 100}])  # noqa: E731
    answers = lambda prompt: (  # noqa: E731
        "accept" if prompt.startswith("Decision") else "ok"
    )
    code, _ = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=factory,
        out=out,
        say=lambda message: None,
        ask=answers,
    )
    assert code == EXIT_OK
    (first,) = _run_dirs(out)
    assert (first / "release.zip").is_file()
    code, _ = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=factory,
        out=out,
        say=lambda message: None,
        ask=answers,
    )
    assert code == EXIT_OK
    both = _run_dirs(out)
    assert len(both) == 2
    second = next(run for run in both if run != first)
    assert (second / "release.zip").is_file()


def _bundle() -> dict[str, bytes]:
    payload = b"value,label\n1,A\n2,B\n"
    return {
        "data.csv": payload,
        "plan.json": json.dumps({"seed": 1, "n_rows": 2}).encode() + b"\n",
        "validation-report.json": json.dumps(
            {
                "artifact_name": "atomic:attempt-1",
                "artifact_sha256": hashlib.sha256(payload).hexdigest(),
                "verdict": "pass",
                "results": [],
            }
        ).encode()
        + b"\n",
        "seed.txt": b"1\n",
        "provenance.md": b"# Provenance\n",
        "run.jsonl": b"{}\n",
    }


def test_release_bytes_deterministic_for_frozen_inputs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    zip_a, sha_a = write_release_zip(first, _bundle())
    zip_b, sha_b = write_release_zip(second, _bundle())
    assert sha_a == sha_b
    assert zip_a.read_bytes() == zip_b.read_bytes()
    with zipfile.ZipFile(zip_a) as archive:
        assert sorted(archive.namelist()) == sorted(
            list(_bundle()) + ["checksums.sha256"]
        )
