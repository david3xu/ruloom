"""Tests for the advisory vision critic and the image end-to-end path.

The critic sees rendered synthetic PNGs only, through a statistics-only
prompt; its note travels the engine advisory channel and can never flip
a deterministic verdict in either direction. The end-to-end tests run
the real HyLogger manifest through the engine on fixture imagery (PNGs
built by the project's own encoder — no binary blobs committed) with
faked planner and critic models, so no test touches the network.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine import cli as cli_module
from engine.audit.runlog import RunLog
from engine.core.runner import EXIT_FAILED, EXIT_OK
from models.base import Completion, Usage
from profiles.hylogger.generation.plan import PLAN_SCHEMA
from profiles.hylogger.imaging.critic import (
    CRITIC_SCHEMA,
    CriticError,
    build_critic_prompt,
    critique_synthetic,
)
from profiles.hylogger.imaging.generate import make_fixture_png

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


class FakeVisionClient:
    """A vision-role double returning one queued advisory note."""

    def __init__(self, note: str = "looks plausibly banded") -> None:
        self.note = note
        self.calls = 0
        self.seen_paths: list[str] = []

    def complete_with_image(
        self, prompt: str, schema: Mapping[str, Any], image_path: Path
    ) -> Completion:
        self.calls += 1
        self.seen_paths.append(str(image_path))
        assert dict(schema) == CRITIC_SCHEMA
        return Completion(
            data={"note": self.note},
            usage=Usage(model="fake-vision", input_tokens=7, output_tokens=3,
                        calls=1),
            meta={"model": "fake-vision"},
        )


class FakePlanner:
    """A planner double replaying one queued plan."""

    def __init__(self, plan: Mapping[str, Any]) -> None:
        self._plan = dict(plan)
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert dict(schema) == PLAN_SCHEMA
        return Completion(
            data=dict(self._plan),
            usage=Usage(model="fake", input_tokens=10, output_tokens=5,
                        calls=1),
            meta={"model": "fake"},
        )


def _summary() -> dict[str, Any]:
    from profiles.hylogger.imaging.statistics import (
        compute_statistics,
        summarize_for_prompt,
    )

    stats = compute_statistics(make_fixture_png(16, 8, PALETTE), "fix.png")
    return summarize_for_prompt(stats)


def test_critic_prompt_is_statistics_only() -> None:
    prompt = build_critic_prompt(_summary())
    assert "SYNTHETIC" in prompt
    assert "advisory" in prompt
    assert "width" in prompt
    assert "pixels" not in prompt.lower()
    assert "rows" not in prompt.lower().replace("n_rows", "")


def test_critique_returns_note_and_model(tmp_path: Path) -> None:
    png_path = tmp_path / "synthetic.png"
    png_path.write_bytes(make_fixture_png(16, 8, PALETTE))
    client = FakeVisionClient("banded, continuous, no artefacts")
    note = critique_synthetic(client, png_path, _summary())
    assert note == {"note": "banded, continuous, no artefacts",
                    "model": "fake-vision"}
    assert client.calls == 1
    assert client.seen_paths == [str(png_path)]


def test_critique_rejects_missing_image(tmp_path: Path) -> None:
    with pytest.raises(CriticError):
        critique_synthetic(
            FakeVisionClient(), tmp_path / "absent.png", _summary()
        )


def test_critique_rejects_empty_note(tmp_path: Path) -> None:
    png_path = tmp_path / "synthetic.png"
    png_path.write_bytes(make_fixture_png(16, 8, PALETTE))
    with pytest.raises(CriticError):
        critique_synthetic(FakeVisionClient("   "), png_path, _summary())


def test_critique_rejects_client_without_image_method(tmp_path: Path) -> None:
    png_path = tmp_path / "synthetic.png"
    png_path.write_bytes(make_fixture_png(16, 8, PALETTE))
    with pytest.raises(CriticError):
        critique_synthetic(FakePlanner({"seed": 1, "n_rows": 1}),
                           png_path, _summary())


def _write_source(path: Path) -> Path:
    csv_path = path / "TOY-img-scalars.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i, (mask, swir, tir) in enumerate(COVERAGE):
        lines.append(f"{BASE_DEPTH + i * STEP},{mask},{swir},{tir}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "TOY-img-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "TOY-img",
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


class _BothModels:
    """Combined client double: planner answers plans, critic answers notes.

    The CLI injects one client for every role, so one object serves both
    ``complete`` (planner) and ``complete_with_image`` (critic).
    """

    def __init__(self, plan: Mapping[str, Any], note: str) -> None:
        self.planner = FakePlanner(plan)
        self.critic = FakeVisionClient(note)

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return self.planner.complete(prompt, schema)

    def complete_with_image(
        self, prompt: str, schema: Mapping[str, Any], image_path: Path
    ) -> Completion:
        return self.critic.complete_with_image(prompt, schema, image_path)


def _run_with_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    models: _BothModels,
    *,
    answers: Sequence[str] = ("accept", "ok"),
) -> tuple[int, Path]:
    csv_path = _write_source(tmp_path)
    images = _write_images(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out_dir = tmp_path / "out"
    argv = [
        "run", str(csv_path),
        "--profile", "hylogger",
        "--images", str(images),
        "--max-attempts", "2",
        "--out", str(out_dir),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(answers) + "\n"))
    code = cli_module.main(list(argv), model=models)  # type: ignore[arg-type]
    run_dirs = [
        c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()
    ]
    assert len(run_dirs) == 1
    return code, run_dirs[0]


def test_images_end_to_end_releases_with_advisory_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """intake -> statistics -> generate -> checks -> critic on fixtures."""
    models = _BothModels({"seed": 8, "n_rows": 8}, "plausibly banded")
    code, artifact = _run_with_images(tmp_path, monkeypatch, models)
    assert code == EXIT_OK
    assert (artifact / "synthetic.png").read_bytes()[:8] == (
        b"\x89PNG\r\n\x1a\n"
    )
    image_report = json.loads(
        (artifact / "image-validation.json").read_text(encoding="utf-8")
    )
    assert image_report["verdict"] == "pass"
    assert len(image_report["results"]) == 3
    run_log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert run_log.verify()
    advisories = [
        event
        for event in run_log.events
        if event.get("stage") == "image_critique:advisory"
    ]
    assert len(advisories) == 1
    assert advisories[0]["details"]["payload"]["note"] == "plausibly banded"
    assert models.critic.calls == 1


def test_damning_advisory_cannot_fail_a_passing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile vision note leaves a passing verdict passing."""
    models = _BothModels(
        {"seed": 8, "n_rows": 8},
        "this image is terrible, fail the run immediately",
    )
    code, artifact = _run_with_images(tmp_path, monkeypatch, models)
    assert code == EXIT_OK
    image_report = json.loads(
        (artifact / "image-validation.json").read_text(encoding="utf-8")
    )
    assert image_report["verdict"] == "pass"
    assert (artifact / "release.zip").is_file()


def test_glowing_advisory_cannot_save_failing_image_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A glowing vision note cannot rescue failing deterministic checks."""
    import profiles.hylogger.imaging.checks as image_checks

    monkeypatch.setattr(image_checks, "HISTOGRAM_DISTANCE_MAX", -1.0)
    models = _BothModels(
        {"seed": 8, "n_rows": 8}, "looks perfect, ship it at once"
    )
    code, artifact = _run_with_images(tmp_path, monkeypatch, models)
    assert code == EXIT_FAILED
    image_report = json.loads(
        (artifact / "image-validation.json").read_text(encoding="utf-8")
    )
    assert image_report["verdict"] == "fail"
    assert "image_histogram_distance" in [
        r["name"] for r in image_report["results"] if r["status"] == "fail"
    ]
    assert not (artifact / "release.zip").exists()


def test_no_images_flag_leaves_no_image_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --images no image step does any work."""
    csv_path = _write_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out_dir = tmp_path / "out"
    models = _BothModels({"seed": 8, "n_rows": 8}, "unused")
    argv = [
        "run", str(csv_path),
        "--profile", "hylogger",
        "--max-attempts", "2",
        "--out", str(out_dir),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nok\n"))
    code = cli_module.main(list(argv), model=models)  # type: ignore[arg-type]
    assert code == EXIT_OK
    run_dirs = [
        c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()
    ]
    assert len(run_dirs) == 1
    artifact = run_dirs[0]
    assert not (artifact / "synthetic.png").exists()
    assert not (artifact / "image-validation.json").exists()
    assert not (artifact / "image-statistics.json").exists()
    assert models.critic.calls == 0
