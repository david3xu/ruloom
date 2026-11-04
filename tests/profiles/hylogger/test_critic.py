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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from engine import cli as cli_module
from engine.audit.runlog import RunLog
from engine.core.runner import EXIT_FAILED, EXIT_OK
from models.base import Completion, ModelCapabilities, Usage
from profiles.hylogger.generation.plan import provider_schema
from profiles.hylogger.imaging.critic import (
    CRITIC_SCHEMA,
    ECHO_BEGIN,
    ECHO_END,
    OBSERVATION_KEYS,
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


FACTS = [{"name": "image_dimensions", "status": "pass", "detail": "16x8 fixture"}]


def vision_answer(prompt: str, note: str) -> dict[str, Any]:
    """Controlled response echoes the exact facts supplied by the real workflow."""
    echo = json.loads(
        prompt.split(ECHO_BEGIN + "\n", 1)[1].split("\n" + ECHO_END, 1)[0]
    )
    return {
        **echo,
        "note": note,
        **{key: "None visible in this injected fixture." for key in OBSERVATION_KEYS},
        "cited_check_ids": [],
    }


class FakeVisionClient:
    """A vision-role double returning one queued advisory note."""

    capabilities = ModelCapabilities(
        input_modalities=("text", "image"),
        structured_output=True,
        image_media_types=("image/png",),
        max_image_bytes=10_000_000,
        max_image_pixels=10_000_000,
    )

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
            data=vision_answer(prompt, self.note),
            usage=Usage(model="fake-vision", input_tokens=7, output_tokens=3, calls=1),
            meta={
                "model": "fake-vision",
                "provider": "fixture",
                "adapter": "injected",
                "evidence_kind": "injected",
            },
        )


class FakePlanner:
    """A planner double replaying one queued plan."""

    def __init__(self, plan: Mapping[str, Any]) -> None:
        self._plan = dict(plan)
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert dict(schema) == provider_schema(["fitted_markov_joint"])
        return Completion(
            data=dict(self._plan),
            usage=Usage(model="fake", input_tokens=10, output_tokens=5, calls=1),
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
    prompt = build_critic_prompt(_summary(), "0" * 64, FACTS)
    assert "SYNTHETIC" in prompt
    assert "advisory" in prompt
    assert "width" in prompt
    assert "pixels" not in prompt.lower()
    assert "rows" not in prompt.lower().replace("n_rows", "")


def test_critique_returns_note_and_model(tmp_path: Path) -> None:
    png_path = tmp_path / "synthetic.png"
    png_path.write_bytes(make_fixture_png(16, 8, PALETTE))
    client = FakeVisionClient("banded, continuous, no artefacts")
    note = critique_synthetic(client, png_path, _summary(), deterministic_checks=FACTS)
    assert note["note"] == "banded, continuous, no artefacts"
    assert note["model"] == "fake-vision"
    assert note["status"] == "ok"
    assert "observations" in note
    assert "artifact_sha256" in note
    assert "receipt_sha256" in note
    assert client.calls == 1
    assert client.seen_paths == [str(png_path)]


def test_critique_rejects_missing_image(tmp_path: Path) -> None:
    with pytest.raises(CriticError):
        critique_synthetic(
            FakeVisionClient(),
            tmp_path / "absent.png",
            _summary(),
            deterministic_checks=FACTS,
        )


def test_critique_rejects_empty_note(tmp_path: Path) -> None:
    png_path = tmp_path / "synthetic.png"
    png_path.write_bytes(make_fixture_png(16, 8, PALETTE))
    with pytest.raises(CriticError):
        critique_synthetic(
            FakeVisionClient("   "), png_path, _summary(), deterministic_checks=FACTS
        )


def test_critique_rejects_client_without_image_method(tmp_path: Path) -> None:
    png_path = tmp_path / "synthetic.png"
    png_path.write_bytes(make_fixture_png(16, 8, PALETTE))
    with pytest.raises(CriticError):
        critique_synthetic(
            FakePlanner({"seed": 1, "n_rows": 1}),
            png_path,
            _summary(),
            deterministic_checks=FACTS,
        )


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


class _BothModels:
    """Combined client double: planner answers plans, critic answers notes.

    The CLI injects one client for every role, so one object serves both
    ``complete`` (planner) and ``complete_with_image`` (critic).
    """

    def __init__(self, plan: Mapping[str, Any], note: str) -> None:
        self.planner = FakePlanner(plan)
        self.critic = FakeVisionClient(note)

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.critic.capabilities

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
    extra_argv: Sequence[str] = (),
) -> tuple[int, Path]:
    csv_path = _write_source(tmp_path)
    images = _write_images(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out_dir = tmp_path / "out"
    argv = [
        "run",
        str(csv_path),
        "--profile",
        "hylogger",
        "--images",
        str(images),
        "--max-attempts",
        "2",
        "--out",
        str(out_dir),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(answers) + "\n"))
    code = cli_module.main(list(argv) + list(extra_argv), model=models)  # type: ignore[arg-type]
    run_dirs = [c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()]
    assert len(run_dirs) == 1
    return code, run_dirs[0]


def test_images_end_to_end_releases_with_advisory_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """intake -> statistics -> generate -> checks -> critic on fixtures."""
    models = _BothModels({"seed": 8, "n_rows": 8}, "plausibly banded")
    code, artifact = _run_with_images(tmp_path, monkeypatch, models)
    assert code == EXIT_OK
    assert (artifact / "synthetic.png").read_bytes()[:8] == (b"\x89PNG\r\n\x1a\n")
    image_report = json.loads(
        (artifact / "image-validation.json").read_text(encoding="utf-8")
    )
    assert image_report["verdict"] == "pass"
    assert len(image_report["results"]) == 5
    run_log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert run_log.verify()
    advisories = [
        event
        for event in run_log.events
        if event.get("stage") == "image_critique:advisory"
    ]
    assert len(advisories) == 1
    assert advisories[0]["details"]["payload"]["note"] == "plausibly banded"
    assert (artifact / "vision-advisory.json").is_file()
    advisory_payload = json.loads(
        (artifact / "vision-advisory.json").read_text(encoding="utf-8")
    )
    assert advisory_payload["status"] == "ok"
    assert advisory_payload["note"] == "plausibly banded"
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
    models = _BothModels({"seed": 8, "n_rows": 8}, "looks perfect, ship it at once")
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
        "run",
        str(csv_path),
        "--profile",
        "hylogger",
        "--max-attempts",
        "2",
        "--out",
        str(out_dir),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nok\n"))
    code = cli_module.main(list(argv), model=models)  # type: ignore[arg-type]
    assert code == EXIT_OK
    run_dirs = [c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()]
    assert len(run_dirs) == 1
    artifact = run_dirs[0]
    assert not (artifact / "synthetic.png").exists()
    assert not (artifact / "image-validation.json").exists()
    assert not (artifact / "image-statistics.json").exists()
    assert models.critic.calls == 0


@pytest.mark.parametrize(
    "fault",
    [
        "note_type",
        "category_type",
        "missing_category",
        "extra_key",
        "wrong_digest",
        "wrong_facts",
        "unknown_citation",
        "long_note",
    ],
)
def test_strict_critic_rejects_malformed_opinions(tmp_path, fault):
    import dataclasses

    class BadClient(FakeVisionClient):
        def complete_with_image(self, prompt, schema, image_path):
            completion = super().complete_with_image(prompt, schema, image_path)
            data = dict(completion.data)
            if fault == "note_type":
                data["note"] = 5
            if fault == "category_type":
                data["broken_bands"] = ["bad"]
            if fault == "missing_category":
                del data["broken_bands"]
            if fault == "extra_key":
                data["verdict"] = "pass"
            if fault == "wrong_digest":
                data["artifact_sha256"] = "0" * 64
            if fault == "wrong_facts":
                data["deterministic_checks"][0]["status"] = "fail"
            if fault == "unknown_citation":
                data["cited_check_ids"] = ["unknown_check"]
            if fault == "long_note":
                data["note"] = "x" * 2001
            return dataclasses.replace(completion, data=data)

    image = tmp_path / "generated.png"
    image.write_bytes(make_fixture_png(16, 8, PALETTE))
    with pytest.raises(CriticError):
        critique_synthetic(BadClient(), image, _summary(), deterministic_checks=FACTS)


def test_claimed_artifact_mismatch_refused_before_critic(tmp_path):
    image = tmp_path / "generated.png"
    image.write_bytes(make_fixture_png(16, 8, PALETTE))
    client = FakeVisionClient()
    with pytest.raises(CriticError, match="does not match"):
        critique_synthetic(
            client,
            image,
            _summary(),
            artifact_sha256="0" * 64,
            deterministic_checks=FACTS,
        )
    assert client.calls == 0


def test_prompt_excludes_source_identifiers_and_arbitrary_text():
    summary = _summary()
    summary.update(
        name="PRIVATE_SOURCE_MARKER",
        sha256="PRIVATE_HASH_MARKER",
        records=["PRIVATE_RECORD_MARKER"],
    )
    summary["strips"][0]["label"] = "PRIVATE_LABEL_MARKER"
    prompt = build_critic_prompt(summary, "0" * 64, FACTS)
    assert "PRIVATE_" not in prompt
    assert "histograms" in prompt


def test_optional_provider_failure_receipt_redacts_error(tmp_path, monkeypatch):
    from models.base import ModelError

    models = _BothModels({"seed": 8, "n_rows": 8}, "unused")

    def fail(*args):
        models.critic.calls += 1
        raise ModelError("PRIVATE_PROVIDER_CANARY")

    models.critic.complete_with_image = fail
    code, artifact = _run_with_images(tmp_path, monkeypatch, models)
    assert code == EXIT_OK
    receipt = json.loads((artifact / "vision-advisory.json").read_text())
    assert receipt["status"] == "advisory-failed"
    assert receipt["usage"] is None
    assert receipt["cost"]["status"] == "unavailable"
    assert models.critic.calls == 1
    assert "PRIVATE_PROVIDER_CANARY" not in (artifact / "run.jsonl").read_text()
    assert "PRIVATE_PROVIDER_CANARY" not in json.dumps(receipt)


def test_text_only_critic_records_unavailable_without_call(tmp_path, monkeypatch):
    models = _BothModels({"seed": 8, "n_rows": 8}, "unused")
    models.critic.capabilities = ModelCapabilities()
    code, artifact = _run_with_images(tmp_path, monkeypatch, models)
    assert code == EXIT_OK
    receipt = json.loads((artifact / "vision-advisory.json").read_text())
    assert receipt["status"] == "unavailable"
    assert receipt["evidence_kind"] == "not_dispatched"
    assert models.critic.calls == 0


@pytest.mark.parametrize(
    "policy,expected", [("optional", EXIT_OK), ("required", EXIT_FAILED)]
)
def test_offline_vision_policy_is_explicit_and_required_blocks(
    tmp_path, monkeypatch, policy, expected
):
    import dataclasses

    from engine.core.manifest import load_manifest

    original = load_manifest(
        Path(__file__).resolve().parents[3] / "profiles/hylogger/profile.json"
    )
    capabilities = dict(original.capabilities)
    capabilities["image_critique"] = {
        **capabilities["image_critique"],
        "critic_policy": policy,
    }
    manifest = dataclasses.replace(original, capabilities=capabilities)
    monkeypatch.setattr(cli_module, "load_manifest", lambda path: manifest)
    fixed = tmp_path / "fixed-plan.json"
    fixed.write_text(json.dumps({"seed": 8, "n_rows": 8}))
    models = _BothModels({"seed": 8, "n_rows": 8}, "unused")
    code, artifact = _run_with_images(
        tmp_path,
        monkeypatch,
        models,
        extra_argv=("--plan", str(fixed), "--model-policy", "offline"),
    )
    assert code == expected
    assert models.critic.calls == models.planner.calls == 0
    log = RunLog.read_jsonl(artifact / "run.jsonl")
    receipts = [
        e["details"]["payload"]
        for e in log.events
        if e.get("stage") == "image_critique:advisory"
        and e["details"].get("name") == "vision_note"
    ]
    assert receipts and receipts[0]["status"] == "unavailable"
    if policy == "required":
        assert not (artifact / "release.zip").exists()


def test_image_requests_cannot_silently_become_scalar(tmp_path):
    from types import SimpleNamespace

    from profiles.hylogger.capabilities import ImageIntakeError, image_intake

    ctx = SimpleNamespace(images_dir=tmp_path, spec_record=lambda: {})
    with pytest.raises(ImageIntakeError, match="no PNG/JPEG"):
        image_intake(ctx)
    ctx.spec_record = lambda: {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1}
    }
    with pytest.raises(ImageIntakeError, match="separate extension"):
        image_intake(ctx)


def test_source_changed_between_inventory_and_statistics_is_refused(
    tmp_path, monkeypatch
):
    import dataclasses
    from types import SimpleNamespace

    from profiles.hylogger import capabilities

    images = _write_images(tmp_path)
    records = capabilities.inventory_images(images)
    monkeypatch.setattr(
        capabilities,
        "inventory_images",
        lambda folder: [dataclasses.replace(records[0], sha256="0" * 64)],
    )
    ctx = SimpleNamespace(
        images_dir=images,
        spec_record=lambda: {},
        register_source_artifact=lambda *args: None,
    )
    with pytest.raises(capabilities.ImageIntakeError, match="changed during"):
        capabilities.image_intake(ctx)
