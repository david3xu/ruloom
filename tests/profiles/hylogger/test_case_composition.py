"""Tests for HyLogger case composition and capabilities wiring."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from models.base import Completion, Usage
from profiles.hylogger.capabilities import (
    CAPABILITIES,
    case_candidate_review,
    case_repair,
    case_research,
    case_source_assess,
    intake,
    package_artifact,
    validate,
    verify_case_members,
)
from profiles.hylogger.validation.workflow import (
    CASE_CHECK_IDS,
)


def _make_source_csv(
    tmp_path: Path,
    n_rows: int = 120,
    depth_start: float = 0.0,
    spacing: float = 0.0075,
) -> Path:
    cols = [
        "StartDepth",
        "EndDepth",
        "Final_Mask",
        "Grp1_sTSAS",
        "Grp1_sjCLST",
    ]
    path = tmp_path / "test-scalars.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        depth = depth_start
        for i in range(n_rows):
            is_valid = (i % 4) != 0
            swir = "CHLORITE" if (is_valid and (i % 5 != 0)) else ""
            tir = "AMPHIBOLE" if (is_valid and (i % 6 != 0)) else ""
            row = [
                f"{depth:.4f}",
                f"{depth + spacing:.4f}",
                "1" if is_valid else "0",
                swir,
                tir,
            ]
            writer.writerow(row)
            depth += spacing

    manifest_p = tmp_path / "test-source.json"
    raw_bytes = path.read_bytes()
    manifest_p.write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "licence": "CC-BY-4.0",
                "attribution": "Test Suite",
                "output_terms": "CC0-1.0",
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_candidate_csv(
    n_rows: int = 200,
    depth_start: float = 0.0,
    spacing: float = 0.0075,
) -> bytes:
    output = ["depth_m,valid,swir_group,tir_group\n"]
    depth = depth_start
    for i in range(n_rows):
        is_val = (i % 4) != 0
        sw = "CHLORITE" if (is_val and (i % 5 != 0)) else ""
        ti = "AMPHIBOLE" if (is_val and (i % 6 != 0)) else ""
        flag = "true" if is_val else "false"
        output.append(f"{depth:.4f},{flag},{sw},{ti}\n")
        depth += spacing
    return "".join(output).encode("utf-8")


CASE_REF = "hylogger_scalar_teaching_v1@1"


class _RequestModel:
    """Fixture request model: counts calls, keeps prompts, may touch the source."""

    def __init__(self, n_rows: int = 200, on_call: Any = None) -> None:
        self.n_rows = n_rows
        self.on_call = on_call
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, schema: Any) -> Completion:
        self.calls += 1
        self.prompts.append(prompt)
        if self.on_call is not None:
            self.on_call()
        return Completion(
            {
                "summary": "Practice rows for a lesson.",
                "unsupported": [],
                "n_rows": self.n_rows,
                "seed": 8,
                "outputs": ["scalar"],
                "strategy": "fitted_markov_balanced",
            },
            Usage(model="fixture-only", calls=1),
        )


def _prepare_case(tmp_path: Path, source: Path, model: Any) -> int:
    from engine import cli

    return cli.main(
        [
            "prepare",
            str(source),
            "--profile",
            "hylogger",
            "--case",
            CASE_REF,
            "--request",
            "Create practice rows for a data-checking lesson.",
            "--max-attempts",
            "1",
            "--out",
            str(tmp_path / "draft"),
            "--role-adapter",
            "planner=glimmer_local",
        ],
        model=model,
    )


def _receipt_without_draft(tmp_path: Path) -> dict[str, Any]:
    draft = tmp_path / "draft"
    assert not (draft / "spec.json").exists()
    assert not (draft / "plan.json").exists()
    return json.loads((draft / "request.json").read_text())


def test_suitable_source_and_feasible_count_prepare_a_draft_without_rows(
    tmp_path: Path,
) -> None:
    source = _make_source_csv(tmp_path)
    model = _RequestModel(n_rows=200)
    assert _prepare_case(tmp_path, source, model) == 0
    assert model.calls == 1
    draft = tmp_path / "draft"
    spec = json.loads((draft / "spec.json").read_text())
    assert spec["case"] == {"id": "hylogger_scalar_teaching_v1", "version": 1}
    assert spec["target_rows"] == 200  # the count is never changed
    receipt = json.loads((draft / "request.json").read_text())
    assert receipt["case_preflight"]["source_fit"] == "suitable"
    assert receipt["case_target_check"]["n_rows"] == 200
    assert receipt["source_rows_sent"] is False
    prompt = model.prompts[0]
    # No source row content, label vocabulary or licence text reaches the model.
    for leaked in ("CHLORITE", "AMPHIBOLE", "CC-BY-4.0", "Test Suite", "0.0075"):
        assert leaked not in prompt


def test_unsuitable_source_is_refused_before_the_request_model(
    tmp_path: Path,
) -> None:
    source = _make_source_csv(tmp_path, n_rows=10)  # fewer rows than the case needs
    model = _RequestModel()
    assert _prepare_case(tmp_path, source, model) == 1
    assert model.calls == 0
    receipt = _receipt_without_draft(tmp_path)
    assert receipt["status"] == "error"
    assert "not suitable" in receipt["error"]
    assert "case_preflight" not in receipt


def test_infeasible_count_is_refused_after_interpretation_before_any_draft(
    tmp_path: Path,
) -> None:
    source = _make_source_csv(tmp_path)
    model = _RequestModel(n_rows=10)  # below the case's minimum satisfiable rows
    assert _prepare_case(tmp_path, source, model) == 1
    assert model.calls == 1  # the count exists only after interpretation
    receipt = _receipt_without_draft(tmp_path)
    assert (
        "requested row count 10 was refused before any draft was written"
        in (receipt["error"])
    )
    assert receipt["case_preflight"]["source_fit"] == "suitable"
    assert "case_target_check" not in receipt


def test_source_changed_while_the_model_interprets_is_refused(tmp_path: Path) -> None:
    source = _make_source_csv(tmp_path)

    def change_source() -> None:
        source.write_text(source.read_text() + "0.9000,0.9075,1,CHLORITE,AMPHIBOLE\n")

    model = _RequestModel(on_call=change_source)
    assert _prepare_case(tmp_path, source, model) == 1
    assert (
        "source changed during preparation"
        in (_receipt_without_draft(tmp_path)["error"])
    )


def test_source_changing_after_the_draft_is_written_leaves_no_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine import cli
    from engine.cli.request import CasePreparationHooks

    real = cli.load_case_preparation_hooks("hylogger")
    confirms = {"n": 0}

    def confirm_then_fail_on_the_last_look(handle: Any) -> None:
        confirms["n"] += 1
        if confirms["n"] >= 2:  # the check after the draft files are written
            raise ValueError("source changed during preparation: after writing")
        real.confirm_unchanged(handle)

    monkeypatch.setattr(
        cli,
        "load_case_preparation_hooks",
        lambda profile_id: CasePreparationHooks(
            preflight=real.preflight,
            check_target=real.check_target,
            confirm_unchanged=confirm_then_fail_on_the_last_look,
        ),
    )
    source = _make_source_csv(tmp_path)
    assert _prepare_case(tmp_path, source, _RequestModel()) == 1
    assert confirms["n"] == 2
    assert "after writing" in _receipt_without_draft(tmp_path)["error"]


def test_source_confirmation_oserror_after_writes_removes_both_draft_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify OSError on second confirmation removes both draft files."""
    from engine import cli
    from engine.cli.request import CasePreparationHooks

    real = cli.load_case_preparation_hooks("hylogger")
    confirms = {"n": 0}

    def confirm_then_fail_with_oserror(handle: Any) -> None:
        confirms["n"] += 1
        if confirms["n"] >= 2:  # the check after the draft files are written
            raise OSError("simulated post-write registry read error")
        real.confirm_unchanged(handle)

    monkeypatch.setattr(
        cli,
        "load_case_preparation_hooks",
        lambda profile_id: CasePreparationHooks(
            preflight=real.preflight,
            check_target=real.check_target,
            confirm_unchanged=confirm_then_fail_with_oserror,
        ),
    )
    source = _make_source_csv(tmp_path)
    assert _prepare_case(tmp_path, source, _RequestModel()) == 1
    assert confirms["n"] == 2
    receipt = _receipt_without_draft(tmp_path)
    assert receipt["status"] == "error"
    assert "simulated post-write registry read error" in receipt["error"]
    assert "OSError" in receipt["error"]


def test_preflight_evidence_carries_no_rows_vocabulary_or_licence_text(
    tmp_path: Path,
) -> None:
    from profiles.hylogger.capabilities import prepare_case_preflight

    source = _make_source_csv(tmp_path)
    _handle, evidence = prepare_case_preflight(
        source,
        {"id": "hylogger_scalar_teaching_v1", "version": 1},
        {"data_suffix": "-scalars.csv", "manifest_suffix": "-source.json"},
    )
    assert set(evidence) == {
        "case_id",
        "case_version",
        "source_sha256",
        "source_fit",
    }
    text = json.dumps(evidence)
    for leaked in ("CHLORITE", "AMPHIBOLE", "CC-BY-4.0", "Test Suite", "CC0-1.0"):
        assert leaked not in text


def test_target_check_refuses_infeasible_counts_and_never_changes_them(
    tmp_path: Path,
) -> None:
    from profiles.hylogger.capabilities import (
        check_case_target_rows,
        prepare_case_preflight,
    )

    handle, _ = prepare_case_preflight(
        _make_source_csv(tmp_path),
        {"id": "hylogger_scalar_teaching_v1", "version": 1},
        {"data_suffix": "-scalars.csv", "manifest_suffix": "-source.json"},
    )
    assert check_case_target_rows(handle, 200)["case_id"] == (
        "hylogger_scalar_teaching_v1"
    )
    with pytest.raises(ValueError):
        check_case_target_rows(handle, 10)
    for bad in (0, -5, True, 200.0, "200"):
        with pytest.raises(ValueError):
            check_case_target_rows(handle, bad)
    with pytest.raises(TypeError):
        check_case_target_rows(object(), 200)


def test_source_and_registry_record_changes_are_detected(tmp_path: Path) -> None:
    from profiles.hylogger.capabilities import (
        confirm_case_source_unchanged,
        prepare_case_preflight,
    )

    cfg = {"data_suffix": "-scalars.csv", "manifest_suffix": "-source.json"}
    case = {"id": "hylogger_scalar_teaching_v1", "version": 1}
    source = _make_source_csv(tmp_path)
    handle, _ = prepare_case_preflight(source, case, cfg)
    confirm_case_source_unchanged(handle)  # untouched: fine
    sidecar = tmp_path / "test-source.json"
    original = sidecar.read_text()
    sidecar.write_text(original.replace("Test Suite", "Someone Else"))
    with pytest.raises(ValueError, match="registry record changed"):
        confirm_case_source_unchanged(handle)
    sidecar.unlink()
    with pytest.raises(ValueError, match="appeared or vanished"):
        confirm_case_source_unchanged(handle)
    sidecar.write_text(original)
    source.write_text(source.read_text() + "0.9,0.9075,1,CHLORITE,AMPHIBOLE\n")
    with pytest.raises(ValueError, match="source changed during preparation"):
        confirm_case_source_unchanged(handle)


def test_run_intake_still_refuses_independently_of_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from profiles.hylogger.validation.workflow import prepare_case_context

    monkeypatch.delenv("RULOOM_THRESHOLDS", raising=False)
    case = {"id": "hylogger_scalar_teaching_v1", "version": 1}

    def spec_for(path: Path, rows: int) -> dict[str, Any]:
        return {
            "case": case,
            "target_rows": rows,
            "source": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            "outputs": ["scalar"],
        }

    good = _make_source_csv(tmp_path)
    assert prepare_case_context(good, spec_for(good, 200), "CC-BY-4.0")["binding"]
    with pytest.raises(ValueError):  # the intake's own feasibility check
        prepare_case_context(good, spec_for(good, 10), "CC-BY-4.0")
    short = _make_source_csv(tmp_path / "short", n_rows=10)
    with pytest.raises(ValueError, match="not suitable"):  # and its own suitability
        prepare_case_context(short, spec_for(short, 200), "CC-BY-4.0")


class MockArtifact:
    def __init__(self, sha256: str) -> None:
        self.sha256 = sha256


class MockRunContext:
    """Mock context implementing the engine-provided RunContext interface."""

    def __init__(
        self,
        source_path: Path,
        spec: dict[str, Any],
        model_policy: str = "hosted",
    ) -> None:
        self._source_path = source_path
        self._spec = spec
        self._model_policy = model_policy
        self._requirements: dict[str, Any] | None = None
        self._source_meta: dict[str, Any] = {}
        self._source_frame: list[Any] = []
        self._profile: Any = None
        self._plan: dict[str, Any] = {
            "seed": 42,
            "n_rows": 200,
            "strategy": "fitted_markov_joint",
        }
        self._reports: dict[str, Any] = {}
        self._latest_report: Any = None
        self._recorded_checks: list[dict[str, Any]] = []
        self._advisories: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._messages: list[str] = []
        self._artifacts: dict[str, bytes] = {}
        self._latest_artifact_sha: str | None = None
        self._artifact_dir: Path | None = None
        self._models: dict[str, Any] = {}

    @property
    def source_path(self) -> Path:
        return self._source_path

    @property
    def source_config(self) -> dict[str, Any]:
        return {"data_suffix": "-scalars.csv", "manifest_suffix": "-source.json"}

    @property
    def model_policy(self) -> str:
        return self._model_policy

    @property
    def attempt_number(self) -> int:
        return 1

    @property
    def local_critic_enabled(self) -> bool:
        return False

    @property
    def declared_checks(self) -> tuple[dict[str, Any], ...]:
        return ()

    def raw_bytes(self) -> bytes:
        return self._source_path.read_bytes()

    def spec_record(self) -> dict[str, Any]:
        return dict(self._spec)

    def spec_digest(self) -> str:
        return "a" * 64

    def requirements(self) -> dict[str, Any] | None:
        return dict(self._requirements) if self._requirements is not None else None

    def set_requirements(self, record: dict[str, Any]) -> None:
        if self._requirements is not None:
            raise RuntimeError("requirements write-once violation")
        self._requirements = dict(record)

    def set_source_meta(self, **kwargs: Any) -> None:
        if "name" in kwargs:
            kwargs["source_name"] = kwargs["name"]
        if "sha256" in kwargs:
            kwargs["source_sha256"] = kwargs["sha256"]
        self._source_meta.update(kwargs)

    def source_meta(self) -> dict[str, Any]:
        return dict(self._source_meta)

    def set_source_frame(self, frame: list[Any]) -> None:
        self._source_frame = list(frame)

    def set_profile(self, profile: Any) -> None:
        self._profile = profile

    def profile(self) -> Any:
        return self._profile

    def reference_stats(self) -> dict[str, Any]:
        return {}

    def plan(self) -> dict[str, Any]:
        return dict(self._plan)

    def set_plan(self, plan: dict[str, Any]) -> None:
        self._plan = dict(plan)

    def selected_plan(self) -> dict[str, Any]:
        return dict(self._plan)

    def companion(self) -> dict[str, Any]:
        return {}

    def latest_artifact(self) -> MockArtifact:
        if not self._latest_artifact_sha:
            raise RuntimeError("no artifact")
        return MockArtifact(self._latest_artifact_sha)

    def artifact_payload(self, sha: str) -> bytes:
        return self._artifacts[sha]

    def set_artifact(self, payload: bytes) -> str:
        sha = hashlib.sha256(payload).hexdigest()
        self._artifacts[sha] = payload
        self._latest_artifact_sha = sha
        return sha

    def selected_artifact_sha(self) -> str:
        return self._latest_artifact_sha or ""

    def set_report(self, report: Any) -> None:
        self._latest_report = report
        if self._latest_artifact_sha:
            self._reports[self._latest_artifact_sha] = report

    def latest_report(self) -> Any:
        return self._latest_report

    def report_for(self, sha: str) -> Any:
        return self._reports[sha]

    def record_checks(self, items: list[dict[str, Any]]) -> None:
        self._recorded_checks.extend(items)

    def record_advisory(self, name: str, payload: Any) -> None:
        self._advisories.append({"name": name, "payload": payload})

    def advisory(self, name: str) -> Any:
        for a in reversed(self._advisories):
            if a["name"] == name:
                return a["payload"]
        return None

    def event(
        self,
        stage: str,
        status: str,
        details: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._events.append(
            {"stage": stage, "status": status, "details": details or {}}
        )

    def say(self, message: str) -> None:
        self._messages.append(message)

    def prepare_artifact_dir(self) -> Path:
        p = self._source_path.parent / "artifact_out"
        p.mkdir(parents=True, exist_ok=True)
        self._artifact_dir = p
        return p

    def provenance_text(self) -> str:
        return "# Provenance\nTest provenance"

    def flush_log(self, path: Path) -> None:
        path.write_text("{}\n", encoding="utf-8")

    def model(self, role: str) -> Any:
        return self._models[role]


def test_capabilities_export():
    """Verify CAPABILITIES dict contains case capabilities and verify_case_members is exported."""
    assert "case_source_assess" in CAPABILITIES
    assert "case_research" in CAPABILITIES
    assert "case_repair" in CAPABILITIES
    assert "case_candidate_review" in CAPABILITIES
    assert callable(verify_case_members)


def test_intake_prepares_case_context(tmp_path: Path):
    """Verify intake prepares immutable requirements for a case-selected run."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    raw_hash = hashlib.sha256(source_p.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec)
    intake(ctx)

    req = ctx.requirements()
    assert req is not None
    assert req["binding"]["case_id"] == "hylogger_scalar_teaching_v1"
    assert req["binding"]["case_version"] == 1
    assert req["binding"]["source_sha256"] == raw_hash
    assert "contract" in req
    assert "reference" in req
    assert "source_fit_receipt" in req


def test_intake_refuses_rulooms_thresholds_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify intake strictly refuses RULOOM_THRESHOLDS override for case runs."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    raw_hash = hashlib.sha256(source_p.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec)
    monkeypatch.setenv("RULOOM_THRESHOLDS", "/tmp/fake_thresholds.json")
    with pytest.raises(
        RuntimeError, match="RULOOM_THRESHOLDS environment override is forbidden"
    ):
        intake(ctx)


def test_intake_refuses_source_pin_mismatch(tmp_path: Path):
    """Verify intake refuses source hash pin mismatch before any model call."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": "0" * 64},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec)
    with pytest.raises(RuntimeError, match="source hash mismatch"):
        intake(ctx)


def test_validate_records_all_13_case_checks(tmp_path: Path):
    """Verify validate evaluates candidate bytes and emits all 13 case checks."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    raw_hash = hashlib.sha256(source_p.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec)
    intake(ctx)

    cand_bytes = _make_candidate_csv(n_rows=200)
    ctx.set_artifact(cand_bytes)
    validate(ctx)

    recorded_ids = [c["check_id"] for c in ctx._recorded_checks]
    for cid in CASE_CHECK_IDS:
        assert cid in recorded_ids

    report = ctx.latest_report()
    assert hasattr(report, "case_utility")
    assert (
        report.case_utility["candidate_sha256"]
        == hashlib.sha256(cand_bytes).hexdigest()
    )


def test_case_repair_deterministic_strategy_switch(tmp_path: Path):
    """Verify case_repair causally switches fitted_markov_joint to fitted_markov_balanced on inclusion failure."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    raw_hash = hashlib.sha256(source_p.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec)
    intake(ctx)

    # Initial plan with joint strategy and seed
    ctx.set_plan({"seed": 77, "n_rows": 200, "strategy": "fitted_markov_joint"})

    class MockReportWithFailure:
        failed = ["case.inclusion_fraction"]
        case_utility = {"failures": ["inclusion_fraction"]}

    ctx.set_report(MockReportWithFailure())
    case_repair(ctx)

    repaired = ctx.plan()
    assert repaired["strategy"] == "fitted_markov_balanced"
    assert repaired["seed"] == 77
    assert repaired["n_rows"] == 200

    # Running repair again on balanced strategy yields unchanged plan
    case_repair(ctx)
    repaired2 = ctx.plan()
    assert repaired2["strategy"] == "fitted_markov_balanced"
    assert repaired2["seed"] == 77


def test_package_artifact_writes_all_5_case_members(tmp_path: Path):
    """Verify package_artifact builds and writes all 5 flat case members."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    raw_hash = hashlib.sha256(source_p.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec)
    intake(ctx)

    cand_bytes = _make_candidate_csv(n_rows=200)
    ctx.set_artifact(cand_bytes)
    validate(ctx)
    package_artifact(ctx)

    art_dir = ctx._artifact_dir
    assert art_dir is not None
    assert (art_dir / "data.csv").is_file()
    assert (art_dir / "case-binding.json").is_file()
    assert (art_dir / "case-contract.json").is_file()
    assert (art_dir / "case-reference.json").is_file()
    assert (art_dir / "case-utility.json").is_file()
    assert (art_dir / "case-evidence.json").is_file()

    # Validate binding member
    binding = json.loads((art_dir / "case-binding.json").read_text(encoding="utf-8"))
    assert binding["case_id"] == "hylogger_scalar_teaching_v1"
    assert binding["case_version"] == 1
    assert binding["candidate_sha256"] == hashlib.sha256(cand_bytes).hexdigest()


def test_advisory_roles_offline_recording(tmp_path: Path):
    """Verify advisory roles record 'unavailable' under offline model policy with zero model calls."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    raw_hash = hashlib.sha256(source_p.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec, model_policy="offline")
    intake(ctx)

    cand_bytes = _make_candidate_csv(n_rows=200)
    ctx.set_artifact(cand_bytes)
    validate(ctx)

    case_source_assess(ctx)
    case_research(ctx)
    case_candidate_review(ctx)

    adv_map = {a["name"]: a["payload"] for a in ctx._advisories}
    assert adv_map["case_source_assess"]["status"] == "unavailable"
    assert adv_map["case_research"]["status"] == "unavailable"
    assert adv_map["case_candidate_review"]["status"] == "unavailable"


def test_advisory_roles_mock_invocations(tmp_path: Path):
    """Verify advisory roles execute and record valid responses when models are present."""
    source_p = _make_source_csv(tmp_path, n_rows=120)
    raw_hash = hashlib.sha256(source_p.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(source_p, spec, model_policy="hosted")
    intake(ctx)

    cand_bytes = _make_candidate_csv(n_rows=200)
    ctx.set_artifact(cand_bytes)
    validate(ctx)

    class MockModel:
        def __init__(self, response: dict[str, Any]) -> None:
            self._resp = response

        def complete(self, prompt: str, schema: Any) -> Completion:
            return Completion(
                data=self._resp,
                usage=Usage(model="mock", input_tokens=10, output_tokens=10, calls=1),
                meta={"model": "mock"},
            )

    # 1. source assessor
    ctx._models["source_assessor"] = MockModel(
        {
            "case_id": "hylogger_scalar_teaching_v1",
            "source_sha256": raw_hash,
            "cited_evidence_ids": ["auscope_nvcl"],
            "fit_assessment": "Suitable bounded scalar teaching source",
            "source_limitations": ["Engineering similarity only"],
            "teaching_suitability": "Suitable for classroom instruction",
        }
    )
    case_source_assess(ctx)
    assert (
        ctx.advisory("case_source_assess")["case_id"] == "hylogger_scalar_teaching_v1"
    )

    # 2. case research
    ctx._models["case_researcher"] = MockModel(
        {
            "case_id": "hylogger_scalar_teaching_v1",
            "case_version": 1,
            "cited_evidence_ids": ["auscope_nvcl"],
            "passage_citations": [
                {
                    "evidence_id": "auscope_nvcl",
                    "passage_sha256": "df25a037e87cfe17683b993e3bcdc45986b21e9597f9978419c62b31df37fe4b",
                    "relevance": "Section on data quality",
                }
            ],
            "supported_requirements": ["Scalar checking"],
            "unsupported_requirements": [],
            "research_summary": "Verified research summary",
            "limitations": ["Pre-admitted evidence only"],
        }
    )
    case_research(ctx)
    assert ctx.advisory("case_research")["case_id"] == "hylogger_scalar_teaching_v1"

    # 3. case candidate review
    ctx._models["case_reviewer"] = MockModel(
        {
            "case_id": "hylogger_scalar_teaching_v1",
            "candidate_sha256": hashlib.sha256(cand_bytes).hexdigest(),
            "code_verdict_acknowledged": "pass",
            "operator_recommendation": "recommend_proceed_to_human_gate",
            "evidence_facts": [{"measurement": "row_count", "value": 200}],
            "evaluator_opinion": "Candidate meets requirements",
            "strengths": ["All checks pass"],
            "concerns": [],
        }
    )
    case_candidate_review(ctx)
    assert (
        ctx.advisory("case_candidate_review")["operator_recommendation"]
        == "recommend_proceed_to_human_gate"
    )


def _make_passing_case_zip(tmp_path: Path) -> Path:
    from engine.artifacts.package import build_checksums, build_zip_bytes

    source_dir = tmp_path / "case_src"
    source_dir.mkdir(parents=True, exist_ok=True)
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i in range(120):
        depth = round(10.0 + i * 0.0075, 4)
        is_valid = (i % 4) != 0
        mask = "1" if is_valid else "0"
        swir = "CHLORITE" if (is_valid and (i % 5 != 0)) else ""
        tir = "AMPHIBOLE" if (is_valid and i not in (1, 5)) else ""
        lines.append(f"{depth},{mask},{swir},{tir}")
    src_csv = source_dir / "TEST-scalars.csv"
    src_csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raw_hash = hashlib.sha256(src_csv.read_bytes()).hexdigest()
    (source_dir / "TEST-source.json").write_text(
        json.dumps(
            {
                "sha256": raw_hash,
                "dataset_name": "TEST",
                "licence": "CC-BY-4.0",
                "attribution": "Test Suite",
                "output_terms": "teaching_only",
            }
        ),
        encoding="utf-8",
    )

    cand_lines = ["depth_m,valid,swir_group,tir_group\n"]
    depth = 10.0
    for i in range(200):
        is_val = (i % 4) != 0
        sw = "CHLORITE" if (is_val and (i % 5 != 0)) else ""
        ti = "AMPHIBOLE" if (is_val and i not in (1, 5)) else ""
        v_str = "true" if is_val else "false"
        cand_lines.append(f"{depth:.4f},{v_str},{sw},{ti}\n")
        depth += 0.0075
    cand_bytes = "".join(cand_lines).encode("utf-8")

    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": raw_hash},
        "outputs": ["scalar"],
    }
    ctx = MockRunContext(src_csv, spec)
    intake(ctx)
    ctx.set_artifact(cand_bytes)
    validate(ctx)
    package_artifact(ctx)
    art_dir = ctx._artifact_dir
    assert art_dir is not None
    (art_dir / "run.jsonl").write_text(
        '{"stage": "run_start", "status": "ok"}\n', encoding="utf-8"
    )
    files = {p.name: p.read_bytes() for p in art_dir.iterdir() if p.is_file()}
    files["checksums.sha256"] = build_checksums(files).encode("utf-8")
    zip_bytes = build_zip_bytes(files)
    zip_path = tmp_path / "case_release.zip"
    zip_path.write_bytes(zip_bytes)
    return zip_path


def _make_legacy_tabular_zip(tmp_path: Path) -> Path:
    from engine.artifacts.package import build_checksums, build_zip_bytes

    payload = b"id,val\n1,10\n"
    report = {
        "artifact_name": "data.csv",
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "verdict": "pass",
        "results": [],
    }
    files = {
        "data.csv": payload,
        "plan.json": b'{"seed": 1, "n_rows": 1}\n',
        "validation-report.json": (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
        "seed.txt": b"1\n",
        "provenance.md": b"# Provenance\n",
        "run.jsonl": b'{"stage": "run_start", "status": "ok"}\n',
    }
    files["checksums.sha256"] = build_checksums(files).encode("utf-8")
    zip_bytes = build_zip_bytes(files)
    zip_path = tmp_path / "legacy_tabular.zip"
    zip_path.write_bytes(zip_bytes)
    return zip_path


def test_verify_explicit_profile_tabular_refuses_case_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review F4: verify --profile tabular refuses case archives without breaking auto-resolution or legacy zips."""
    from engine import cli

    case_zip = _make_passing_case_zip(tmp_path)
    legacy_zip = _make_legacy_tabular_zip(tmp_path)

    # 1. Explicit profile tabular on case release: refuses
    code = cli.main(["verify", str(case_zip), "--profile", "tabular"])
    assert code == 1
    err = capsys.readouterr().err
    assert "profile 'tabular' cannot verify case members" in err

    # 2. Explicit profile hylogger on case release: accepts
    code = cli.main(["verify", str(case_zip), "--profile", "hylogger"])
    assert code == 0
    _ = capsys.readouterr()

    # 3. Omitted profile on case release: auto-resolves hylogger and accepts
    code = cli.main(["verify", str(case_zip)])
    assert code == 0
    _ = capsys.readouterr()

    # 4. Explicit profile tabular on legacy tabular release: accepts
    code = cli.main(["verify", str(legacy_zip), "--profile", "tabular"])
    assert code == 0
    _ = capsys.readouterr()

    # 5. Omitted profile on legacy tabular release: accepts
    code = cli.main(["verify", str(legacy_zip)])
    assert code == 0
    _ = capsys.readouterr()


def test_verify_require_case_strict_positive_integer_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review F5: --require-case enforces strict positive ASCII integer version."""
    from engine import cli

    case_zip = _make_passing_case_zip(tmp_path)

    # Negative controls
    for bad in (
        "hylogger_scalar_teaching_v1@+1",
        "hylogger_scalar_teaching_v1@ 1 ",
        "hylogger_scalar_teaching_v1@1_0",
        "hylogger_scalar_teaching_v1@0",
        "hylogger_scalar_teaching_v1@01",
        "hylogger_scalar_teaching_v1@1.0",
        "hylogger_scalar_teaching_v1@-1",
        "hylogger_scalar_teaching_v1@１",
        "hylogger_scalar_teaching_v1@",
        "hylogger_scalar_teaching_v1",
        "@1",
    ):
        code = cli.main(["verify", str(case_zip), "--require-case", bad])
        assert code == 1, f"Expected 1 for bad case spec {bad!r}, got {code}"
        err = capsys.readouterr().err
        assert "--require-case" in err

    # Positive control
    code = cli.main(
        ["verify", str(case_zip), "--require-case", "hylogger_scalar_teaching_v1@1"]
    )
    assert code == 0
    _ = capsys.readouterr()
