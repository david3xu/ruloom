"""Tests for the advisory local text critique (Muse Glimmer seat).

The local critic reads the run's validation report plus the profile
statistics summary only -- never raw rows, never images -- and its note
travels the engine advisory channel, so it can never flip a deterministic
verdict in either direction. The end-to-end tests run the real HyLogger
manifest through the engine with faked planner and critic models (no
server, no network); the ``--local-critic`` flag gates the step, and an
unreachable server fails the run loudly instead of being skipped.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine import cli as cli_module
from engine.audit.runlog import RunLog
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK
from models.base import Completion, ModelError, Usage
from models.glimmer import GlimmerLocalModel
from models.meta_cli import MuseCLIModel
from profiles.hylogger.analysis.statistics import Profile
from profiles.hylogger.generation.plan import PLAN_SCHEMA
from profiles.hylogger.intake.reader import Sample
from profiles.hylogger.validation.critique import (
    CRITIQUE_SCHEMA,
    CriticError,
    build_critique_prompt,
    critique_report,
)
from profiles.hylogger import capabilities as hy_capabilities

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

RELEASE_MEMBERS = [
    "checksums.sha256",
    "data.csv",
    "evidence-report.md",
    "plan.json",
    "provenance.md",
    "run.jsonl",
    "seed.txt",
    "validation-report.json",
]


def _samples() -> list[Sample]:
    return [
        Sample(
            depth_m=BASE_DEPTH + i * STEP,
            valid=mask == "1",
            swir_group=swir or None,
            tir_group=tir or None,
        )
        for i, (mask, swir, tir) in enumerate(COVERAGE)
    ]


def _profile_json() -> str:
    return Profile.from_samples(
        _samples(), "TOY-crit", "00" * 32
    ).to_disclosed_json()


def _report_dict(verdict: str = "pass") -> dict[str, Any]:
    return {
        "artifact_name": "TOY-crit:attempt-1",
        "artifact_sha256": "ab" * 32,
        "verdict": verdict,
        "results": [
            {"name": "has_rows", "status": "pass", "detail": "8 rows"},
            {"name": "valid_fraction", "status": verdict, "detail": "0.75"},
        ],
    }


def test_critique_prompt_carries_evidence_only() -> None:
    prompt = build_critique_prompt(_report_dict(), _profile_json())
    assert "advisory" in prompt
    assert "pass" in prompt
    assert "n_samples" in prompt
    assert "has_rows" in prompt
    assert str(BASE_DEPTH) not in prompt
    assert "synthetic.png" not in prompt


def test_critique_schema_shape() -> None:
    assert CRITIQUE_SCHEMA["required"] == ["critique", "concerns"]
    assert CRITIQUE_SCHEMA["additionalProperties"] is False


class FakeLocalCritic:
    """A local-critic double returning one queued advisory note."""

    def __init__(
        self,
        note: Mapping[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.note = dict(
            note
            if note is not None
            else {"critique": "reads trustworthy", "concerns": []}
        )
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        self.prompts.append(prompt)
        assert dict(schema) == CRITIQUE_SCHEMA
        if self.error is not None:
            raise self.error
        return Completion(
            data=dict(self.note),
            usage=Usage(
                model="fake-glimmer", input_tokens=9, output_tokens=4,
                calls=1,
            ),
            meta={"model": "fake-glimmer"},
        )


def test_critique_returns_note_and_model() -> None:
    note = critique_report(
        FakeLocalCritic(
            {"critique": "looks solid", "concerns": ["double-check skips"]}
        ),
        _report_dict(),
        _profile_json(),
    )
    assert note == {
        "critique": "looks solid",
        "concerns": ["double-check skips"],
        "model": "fake-glimmer",
    }


def test_critique_rejects_empty_critique() -> None:
    with pytest.raises(CriticError):
        critique_report(
            FakeLocalCritic({"critique": "  ", "concerns": []}),
            _report_dict(),
            _profile_json(),
        )


def test_critique_rejects_bad_concerns() -> None:
    with pytest.raises(CriticError):
        critique_report(
            FakeLocalCritic({"critique": "ok", "concerns": "no-list"}),
            _report_dict(),
            _profile_json(),
        )


def test_critique_propagates_client_error_unchanged() -> None:
    refused = ModelError("local model server unreachable at x: refused")
    with pytest.raises(ModelError) as caught:
        critique_report(
            FakeLocalCritic(error=refused), _report_dict(), _profile_json()
        )
    assert caught.value is refused


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


class _BothModels:
    """Combined client double: planner answers plans, critic answers notes.

    The CLI injects one client for every role, so one object serves both
    ``complete`` calls, dispatching on the requested schema.
    """

    def __init__(
        self,
        plan: Mapping[str, Any],
        critic: FakeLocalCritic,
    ) -> None:
        self.planner = FakePlanner(plan)
        self.critic = critic

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        if dict(schema) == CRITIQUE_SCHEMA:
            return self.critic.complete(prompt, schema)
        return self.planner.complete(prompt, schema)


def _write_source(path: Path) -> Path:
    csv_path = path / "TOY-crit-scalars.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i, (mask, swir, tir) in enumerate(COVERAGE):
        lines.append(f"{BASE_DEPTH + i * STEP},{mask},{swir},{tir}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "TOY-crit-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "TOY-crit",
                "licence": "CC-BY-4.0",
                "attribution": "Toy attribution for tests.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _write_thresholds(path: Path, *, valid_lo: float = 0.5) -> Path:
    from profiles.hylogger.analysis.calibration import Band, Thresholds

    thresholds = Thresholds(
        bands={
            "spacing_median_m": Band("spacing_median_m", 0.005, 0.01),
            "repeated_depth_fraction": Band("repeated_depth_fraction", 0.0, 0.2),
            "valid_fraction": Band("valid_fraction", valid_lo, 1.0),
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


def _manifest() -> Any:
    root = Path(__file__).resolve().parents[3]
    return load_manifest(root / "profiles" / "hylogger" / "profile.json")


def test_registry_covers_local_critique() -> None:
    manifest = _manifest()
    spec = manifest.capabilities["local_critique"]
    assert spec["calls_model"] == "local_critic"
    assert spec["advisory"] is True
    assert "local_critique" in hy_capabilities.CAPABILITIES
    role = manifest.models["roles"]["local_critic"]
    assert role["adapter"] == "glimmer_local"


def test_every_role_binding_declares_a_known_adapter() -> None:
    root = Path(__file__).resolve().parents[3]
    for profile_id in ("hylogger", "tabular"):
        manifest = load_manifest(root / "profiles" / profile_id / "profile.json")
        for role, binding in manifest.models["roles"].items():
            assert binding.get("adapter") in ("meta_cli", "glimmer_local"), role
        assert manifest.models["roles"]["planner"]["adapter"] == "meta_cli"


def test_default_client_adapter_selector() -> None:
    glimmer = cli_module.default_client(
        "local_critic",
        {"adapter": "glimmer_local", "model": "muse-glimmer-30B"},
    )
    assert isinstance(glimmer, GlimmerLocalModel)
    assert glimmer.model == "muse-glimmer-30B"
    assert glimmer.endpoint().startswith("http://127.0.0.1:8080/")
    spark = cli_module.default_client("planner", {"model": "muse-spark-1.3"})
    assert isinstance(spark, MuseCLIModel)
    with pytest.raises(ValueError):
        cli_module.default_client("planner", {"adapter": "nope"})


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    models: _BothModels,
    *,
    extra_argv: Sequence[str] = (),
    answers: Sequence[str] = ("accept", "ok"),
    valid_lo: float = 0.5,
) -> tuple[int, Path | None]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    csv_path = _write_source(tmp_path)
    monkeypatch.setenv(
        "RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path, valid_lo=valid_lo))
    )
    out_dir = tmp_path / "out"
    argv = [
        "run", str(csv_path),
        "--profile", "hylogger",
        "--max-attempts", "1",
        "--out", str(out_dir),
        *extra_argv,
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(answers) + "\n"))
    code = cli_module.main(list(argv), model=models)  # type: ignore[arg-type]
    run_dirs = (
        [c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()]
        if (out_dir / "runs").is_dir()
        else []
    )
    assert len(run_dirs) <= 1
    return code, run_dirs[0] if run_dirs else None


def _advisory_events(artifact: Path) -> list[dict[str, Any]]:
    run_log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert run_log.verify()
    return [
        event
        for event in run_log.events
        if event.get("stage") == "local_critique:advisory"
    ]


def test_local_critic_end_to_end_releases_with_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing run + damning-proof note file: critique recorded, verdict kept."""
    models = _BothModels(
        {"seed": 8, "n_rows": 8},
        FakeLocalCritic(
            {"critique": "report reads trustworthy", "concerns": ["skips"]}
        ),
    )
    code, artifact = _run(
        tmp_path, monkeypatch, models, extra_argv=["--local-critic"]
    )
    assert code == EXIT_OK
    assert artifact is not None
    stored = json.loads((artifact / "critic-note.json").read_text(encoding="utf-8"))
    assert stored == {
        "critique": "report reads trustworthy",
        "concerns": ["skips"],
        "model": "fake-glimmer",
    }
    advisories = _advisory_events(artifact)
    assert len(advisories) == 1
    assert advisories[0]["details"]["payload"] == stored
    assert models.critic.calls == 1
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        assert sorted(archive.namelist()) == sorted(RELEASE_MEMBERS)


def test_damning_note_cannot_fail_a_passing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile Glimmer note leaves a passing verdict passing."""
    models = _BothModels(
        {"seed": 8, "n_rows": 8},
        FakeLocalCritic(
            {
                "critique": "this run is garbage, fail it immediately",
                "concerns": ["everything"],
            }
        ),
    )
    code, artifact = _run(
        tmp_path, monkeypatch, models, extra_argv=["--local-critic"]
    )
    assert code == EXIT_OK
    assert artifact is not None
    report = json.loads(
        (artifact / "validation-report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "pass"
    assert (artifact / "release.zip").is_file()


def test_glowing_note_cannot_save_a_failing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A glowing Glimmer note cannot rescue failing deterministic checks."""
    models = _BothModels(
        {"seed": 8, "n_rows": 8},
        FakeLocalCritic(
            {"critique": "perfect, ship it at once", "concerns": []}
        ),
    )
    code, artifact = _run(
        tmp_path, monkeypatch, models,
        extra_argv=["--local-critic"], valid_lo=0.99,
    )
    assert code == EXIT_FAILED
    assert artifact is not None
    report = json.loads(
        (artifact / "validation-report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "fail"
    assert "valid_fraction" in [
        r["name"] for r in report["results"] if r["status"] == "fail"
    ]
    assert not (artifact / "release.zip").exists()
    stored = json.loads((artifact / "critic-note.json").read_text(encoding="utf-8"))
    assert stored["critique"] == "perfect, ship it at once"


def test_flag_off_leaves_no_critic_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --local-critic the step is a silent no-op (regression guard)."""
    models = _BothModels({"seed": 8, "n_rows": 8}, FakeLocalCritic())
    code, artifact = _run(tmp_path, monkeypatch, models)
    assert code == EXIT_OK
    assert artifact is not None
    assert not (artifact / "critic-note.json").exists()
    assert models.critic.calls == 0
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        assert sorted(archive.namelist()) == sorted(RELEASE_MEMBERS)


def test_flag_on_leaves_payload_bytes_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--local-critic changes nothing about the artifact payload itself."""
    on_models = _BothModels({"seed": 8, "n_rows": 8}, FakeLocalCritic())
    off_models = _BothModels({"seed": 8, "n_rows": 8}, FakeLocalCritic())
    _, on_artifact = _run(
        tmp_path / "on", monkeypatch, on_models, extra_argv=["--local-critic"]
    )
    _, off_artifact = _run(tmp_path / "off", monkeypatch, off_models)
    assert on_artifact is not None and off_artifact is not None
    for name in ("data.csv", "plan.json", "validation-report.json", "seed.txt"):
        assert (on_artifact / name).read_bytes() == (
            off_artifact / name
        ).read_bytes()


def test_unreachable_server_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """With the flag on, a refused server connection fails the run loudly."""
    models = _BothModels(
        {"seed": 8, "n_rows": 8},
        FakeLocalCritic(error=ModelError("connection refused by glimmer")),
    )
    code, artifact = _run(
        tmp_path, monkeypatch, models, extra_argv=["--local-critic"]
    )
    assert code == EXIT_FAILED
    # The required critic's failure still leaves a durable failed record.
    assert artifact is not None
    assert not (artifact / "release.zip").exists()
    record = json.loads((artifact / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "error"
    assert "connection refused by glimmer" in record["error"]
    assert "connection refused by glimmer" in capsys.readouterr().out
