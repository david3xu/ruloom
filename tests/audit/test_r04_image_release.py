"""Audit regression: R04 membership side -- the release carries the
evidence the run actually produced.

A passing image-enabled run must ship the synthetic image plus its
registration and check evidence (``synthetic.png``,
``image-statistics.json``, ``image-validation.json``) with hashes
matching the shipped bytes; a scalar run must omit them. A failing
report -- scalar or image -- still blocks acceptance, exactly as
before. The rights/claims side of R04 stays open for RP-06; nothing
here asserts any licence or CC0 wording.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine.artifacts.consumer import verify_release
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute
from models.base import Completion, Usage
from profiles.hylogger import capabilities as hy_capabilities
from profiles.hylogger.generation.plan import provider_schema
from profiles.hylogger.imaging.critic import CRITIC_SCHEMA
from profiles.hylogger.imaging.generate import make_fixture_png

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "hylogger" / "profile.json")

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

SCALAR_MEMBERS = [
    "data.csv",
    "plan.json",
    "validation-report.json",
    "seed.txt",
    "provenance.md",
    "run.jsonl",
    "evidence-report.md",
]
IMAGE_MEMBERS = [
    "synthetic.png",
    "image-statistics.json",
    "image-validation.json",
]


class FakePlanner:
    """A planner double replaying one queued plan."""

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
            usage=Usage(
                model="fake-vision", input_tokens=7, output_tokens=3, calls=1
            ),
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


def _write_source(path: Path) -> Path:
    csv_path = path / "TOY-imgmem-scalars.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i, (mask, swir, tir) in enumerate(COVERAGE):
        lines.append(f"{BASE_DEPTH + i * STEP},{mask},{swir},{tir}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "TOY-imgmem-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "TOY-imgmem",
                "licence": "CC-BY-4.0",
                "attribution": "Toy attribution for tests.",
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
            "swir_answer_rate": Band(
                "swir_answer_rate", 0.0, 1.0, vacuous=True
            ),
            "tir_answer_rate": Band("tir_answer_rate", 0.9, 1.0),
            "repeated_valid_fraction": Band(
                "repeated_valid_fraction", 0.0, 0.05
            ),
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


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    images: bool,
    answers: Sequence[str] = ("accept", "ok"),
) -> tuple[int, dict[str, Any]]:
    source = _write_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out = tmp_path / "out"
    models = BothModels({"seed": 8, "n_rows": 8})
    queue = list(answers)
    return execute(
        MANIFEST,
        hy_capabilities.CAPABILITIES,
        source=source,
        max_attempts=2,
        model_factory=models.for_role,
        out=out,
        images_dir=_write_images(tmp_path) if images else None,
        say=lambda message: None,
        ask=lambda prompt: queue.pop(0),
    )


def test_image_run_releases_image_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, result = _run(tmp_path, monkeypatch, images=True)
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        assert sorted(archive.namelist()) == sorted(
            SCALAR_MEMBERS + IMAGE_MEMBERS + ["checksums.sha256"]
        )
        shipped_image = archive.read("synthetic.png")
        image_report = json.loads(archive.read("image-validation.json"))
    assert shipped_image[:8] == b"\x89PNG\r\n\x1a\n"
    # The registration and check evidence bind the shipped image bytes.
    assert image_report["verdict"] == "pass"
    import hashlib as _hashlib

    assert image_report["artifact_sha256"] == _hashlib.sha256(
        shipped_image
    ).hexdigest()
    manifest = json.loads(
        (artifact / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["artifact_sha256"] == result["artifact_sha256"]
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        for entry in manifest["members"]:
            assert entry["sha256"] == hashlib.sha256(
                archive.read(entry["path"])
            ).hexdigest()
    decision = json.loads(
        (artifact / "decision.json").read_text(encoding="utf-8")
    )
    assert decision["frozen_manifest_sha256"] == manifest["evidence_digest"]
    outcome = verify_release(artifact / "release.zip", manifest=manifest)
    assert outcome["artifact_sha256"] == result["artifact_sha256"]


def test_scalar_run_omits_image_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, result = _run(tmp_path, monkeypatch, images=False)
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        assert sorted(archive.namelist()) == sorted(
            SCALAR_MEMBERS + ["checksums.sha256"]
        )
    manifest = json.loads(
        (artifact / "manifest.json").read_text(encoding="utf-8")
    )
    assert all(
        entry["path"] not in IMAGE_MEMBERS for entry in manifest["members"]
    )
    assert not (artifact / "synthetic.png").exists()
    outcome = verify_release(artifact / "release.zip", manifest=manifest)
    assert outcome["artifact_sha256"] == result["artifact_sha256"]


def test_failing_image_checks_block_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import profiles.hylogger.imaging.checks as image_checks

    monkeypatch.setattr(image_checks, "HISTOGRAM_DISTANCE_MAX", -1.0)
    code, result = _run(tmp_path, monkeypatch, images=True)
    assert code == EXIT_FAILED
    assert result["status"] == "failed"
    run_dirs = [
        c
        for c in (tmp_path / "out" / "runs").iterdir()
        if (c / "run.json").is_file()
    ]
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "release.zip").exists()
