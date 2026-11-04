"""Request drafts use fake models only; source bytes never leave intake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import cli
from engine.artifacts.hashing import sha256_bytes, sha256_canonical
from engine.cli.request_context import build_capability_context
from engine.core.manifest import ManifestError, load_manifest, validate_manifest
from models.base import Completion, Usage

ROOT = Path(__file__).resolve().parents[2]


def _payload(profile="tabular"):
    return json.loads((ROOT / "profiles" / profile / "profile.json").read_text())


class FixtureModel:
    def __init__(self, **changes):
        self.answer = {
            "summary": "Create 20 rows for a demo.",
            "unsupported": [],
            "n_rows": 20,
            "seed": 8,
            "outputs": ["scalar"],
            "strategy": "fitted_gaussian_disclosed",
            **changes,
        }
        self.calls = 0
        self.prompt = ""

    def complete(self, prompt, schema):
        self.calls += 1
        self.prompt = prompt
        return Completion(self.answer, Usage(model="fixture-only", calls=1))


def _prepare(tmp_path, model, extra=()):
    source = tmp_path / "source.csv"
    if not source.exists():
        source.write_text("id,value,label\nprivate-canary,1,common\n")
    return cli.main(
        [
            "prepare",
            str(source),
            "--profile",
            "tabular",
            "--request",
            "Create 20 synthetic rows for a demo.",
            "--max-attempts",
            "1",
            "--out",
            str(tmp_path / "draft"),
            "--role-adapter",
            "planner=glimmer_local",
            *extra,
        ],
        model=model,
    )


class OrderedModel(FixtureModel):
    """Fixture model that records when it is contacted, and may touch the source."""

    def __init__(self, events, on_call=None, **changes):
        super().__init__(**changes)
        self.events = events
        self.on_call = on_call

    def complete(self, prompt, schema):
        self.events.append("model")
        if self.on_call is not None:
            self.on_call()
        return super().complete(prompt, schema)


class RecordingHooks:
    """Domain-free stand-in for a profile's case checks; records every call."""

    def __init__(self, events, *, preflight_error=None, target_error=None):
        self.events = events
        self.preflight_error = preflight_error
        self.target_error = target_error

    def preflight(self, source, case, source_config):
        self.events.append("preflight")
        if self.preflight_error:
            raise ValueError(self.preflight_error)
        return object(), {"source_fit": "suitable"}

    def check_target(self, handle, n_rows):
        self.events.append(f"check_target:{n_rows}")
        if self.target_error:
            raise ValueError(self.target_error)
        return {"checked": n_rows}

    def confirm_unchanged(self, handle):
        self.events.append("confirm_unchanged")

    def hooks(self):
        from engine.cli.request import CasePreparationHooks

        return CasePreparationHooks(
            preflight=self.preflight,
            check_target=self.check_target,
            confirm_unchanged=self.confirm_unchanged,
        )


def _declare_case(monkeypatch, hooks):
    """Make the tabular profile declare a case and compose the given hooks."""
    import dataclasses

    manifest = load_manifest(ROOT / "profiles" / "tabular" / "profile.json")
    declared = dataclasses.replace(
        manifest,
        generation={
            **manifest.generation,
            "cases": [{"id": "demo_case", "version": 1}],
        },
    )
    monkeypatch.setattr(cli, "load_manifest", lambda path: declared)
    monkeypatch.setattr(cli, "load_case_preparation_hooks", lambda profile_id: hooks)


CASE_ARGS = ("--case", "demo_case@1")


def _no_draft_files(tmp_path):
    draft = tmp_path / "draft"
    assert not (draft / "spec.json").exists()
    assert not (draft / "plan.json").exists()
    return json.loads((draft / "request.json").read_text())


def test_selected_case_unsuitable_source_is_refused_before_the_model(
    tmp_path, monkeypatch
):
    events = []
    hooks = RecordingHooks(events, preflight_error="source is not suitable: too short")
    _declare_case(monkeypatch, hooks.hooks())
    model = OrderedModel(events)
    assert _prepare(tmp_path, model, CASE_ARGS) == 1
    assert model.calls == 0
    assert events == ["preflight"]  # the model was never reached
    receipt = _no_draft_files(tmp_path)
    assert receipt["status"] == "error"
    assert "source is not suitable" in receipt["error"]
    assert receipt["source_rows_sent"] is False


def test_infeasible_count_is_refused_after_interpretation_before_any_draft(
    tmp_path, monkeypatch
):
    events = []
    hooks = RecordingHooks(events, target_error="20 rows is below the minimum")
    _declare_case(monkeypatch, hooks.hooks())
    model = OrderedModel(events)
    assert _prepare(tmp_path, model, CASE_ARGS) == 1
    # Preflight first, then the model, and only then the count (never earlier:
    # the count exists only after interpretation).
    assert events == ["preflight", "model", "confirm_unchanged", "check_target:20"]
    receipt = _no_draft_files(tmp_path)
    assert (
        "requested row count 20 was refused before any draft was written"
        in (receipt["error"])
    )
    assert receipt["case_preflight"]["status"] == "ok"
    assert "case_target_check" not in receipt  # nothing was recorded as feasible


def test_source_changed_during_interpretation_is_refused(tmp_path, monkeypatch):
    events = []
    hooks = RecordingHooks(events)
    _declare_case(monkeypatch, hooks.hooks())
    source = tmp_path / "source.csv"
    source.write_text("id,value,label\nprivate-canary,1,common\n")

    def change_source():
        source.write_text("id,value,label\nprivate-canary,2,common\n")

    model = OrderedModel(events, on_call=change_source)
    assert _prepare(tmp_path, model, CASE_ARGS) == 1
    receipt = _no_draft_files(tmp_path)
    assert "source changed during preparation" in receipt["error"]
    # The engine's own re-hash caught it before the profile's recheck or count.
    assert not any(e.startswith("check_target") for e in events)
    assert "confirm_unchanged" not in events


def test_source_confirmation_oserror_after_writes_removes_both_draft_files(
    tmp_path, monkeypatch
):
    events = []
    confirms = {"calls": 0}

    class OSErrorOnSecondConfirmHooks(RecordingHooks):
        def confirm_unchanged(self, handle):
            confirms["calls"] += 1
            self.events.append("confirm_unchanged")
            if confirms["calls"] >= 2:
                raise OSError("injected registry read failure")

    hooks = OSErrorOnSecondConfirmHooks(events)
    _declare_case(monkeypatch, hooks.hooks())
    model = OrderedModel(events)
    assert _prepare(tmp_path, model, CASE_ARGS) == 1
    assert confirms["calls"] == 2
    receipt = _no_draft_files(tmp_path)
    assert receipt["status"] == "error"
    assert "OSError" in receipt["error"]
    assert "injected registry read failure" in receipt["error"]
    assert receipt["source_rows_sent"] is False


def test_source_confirmation_positive_control_retains_draft_files(
    tmp_path, monkeypatch
):
    events = []
    confirms = {"calls": 0}

    class PositiveHooks(RecordingHooks):
        def confirm_unchanged(self, handle):
            confirms["calls"] += 1
            self.events.append("confirm_unchanged")

    hooks = PositiveHooks(events)
    _declare_case(monkeypatch, hooks.hooks())
    model = OrderedModel(events)
    assert _prepare(tmp_path, model, CASE_ARGS) == 0
    assert confirms["calls"] == 2
    draft = tmp_path / "draft"
    assert (draft / "spec.json").is_file()
    assert (draft / "plan.json").is_file()
    receipt = json.loads((draft / "request.json").read_text())
    assert receipt["status"] == "draft-needs-human-review"


def test_case_spec_strict_ascii_positive_integer_version():
    from engine.cli.request import parse_case_spec

    # Positive controls
    assert parse_case_spec("demo_case@1") == ("demo_case", 1)
    assert parse_case_spec("demo_case@10") == ("demo_case", 10)
    assert parse_case_spec("  demo_case  @2") == ("demo_case", 2)
    assert parse_case_spec("case_id@99", flag_name="--require-case") == ("case_id", 99)

    # Negative controls
    for bad in (
        "demo_case@+1",
        "demo_case@ 1 ",
        "demo_case@1 ",
        "demo_case@1\t",
        "demo_case@1\n",
        "demo_case@1_0",
        "demo_case@0",
        "demo_case@01",
        "demo_case@1.0",
        "demo_case@-1",
        "demo_case@１",
        "demo_case@",
        "demo_case",
        "@1",
        "  @1",
    ):
        with pytest.raises(ValueError):
            parse_case_spec(bad)


def test_prepare_case_flag_refuses_non_strict_version(tmp_path, monkeypatch):
    events = []
    hooks = RecordingHooks(events)
    _declare_case(monkeypatch, hooks.hooks())
    model = FixtureModel()
    for idx, bad_case in enumerate(
        (
            "demo_case@+1",
            "demo_case@ 1 ",
            "demo_case@1_0",
            "demo_case@0",
            "demo_case@01",
        )
    ):
        case_tmp = tmp_path / f"case_{idx}"
        case_tmp.mkdir()
        assert _prepare(case_tmp, model, ("--case", bad_case)) == 1
        receipt = _no_draft_files(case_tmp)
        assert "version must be a positive integer" in receipt["error"]


def test_selected_case_without_profile_checks_fails_closed_before_the_model(
    tmp_path, monkeypatch
):
    _declare_case(monkeypatch, None)
    model = FixtureModel()
    assert _prepare(tmp_path, model, CASE_ARGS) == 1
    assert model.calls == 0
    assert "supplies no case preparation checks" in (_no_draft_files(tmp_path)["error"])


def test_without_a_selected_case_the_legacy_path_never_loads_case_hooks(
    tmp_path, monkeypatch
):
    def must_not_load(profile_id):
        raise AssertionError("case hooks were loaded without --case")

    monkeypatch.setattr(cli, "load_case_preparation_hooks", must_not_load)
    model = FixtureModel()
    assert _prepare(tmp_path, model) == 0
    receipt = json.loads((tmp_path / "draft" / "request.json").read_text())
    assert "case_preflight" not in receipt
    assert "case_target_check" not in receipt
    assert (tmp_path / "draft" / "spec.json").is_file()


def test_profile_hooks_resolve_only_when_complete():
    with pytest.raises(ValueError, match="exposes no complete CASE_PREPARATION_HOOKS"):
        cli.load_case_preparation_hooks("tabular")
    resolved = cli.load_case_preparation_hooks("hylogger")
    assert callable(resolved.preflight)
    assert callable(resolved.check_target)
    assert callable(resolved.confirm_unchanged)


def test_request_creates_source_pinned_draft_without_sending_rows(tmp_path):
    model = FixtureModel()
    assert _prepare(tmp_path, model) == 0
    draft = tmp_path / "draft"
    spec = json.loads((draft / "spec.json").read_text())
    assert spec["target_rows"] == 20
    assert spec["purpose"] == "Create 20 synthetic rows for a demo."
    assert json.loads((draft / "plan.json").read_text())["n_rows"] == 20
    receipt = json.loads((draft / "request.json").read_text())
    assert receipt["status"] == "draft-needs-human-review"
    assert receipt["source_rows_sent"] is False
    assert receipt["source_sha256"] == spec["source"]["sha256"]
    assert receipt["observed_model"] == "fixture-only"
    assert "private-canary" not in model.prompt
    assert "source.csv" not in model.prompt
    assert "An intended use such as a demo is context" in model.prompt
    assert "only flag such a guarantee" in model.prompt
    assert "bound locally by the tool" in model.prompt
    assert not list(draft.rglob("data.csv"))


@pytest.mark.parametrize(
    "changes",
    [
        {"unsupported": ["new unimplemented fields"]},
        {"n_rows": True},
        {"n_rows": 200001},
        {"strategy": "invented"},
        {"outputs": ["imaging"]},
        {"new_field": "execute this"},
        {"seed": -1},
        {"unsupported": "none"},
    ],
)
def test_unsupported_or_invalid_interpretation_creates_no_runnable_files(
    tmp_path, changes
):
    assert _prepare(tmp_path, FixtureModel(**changes)) == 1
    assert not (tmp_path / "draft" / "spec.json").exists()
    assert not (tmp_path / "draft" / "plan.json").exists()
    receipt = json.loads((tmp_path / "draft" / "request.json").read_text())
    assert receipt["status"] in ("error", "unsupported")


def test_request_local_policy_denies_default_hosted_planner(tmp_path):
    model = FixtureModel()
    code = cli.main(
        [
            "prepare",
            "not-read.csv",
            "--profile",
            "tabular",
            "--request",
            "20 rows",
            "--max-attempts",
            "1",
            "--out",
            str(tmp_path / "draft"),
        ],
        model=model,
    )
    assert code == 1
    assert model.calls == 0
    assert "non-local adapter" in (tmp_path / "draft" / "request.json").read_text()


def test_existing_draft_is_not_overwritten_or_contacted(tmp_path):
    model = FixtureModel()
    assert _prepare(tmp_path, model) == 0
    before = (tmp_path / "draft" / "request.json").read_bytes()
    assert _prepare(tmp_path, model) == 1
    assert model.calls == 1
    assert (tmp_path / "draft" / "request.json").read_bytes() == before


def test_prepared_request_runs_offline_with_exact_rows(tmp_path, monkeypatch):
    import io
    import zipfile

    from tests.engine.test_plan_replay import (
        RefusingModel,
        _run_dirs,
        _write_tabular_source,
    )

    source = _write_tabular_source(tmp_path)
    model = FixtureModel()
    draft = tmp_path / "draft"
    assert (
        cli.main(
            [
                "prepare",
                str(source),
                "--profile",
                "tabular",
                "--request",
                "Create 20 rows.",
                "--max-attempts",
                "1",
                "--out",
                str(draft),
                "--role-adapter",
                "planner=glimmer_local",
            ],
            model=model,
        )
        == 0
    )
    no_model = RefusingModel()
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nAutomated fixture.\n"))
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "run",
                str(source),
                "--profile",
                "tabular",
                "--max-attempts",
                "1",
                "--spec",
                str(draft / "spec.json"),
                "--plan",
                str(draft / "plan.json"),
                "--model-policy",
                "offline",
                "--out",
                str(out),
            ],
            model=no_model,
        )
        == 0
    )
    assert no_model.calls == 0
    run = _run_dirs(out)[0]
    with zipfile.ZipFile(run / "release.zip") as release:
        assert len(release.read("data.csv").decode().splitlines()) == 21
        report = release.read("evidence-report.md").decode()
    assert "Exact row target: 20" in report
    assert "Create 20 rows." in report
    assert "saved-plan replay" in report

    source.write_text(source.read_text() + "21,99,changed\n")
    assert (
        cli.main(
            [
                "run",
                str(source),
                "--profile",
                "tabular",
                "--max-attempts",
                "1",
                "--spec",
                str(draft / "spec.json"),
                "--plan",
                str(draft / "plan.json"),
                "--model-policy",
                "offline",
                "--out",
                str(tmp_path / "changed"),
            ],
            model=no_model,
        )
        == 1
    )
    assert not list((tmp_path / "changed").rglob("release.zip"))
    assert no_model.calls == 0


def test_missing_source_is_refused_before_model_contact(tmp_path):
    model = FixtureModel()
    assert (
        cli.main(
            [
                "prepare",
                str(tmp_path / "missing.csv"),
                "--profile",
                "tabular",
                "--request",
                "Create 20 rows.",
                "--max-attempts",
                "1",
                "--out",
                str(tmp_path / "draft"),
                "--role-adapter",
                "planner=glimmer_local",
            ],
            model=model,
        )
        == 1
    )
    assert model.calls == 0
    assert not (tmp_path / "draft" / "plan.json").exists()


def test_capabilities_are_saved_and_bound_to_the_interpretation(tmp_path):
    model = FixtureModel()
    assert _prepare(tmp_path, model) == 0
    receipt = json.loads((tmp_path / "draft" / "request.json").read_text())
    context = receipt["capability_context"]
    assert receipt["capability_context_sha256"] == sha256_canonical(context)
    assert receipt["prompt_sha256"] == sha256_bytes(model.prompt.encode())
    assert receipt["manifest_digest"] == context["manifest_digest"]
    assert (
        receipt["manifest_digest"]
        == load_manifest(ROOT / "profiles" / "tabular" / "profile.json").digest
    )
    assert context["strategies"] == ["fitted_gaussian_disclosed"]
    assert {c["id"] for c in context["checks"]["required"]} == {
        "has_rows",
        "schema_columns",
        "row_count",
        "mean_value",
    }
    assert "fitted_markov_joint" not in model.prompt
    assert str(tmp_path) not in model.prompt
    assert "private-canary" not in json.dumps(context)
    assert "source.csv" not in json.dumps(context)


def test_scalar_route_uses_real_checks_and_excludes_image_claims():
    manifest = validate_manifest(_payload("hylogger"))
    context = build_capability_context(manifest)
    checks = context["checks"]
    assert {c["id"] for c in checks["required"]} == {
        c["id"]
        for c in manifest.checks
        if c.get("required", True) and not c.get("applicable_when")
    }
    assert [c["id"] for c in checks["optional"]] == ["swir_answer_rate"]
    assert checks["omitted_conditional_count"] == 18
    assert context["route"]["outputs"] == ["scalar"]
    assert context["profile_brief"]["output_columns"][0].startswith("depth_m:")
    assert {m["name"] for m in context["release_members"]} == {
        "data.csv",
        "plan.json",
        "validation-report.json",
        "seed.txt",
        "provenance.md",
        "run.jsonl",
        "evidence-report.md",
    }
    assert all("when" not in m for m in context["release_members"])
    assert "synthetic.png" not in json.dumps(context)


def test_context_tracks_declaration_changes_instead_of_hardcoded_profile_names():
    payload = _payload()
    original = build_capability_context(validate_manifest(payload))
    payload["checks"][0]["required"] = False
    payload["release"]["members"] = [{"name": "custom-report.json", "required": True}]
    payload["generation"]["request_context"]["description"] = "Changed contract."
    revised = build_capability_context(validate_manifest(payload))
    assert {c["id"] for c in revised["checks"]["optional"]} == {"has_rows"}
    assert "has_rows" not in {c["id"] for c in revised["checks"]["required"]}
    assert revised["release_members"] == [
        {"name": "custom-report.json", "required": True}
    ]
    assert revised["profile_brief"]["description"] == "Changed contract."
    assert sha256_canonical(revised) != sha256_canonical(original)


def test_old_manifest_without_brief_still_drafts(tmp_path, monkeypatch):
    payload = _payload()
    del payload["generation"]["request_context"]
    manifest = validate_manifest(payload)
    monkeypatch.setattr(cli, "load_manifest", lambda unused: manifest)
    model = FixtureModel()
    assert _prepare(tmp_path, model) == 0
    receipt = json.loads((tmp_path / "draft" / "request.json").read_text())
    assert receipt["capability_context"]["profile_brief"] is None
    assert "no capability brief" in model.prompt
    assert receipt["status"] == "draft-needs-human-review"


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "missing", "blank", "wrong_type", "too_long", "duplicate", "control"],
)
def test_bad_brief_refused_before_output_or_model_contact(
    tmp_path, monkeypatch, mutation
):
    payload = _payload()
    brief = payload["generation"]["request_context"]
    if mutation == "unknown":
        brief["allow_execute"] = True
    elif mutation == "missing":
        del brief["limitations"]
    elif mutation == "blank":
        brief["description"] = "  "
    elif mutation == "wrong_type":
        brief["output_columns"] = "column"
    elif mutation == "too_long":
        brief["supported_requests"] = ["x" * 601]
    elif mutation == "duplicate":
        brief["limitations"] = ["No promise.", "No promise."]
    elif mutation == "control":
        brief["description"] = "first\nsecond"
    path = tmp_path / "invalid-profile.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(cli.profiles_package, "manifest_path", lambda unused: path)
    model = FixtureModel()
    assert _prepare(tmp_path, model) == 1
    assert model.calls == 0
    assert not (tmp_path / "draft").exists()


def test_context_overflow_and_unsupported_modality_fail_before_contact(
    tmp_path, monkeypatch
):
    import engine.cli.request_context as context_module

    model = FixtureModel()
    monkeypatch.setattr(context_module, "MAX_CONTEXT_CHARS", 1)
    assert _prepare(tmp_path, model) == 1
    assert model.calls == 0
    assert not (tmp_path / "draft").exists()
    monkeypatch.setattr(context_module, "MAX_CONTEXT_CHARS", 24000)
    payload = _payload()
    payload["generation"]["outputs"] = ["imaging"]
    monkeypatch.setattr(cli, "load_manifest", lambda unused: validate_manifest(payload))
    assert _prepare(tmp_path, model) == 1
    assert model.calls == 0


def test_required_defaults_and_frozen_report_have_honest_context():
    payload = _payload()
    del payload["checks"][0]["required"]
    report = next(
        m for m in payload["release"]["members"] if m["name"] == "evidence-report.md"
    )
    report.pop("source")
    context = build_capability_context(validate_manifest(payload))
    assert "has_rows" in {c["id"] for c in context["checks"]["required"]}
    report_context = next(
        m for m in context["release_members"] if m["name"] == "evidence-report.md"
    )
    assert "origin" not in report_context


def test_brief_is_contract_metadata_not_a_quality_verdict():
    payload = _payload()
    original = validate_manifest(payload)
    payload["generation"]["request_context"]["description"] = "Supported example."
    changed = validate_manifest(payload)
    assert original.digest != changed.digest
    assert original.graph == changed.graph
    assert original.checks == changed.checks
    assert original.release == changed.release
    with pytest.raises(ManifestError):
        payload["generation"]["request_context"] = None
        validate_manifest(payload)


def test_request_prompt_explains_strategy_selection_over_default():
    from engine.cli.request import INTERPRETATION_RULES

    assert "default strategy is a fallback" in INTERPRETATION_RULES
    assert "user to know its internal name" in INTERPRETATION_RULES
