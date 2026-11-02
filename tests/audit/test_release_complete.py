"""Q-RELEASE-COMPLETE: the release packages every declared artifact.

For each supported profile -- tabular (scalar) and hylogger (image) --
an accepted fake-model run must ship every member its release block
declares, with the extracted bytes rechecked against the frozen
manifest and the consumer verification passing. Conditional members
ship exactly when their branch produced them. Tampered or incomplete
bundles are refused against the frozen manifest.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from engine.artifacts.consumer import (
    ReleaseVerificationError,
    verify_release,
)
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_OK, execute
from models.base import Completion, Usage
from profiles.hylogger import capabilities as hy_capabilities
from profiles.hylogger.generation.plan import provider_schema
from profiles.hylogger.imaging.critic import CRITIC_SCHEMA
from profiles.hylogger.imaging.generate import make_fixture_png
from profiles.tabular.capabilities import CAPABILITIES as TAB_CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
TAB_MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")
HY_MANIFEST = load_manifest(REPO_ROOT / "profiles" / "hylogger" / "profile.json")

BASE_DEPTH = 123.456789
STEP = 0.0078
COVERAGE: list[tuple[str, str, str]] = [
    ("1", "A", "X"),
    ("1", "B", ""),
    ("1", "", "Y"),
    ("1", "A", "Y"),
    ("0", "", ""),
    ("1", "B", "X"),
    ("0", "", ""),
    ("1", "A", "X"),
]
PALETTE = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]


class TabularModel:
    """A planner double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(plan) for plan in plans]

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=1, output_tokens=1, calls=1),
            meta={"model": "fake"},
        )


class FakePlanner:
    """A hylogger planner double replaying one queued plan."""

    def __init__(self, plan: Mapping[str, Any]) -> None:
        self._plan = dict(plan)

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        assert dict(schema) == provider_schema(["fitted_markov_joint"])
        return Completion(
            data=dict(self._plan),
            usage=Usage(model="fake", input_tokens=10, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


class FakeVisionClient:
    """A vision-role double returning one queued advisory note."""

    def __init__(self, note: str = "looks plausibly banded") -> None:
        self.note = note

    def complete_with_image(
        self, prompt: str, schema: Mapping[str, Any], image_path: Path
    ) -> Completion:
        assert dict(schema) == CRITIC_SCHEMA
        return Completion(
            data={"note": self.note},
            usage=Usage(model="fake-vision", input_tokens=7, output_tokens=3, calls=1),
            meta={"model": "fake-vision"},
        )


class BothModels:
    """One client per role: planner answers plans, critic answers notes."""

    def __init__(self, plan: Mapping[str, Any]) -> None:
        self.planner = FakePlanner(plan)
        self.critic = FakeVisionClient()

    def for_role(self, role: str, config: Mapping[str, Any]) -> Any:
        if role == "critic":
            return self.critic
        return self.planner


def _write_tabular_source(path: Path) -> Path:
    csv_path = path / "sample-complete-tabular.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (path / "sample-complete-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "complete-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Release fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _write_hy_source(path: Path) -> Path:
    csv_path = path / "TOY-complete-scalars.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i, (mask, swir, tir) in enumerate(COVERAGE):
        lines.append(f"{BASE_DEPTH + i * STEP},{mask},{swir},{tir}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "TOY-complete-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "TOY-complete",
                "licence": "CC-BY-4.0",
                "attribution": "Toy release fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _write_thresholds(path: Path) -> Path:
    from profiles.hylogger.analysis.calibration import Band, Thresholds

    thresholds = Thresholds(
        bands={
            "spacing_median_m": Band("spacing_median_m", 0.005, 0.01),
            "repeated_depth_fraction": Band("repeated_depth_fraction", 0.0, 0.2),
            "valid_fraction": Band("valid_fraction", 0.5, 1.0),
            "swir_answer_rate": Band("swir_answer_rate", 0.0, 1.0, vacuous=True),
            "tir_answer_rate": Band("tir_answer_rate", 0.9, 1.0),
            "repeated_valid_fraction": Band("repeated_valid_fraction", 0.0, 0.05),
            "backward_steps": Band("backward_steps", 0, 0),
        },
        swir_vocabulary=frozenset({"A", "B"}),
        tir_vocabulary=frozenset({"X", "Y"}),
    )
    thresholds_path = path / "thresholds.json"
    thresholds_path.write_text(
        json.dumps(thresholds.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return thresholds_path


def _write_images(path: Path) -> Path:
    images = path / "images"
    images.mkdir()
    (images / "core-a.png").write_bytes(make_fixture_png(16, 8, PALETTE))
    (images / "core-a-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "core-a",
                "licence": "CC-BY-4.0",
                "attribution": "Toy image attribution.",
            }
        ),
        encoding="utf-8",
    )
    return images


def _declared_names(manifest: Any, *, images: bool) -> list[str]:
    """Release-declared member names applicable to this run."""
    names: list[str] = []
    for member in manifest.release.get("members", []):
        if member.get("when") == "images_present" and not images:
            continue
        names.append(member["name"])
    return names


def _check_complete_bundle(
    artifact: Path, manifest_doc: dict[str, Any], expected: list[str]
) -> None:
    """Every declared member ships with frozen bytes; the bundle verifies."""
    release_zip = artifact / "release.zip"
    with zipfile.ZipFile(release_zip) as archive:
        assert sorted(archive.namelist()) == sorted(expected + ["checksums.sha256"])
        by_path = {entry["path"]: entry for entry in manifest_doc["members"]}
        assert set(by_path) == set(expected)
        for name in expected:
            shipped = archive.read(name)
            assert hashlib.sha256(shipped).hexdigest() == by_path[name]["sha256"], name
    outcome = verify_release(release_zip, manifest=manifest_doc)
    assert outcome["artifact_sha256"] == manifest_doc["artifact_sha256"]
    decision = json.loads((artifact / "decision.json").read_text(encoding="utf-8"))
    assert decision["frozen_manifest_sha256"] == manifest_doc["evidence_digest"]


def test_tabular_release_is_complete(tmp_path: Path) -> None:
    source = _write_tabular_source(tmp_path)
    out = tmp_path / "out"
    code, result = execute(
        TAB_MANIFEST,
        TAB_CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: TabularModel([{"seed": 1, "n_rows": 100}]),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    manifest_doc = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_doc["artifact_sha256"] == result["artifact_sha256"]
    _check_complete_bundle(
        artifact, manifest_doc, _declared_names(TAB_MANIFEST, images=False)
    )


def _run_hy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    images: bool,
    answers: Sequence[str] = ("accept", "ok"),
) -> tuple[int, dict[str, Any]]:
    source = _write_hy_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out = tmp_path / "out"
    models = BothModels({"seed": 8, "n_rows": 8})
    queue = list(answers)
    return execute(
        HY_MANIFEST,
        hy_capabilities.CAPABILITIES,
        source=source,
        max_attempts=2,
        model_factory=models.for_role,
        out=out,
        images_dir=_write_images(tmp_path) if images else None,
        say=lambda message: None,
        ask=lambda prompt: queue.pop(0),
    )


def test_hylogger_image_release_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, result = _run_hy(tmp_path, monkeypatch, images=True)
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    manifest_doc = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_doc["artifact_sha256"] == result["artifact_sha256"]
    expected = _declared_names(HY_MANIFEST, images=True)
    assert "synthetic.png" in expected
    _check_complete_bundle(artifact, manifest_doc, expected)


def test_hylogger_scalar_run_omits_conditional_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, result = _run_hy(tmp_path, monkeypatch, images=False)
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    manifest_doc = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    expected = _declared_names(HY_MANIFEST, images=False)
    assert "synthetic.png" not in expected
    _check_complete_bundle(artifact, manifest_doc, expected)


def test_mutation_during_review_still_releases_frozen_complete_bundle(
    tmp_path: Path,
) -> None:
    """Bytes swapped while the review waits never reach the release."""
    source = _write_tabular_source(tmp_path)
    out = tmp_path / "out"

    def ask(prompt: str) -> str:
        if prompt.startswith("Decision"):
            run_dir = next((out / "runs").glob("*/run.json")).parent
            (run_dir / "data.csv").write_text(
                "value,label\n999,UNVALIDATED\n", encoding="utf-8"
            )
            return "accept"
        return "accepted after mutation"

    code, result = execute(
        TAB_MANIFEST,
        TAB_CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: TabularModel([{"seed": 1, "n_rows": 100}]),
        out=out,
        say=lambda message: None,
        ask=ask,
    )
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    manifest_doc = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        assert b"UNVALIDATED" not in archive.read("data.csv")
    _check_complete_bundle(
        artifact, manifest_doc, _declared_names(TAB_MANIFEST, images=False)
    )


def _copy_bundle_without_member(
    release_zip: Path, tmp_path: Path, *, drop: str
) -> Path:
    slim = tmp_path / "slim.zip"
    with (
        zipfile.ZipFile(release_zip) as src,
        zipfile.ZipFile(slim, "w", compression=zipfile.ZIP_DEFLATED) as dst,
    ):
        for name in src.namelist():
            if name != drop:
                dst.writestr(name, src.read(name))
    return slim


def _copy_bundle_with_tampered_member(
    release_zip: Path, tmp_path: Path, *, member: str
) -> Path:
    tampered = tmp_path / "tampered.zip"
    with (
        zipfile.ZipFile(release_zip) as src,
        zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as dst,
    ):
        for name in src.namelist():
            payload = src.read(name)
            if name == member:
                payload = payload + b"\n# tampered\n"
            dst.writestr(name, payload)
    return tampered


def test_bundle_missing_a_declared_member_is_refused(
    tmp_path: Path,
) -> None:
    source = _write_tabular_source(tmp_path)
    out = tmp_path / "out"
    code, result = execute(
        TAB_MANIFEST,
        TAB_CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: TabularModel([{"seed": 1, "n_rows": 100}]),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    manifest_doc = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    slim = _copy_bundle_without_member(
        artifact / "release.zip", tmp_path, drop="validation-report.json"
    )
    with pytest.raises(ReleaseVerificationError, match="missing"):
        verify_release(slim, manifest=manifest_doc)


def test_bundle_with_substituted_bytes_is_refused(tmp_path: Path) -> None:
    source = _write_tabular_source(tmp_path)
    out = tmp_path / "out"
    code, result = execute(
        TAB_MANIFEST,
        TAB_CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: TabularModel([{"seed": 1, "n_rows": 100}]),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    manifest_doc = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    tampered = _copy_bundle_with_tampered_member(
        artifact / "release.zip", tmp_path, member="data.csv"
    )
    with pytest.raises(ReleaseVerificationError):
        verify_release(tampered, manifest=manifest_doc)
