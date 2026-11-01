"""Audit counterexample fixture: R06 released bytes differ from validated bytes.

Release packaging reads the mutable artifact directory after the human
prompt, so replacing ``data.csv`` while the review waits ships bytes
the validation report never attested. The test asserts the corrected
behaviour (zipped payload must hash to the report's artifact hash); it
currently fails because the ZIP carries the replacement, recorded as
strict xfail.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_OK, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

needs_fix = pytest.mark.xfail(
    strict=True, reason="audit R06 open: released bytes differ from validated bytes"
)

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


@needs_fix
def test_mutation_during_review_must_not_release(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    out = tmp_path / "out"

    def ask(prompt: str) -> str:
        if prompt.startswith("Decision"):
            target = next((out / "runs").glob("*/data.csv"))
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
