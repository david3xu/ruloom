"""Audit regression: R06 released bytes must equal validated bytes.

Release packaging used to read the mutable artifact directory after the
human prompt, so replacing ``data.csv`` while the review waited shipped
bytes the validation report never attested. The engine now snapshots
every release candidate before the prompt (the frozen manifest binds
path, size, sha256, and producing attempt), the human decision binds
the manifest digest, and packaging reads only the frozen snapshot --
never mutable working files.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.artifacts.consumer import verify_release
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_OK, execute
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
                "dataset_name": "r06-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def test_mutation_during_review_must_not_release(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    out = tmp_path / "out"

    def ask(prompt: str) -> str:
        if prompt.startswith("Decision"):
            # Mutate the run's own packaged copy: exactly one run.json
            # exists at gate time, so this targets the directory the
            # old packager read (the payload cache has no run.json).
            run_dir = next((out / "runs").glob("*/run.json")).parent
            target = run_dir / "data.csv"
            target.write_text("value,label\n999,UNVALIDATED\n", encoding="utf-8")
            return "accept"
        return "accepted after mutation"

    code, result = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: FakeModel([{"seed": 1, "n_rows": 100}]),
        out=out,
        say=lambda message: None,
        ask=ask,
    )
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        shipped = hashlib.sha256(archive.read("data.csv")).hexdigest()
        report = json.loads(archive.read("validation-report.json"))
    assert shipped == report["artifact_sha256"]


def test_deleted_working_file_after_freeze_still_releases(tmp_path: Path) -> None:
    """Deleting a working file during review cannot break the release.

    The snapshot is taken before the prompt; packaging reads only the
    snapshot, so removing (or replacing) the working copy leaves the
    released bytes identical to the validated ones.
    """
    source = _write_source(tmp_path)
    out = tmp_path / "out"

    def ask(prompt: str) -> str:
        if prompt.startswith("Decision"):
            run_dir = next((out / "runs").glob("*/run.json")).parent
            (run_dir / "data.csv").unlink()
            (run_dir / "plan.json").write_text(
                '{"seed": 999, "n_rows": 999}\n', encoding="utf-8"
            )
            return "accept"
        return "accepted after deletion"

    code, result = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: FakeModel([{"seed": 1, "n_rows": 100}]),
        out=out,
        say=lambda message: None,
        ask=ask,
    )
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    # The working copies are gone or replaced, yet the release survives.
    assert not (artifact / "data.csv").exists()
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    decision = json.loads((artifact / "decision.json").read_text(encoding="utf-8"))
    assert decision["frozen_manifest_sha256"] == manifest["evidence_digest"]
    assert manifest["artifact_sha256"] == result["artifact_sha256"]
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        shipped = hashlib.sha256(archive.read("data.csv")).hexdigest()
        report = json.loads(archive.read("validation-report.json"))
    assert shipped == report["artifact_sha256"]
    member = next(entry for entry in manifest["members"] if entry["path"] == "data.csv")
    assert member["sha256"] == shipped
    assert member["size"] == len((artifact / "frozen" / "data.csv").read_bytes())
    outcome = verify_release(artifact / "release.zip", manifest=manifest)
    assert outcome["artifact_sha256"] == shipped
