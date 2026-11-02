"""Unit tests for scripts/evaluate.py (fake model, fixture CSVs, no network)."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import evaluate as evaluate_module  # noqa: E402
from evaluate import (  # noqa: E402
    evaluate_hole,
    group_results,
    parse_baseline_csv,
    render_results_table,
    run_baseline,
    run_pipeline,
    score_samples,
    summarize,
    usage_dict_tokens,
)

from models.base import Completion, ModelError, Usage  # noqa: E402
from profiles.hylogger.analysis.calibration import load_thresholds  # noqa: E402
from profiles.hylogger.analysis.statistics import Profile  # noqa: E402
from profiles.hylogger.intake.reader import Sample  # noqa: E402
from profiles.hylogger.validation.checks import CheckResult  # noqa: E402
from profiles.hylogger.validation.dataset import to_csv_bytes  # noqa: E402


@pytest.fixture()
def thresholds():
    return load_thresholds(REPO_ROOT / "profiles" / "hylogger" / "thresholds.json")


@pytest.fixture()
def source_samples():
    samples = []
    depth = 100.0
    for i in range(40):
        samples.append(
            Sample(
                depth_m=depth,
                valid=(i % 4 != 0),
                swir_group="QUARTZ" if i % 2 == 0 else None,
                tir_group="CHLORITE" if i % 3 else None,
            )
        )
        depth += 0.005 if i % 5 else 0.0
    return samples


@pytest.fixture()
def profile(source_samples):
    return Profile.from_samples(source_samples, "fixture", "0" * 64)


class FakeModel:
    """Fake StructuredModel returning canned CSV text (no network)."""

    def __init__(self, csv_text: str):
        self.csv_text = csv_text
        self.model = "fake-model"
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert "csv" in schema["properties"]
        return Completion(
            data={"csv": self.csv_text},
            usage=Usage(
                model="fake-model",
                input_tokens=10,
                output_tokens=20,
                reasoning_tokens=5,
                calls=1,
            ),
        )


class RefusingModel:
    """Fake model that always refuses (no network)."""

    model = "fake-refuser"

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        raise ModelError("refused")


def _good_csv(n: int = 6) -> str:
    samples = [
        Sample(
            depth_m=100.0 + 0.005 * i,
            valid=True,
            swir_group="QUARTZ",
            tir_group="CHLORITE",
        )
        for i in range(n)
    ]
    return to_csv_bytes(samples).decode("utf-8")


# --- token counting ---


def test_usage_dict_tokens_accepts_objects_and_stored_dicts():
    usage = Usage(
        model="m", input_tokens=10, output_tokens=20, reasoning_tokens=5, calls=1
    )
    assert usage_dict_tokens(usage) == 35
    assert (
        usage_dict_tokens(
            {"input_tokens": 10, "output_tokens": 20, "reasoning_tokens": 5, "calls": 1}
        )
        == 35
    )
    assert usage_dict_tokens({}) == 0
    assert usage_dict_tokens({"input_tokens": "x", "output_tokens": None}) == 0


# --- scoring aggregation ---


def test_group_results_pass_and_skip_counts_as_pass():
    results = [
        CheckResult("has_rows", "pass", "6 rows"),
        CheckResult("spacing_median", "skip", "no data"),
        CheckResult("swir_fidelity", "pass", "close"),
        CheckResult("swir_vocabulary", "pass", "in vocab"),
        CheckResult("tir_vocabulary", "pass", "in vocab"),
    ]
    kinds = {"spacing_median": "fidelity", "swir_fidelity": "fidelity"}
    groups = group_results(results, kinds)
    assert groups["constraints"]["pass"] is True
    assert groups["fidelity"]["pass"] is True
    assert groups["fidelity"]["n_skipped"] == 1
    assert groups["leakage"]["pass"] is True
    assert groups["leakage"]["n_checks"] == 2


def test_group_results_fail_in_group_fails_group_only():
    results = [
        CheckResult("has_rows", "pass", "6 rows"),
        CheckResult("swir_fidelity", "fail", "TVD too big"),
        CheckResult("swir_vocabulary", "pass", "in vocab"),
        CheckResult("tir_vocabulary", "fail", "unknown groups: ZZ"),
    ]
    kinds = {"swir_fidelity": "fidelity"}
    groups = group_results(results, kinds)
    # tir_vocabulary fails: it counts in constraints (its manifest kind)
    # and in leakage (the harness operationalisation).
    assert groups["constraints"]["pass"] is False
    assert groups["constraints"]["failed"] == ["tir_vocabulary"]
    assert groups["fidelity"]["pass"] is False
    assert groups["fidelity"]["failed"] == ["swir_fidelity"]
    assert groups["leakage"]["pass"] is False
    assert groups["leakage"]["failed"] == ["tir_vocabulary"]


def test_group_results_accepts_plain_json_mappings():
    stored = [
        {"name": "has_rows", "status": "pass"},
        {"name": "swir_fidelity", "status": "skip"},
        {"name": "swir_vocabulary", "status": "pass"},
        {"name": "tir_vocabulary", "status": "pass"},
    ]
    groups = group_results(stored, {"swir_fidelity": "fidelity"})
    assert groups["constraints"]["pass"] is True
    assert groups["fidelity"]["pass"] is True
    assert groups["leakage"]["pass"] is True
    # A group with zero executed checks fails coverage (R09-harness):
    # the old expectation (empty leakage passing) encoded the audit bug.
    missing = group_results(
        [{"name": "has_rows", "status": "pass"}], {"has_rows": "constraint"}
    )
    assert missing["leakage"]["pass"] is False
    assert missing["leakage"]["coverage"] == "empty"


def test_score_samples_mirrors_validate_step(thresholds, source_samples):
    samples = [
        Sample(
            depth_m=100.0 + 0.005 * i,
            valid=True,
            swir_group="QUARTZ",
            tir_group="CHLORITE",
        )
        for i in range(8)
    ]
    report = score_samples(samples, thresholds, source_samples, artifact_name="fixture")
    assert report.results[0].name == "csv_schema"
    assert report.results[0].status == "pass"
    assert report.verdict in ("pass", "fail")
    assert report.artifact_name == "fixture"


# --- baseline CSV parsing ---


def test_parse_good_csv_returns_all_rows():
    text = _good_csv(6)
    samples, parsed, error = parse_baseline_csv(text, 6)
    assert error is None
    assert parsed == 6
    assert samples is not None and len(samples) == 6


def test_parse_truncated_csv_reports_zero_rows():
    # A cut-off final row leaves a corrupt cell (csv.DictReader pads a
    # merely short row with None, so truncation shows up as a bad cell).
    text = "depth_m,valid,swir_group,tir_group\n100.0,tr\n"
    samples, parsed, error = parse_baseline_csv(text, 10)
    assert samples is None
    assert parsed == 0
    assert error is not None
    assert "ValueError" in error


def test_parse_garbage_csv_reports_error():
    samples, parsed, error = parse_baseline_csv("hello world\n", 10)
    assert samples is None
    assert parsed == 0
    assert error is not None


def test_baseline_run_scores_and_counts_tokens(thresholds, source_samples, profile):
    record = run_baseline(
        FakeModel(_good_csv(6)), profile, source_samples, thresholds, 6
    )
    assert record["requested_rows"] == 6
    assert record["parsed_rows"] == 6
    assert record["parse_error"] is None
    assert record["usage_tokens"] == 35  # 10 + 20 + 5
    assert record["wall_s"] >= 0.0
    assert record["reproducible"] is False
    assert record["report"] is not None
    assert "groups" not in record  # grouping happens in evaluate_hole


def test_baseline_run_refusal_records_error(thresholds, source_samples, profile):
    record = run_baseline(RefusingModel(), profile, source_samples, thresholds, 1000)
    assert record["requested_rows"] == 1000
    assert record["parsed_rows"] == 0
    assert "ModelError" in (record["parse_error"] or "")
    assert record["verdict"] == "error"
    assert record["report"] is None


def test_baseline_run_garbage_csv_records_parse_failure(
    thresholds, source_samples, profile
):
    record = run_baseline(
        FakeModel("not a csv\n"), profile, source_samples, thresholds, 1000
    )
    assert record["requested_rows"] == 1000
    assert record["parsed_rows"] == 0
    assert record["parse_error"] is not None
    assert record["verdict"] == "error"


# --- summary + table rendering ---


def _hole_record(hole, verdict, groups_pass, extra=None):
    arm = {
        "verdict": verdict,
        "groups": {
            "constraints": {"pass": groups_pass},
            "fidelity": {"pass": groups_pass},
            "leakage": {"pass": groups_pass},
        },
        "reproducible": {"match": groups_pass},
        "attempts_used": 1,
        "usage_tokens": 100,
        "wall_s": 2.5,
    }
    baseline = dict(arm)
    baseline["reproducible"] = False
    baseline.pop("attempts_used", None)
    record = {"hole": hole, "pipeline": dict(arm), "baseline": baseline}
    if extra:
        record.update(extra)
    return record


def test_summarize_aggregates_both_arms(tmp_path):
    records = [_hole_record("H1", "pass", True), _hole_record("H2", "fail", False)]
    summary = summarize(records)
    pipe, base = summary["arms"]["pipeline"], summary["arms"]["baseline"]
    assert summary["holes"] == ["H1", "H2"]
    assert (pipe["verdict_pass"], pipe["holes"]) == (1, 2)
    assert (pipe["constraints_pass"], pipe["fidelity_pass"]) == (1, 1)
    assert (pipe["leakage_pass"], pipe["reproducible"]) == (1, 1)
    assert pipe["attempts_total"] == 2
    assert pipe["usage_tokens_total"] == 200
    assert base["verdict_pass"] == 1
    assert base["reproducible"] == 0
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    loaded = json.loads(summary_path.read_text(encoding="utf-8"))
    assert loaded["arms"]["pipeline"]["verdict_pass"] == 1


def test_evaluate_hole_writes_json_with_skipped_pipeline(tmp_path, monkeypatch):
    """evaluate_hole creates a fresh out dir and writes the per-hole JSON."""
    import shutil

    staged = tmp_path / "staged"
    sources = staged / "data" / "sources"
    sources.mkdir(parents=True)
    with (sources / "MINI-scalars.csv").open("w", encoding="utf-8") as handle:
        handle.write("StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST\n")
        for i in range(40):
            handle.write(f"{100.0 + 0.005 * i},1,QUARTZ,CHLORITE\n")
    prof_dir = staged / "profiles" / "hylogger"
    prof_dir.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "profiles" / "hylogger" / "thresholds.json",
        prof_dir / "thresholds.json",
    )
    shutil.copy(
        REPO_ROOT / "profiles" / "hylogger" / "profile.json",
        prof_dir / "profile.json",
    )
    monkeypatch.setattr(evaluate_module, "REPO_ROOT", staged)
    out_dir = tmp_path / "fresh" / "nested"
    record = evaluate_hole(
        "MINI",
        max_attempts=3,
        baseline_rows=40,
        out_dir=out_dir,
        model=FakeModel(_good_csv(40)),
        run_pipeline_arm=False,
    )
    assert record["pipeline"] == {"skipped": True}
    assert record["baseline"]["parsed_rows"] == 40
    stored = json.loads((out_dir / "MINI.json").read_text(encoding="utf-8"))
    assert stored["hole"] == "MINI"
    assert stored["baseline"]["requested_rows"] == 40


# --- R09-harness integrity (RP-08): stale, coverage, bytes, recompute ---


class _Proc:
    """Stand-in for subprocess.CompletedProcess (returncode/stdout/stderr)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _write_stale_run(hole_out: Path) -> Path:
    """Plant an old product run dir: passing report over old bytes."""
    stale = hole_out / "runs" / "stale000000000000000000000000000001"
    stale.mkdir(parents=True)
    (stale / "run.jsonl").write_text('{"stage": "done"}\n', encoding="utf-8")
    (stale / "validation-report.json").write_text(
        json.dumps(
            {
                "verdict": "pass",
                "artifact_sha256": "0" * 64,
                "results": [{"name": "has_rows", "status": "pass"}],
            }
        ),
        encoding="utf-8",
    )
    (stale / "data.csv").write_text("depth_m,valid\n100.0,true\n", encoding="utf-8")
    return stale


def _failing_run(monkeypatch, returncode=2, stderr="error: boom"):
    def _run(*args, **kwargs):
        return _Proc(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(evaluate_module.subprocess, "run", _run)


def test_pipeline_failure_over_stale_artifacts_is_unscorable(tmp_path, monkeypatch):
    """R09-harness: CLI exit!=0 with no fresh run dir is NEVER scored.

    Regression for the audit violation: a failing CLI run over an old
    fixture directory was scored as a passing reproducible hole.
    """
    hole_out = tmp_path / "hole"
    hole_out.mkdir()
    stale = _write_stale_run(hole_out)
    _failing_run(monkeypatch, returncode=2)
    source = tmp_path / "hole-src.csv"
    source.write_text("depth_m,valid\n100.0,true\n", encoding="utf-8")
    record = run_pipeline("HOLE", source, 3, hole_out)
    assert record.get("unscorable") is True
    assert record.get("exit_code") == 2
    assert record.get("verdict") != "pass"
    assert "groups" not in record or not any(
        g.get("pass") for g in record["groups"].values()
    )
    assert stale.is_dir()  # stale bytes untouched, just never scored


def test_pipeline_failure_with_no_run_dir_is_unscorable(tmp_path, monkeypatch):
    hole_out = tmp_path / "hole"
    hole_out.mkdir()
    _failing_run(monkeypatch, returncode=1, stderr="error: timeout")
    source = tmp_path / "hole-src.csv"
    source.write_text("depth_m,valid\n100.0,true\n", encoding="utf-8")
    record = run_pipeline("HOLE", source, 3, hole_out)
    assert record.get("unscorable") is True
    assert record.get("verdict", "error") != "pass"


def test_empty_check_group_fails_coverage():
    """R09-harness: a group with zero executed checks cannot pass."""
    groups = group_results(
        [CheckResult("has_rows", "pass", "1 row")], {"has_rows": "constraint"}
    )
    assert groups["constraints"]["pass"] is True
    assert groups["fidelity"]["pass"] is False
    assert groups["fidelity"]["n_checks"] == 0
    assert groups["leakage"]["pass"] is False
    assert groups["leakage"]["n_checks"] == 0


def test_baseline_run_saves_raw_bytes_and_invocation(
    thresholds, source_samples, profile, tmp_path
):
    """R09-harness: every baseline invocation saves its raw output bytes,
    prompt digest, model identity, invocation id, usage, and error state."""
    save_dir = tmp_path / "baseline"
    record = run_baseline(
        FakeModel(_good_csv(6)),
        profile,
        source_samples,
        thresholds,
        6,
        save_dir=save_dir,
    )
    assert record.get("unscorable") is not True
    assert (save_dir / "baseline.csv").is_file()
    assert (save_dir / "invocation.json").is_file()
    invocation = json.loads((save_dir / "invocation.json").read_text(encoding="utf-8"))
    assert invocation["model"] == "fake-model"
    assert invocation["invocation_id"] == record["invocation_id"]
    assert invocation["prompt_sha256"] == record["prompt_sha256"]
    assert invocation["output_sha256"] == record["output_bytes_sha256"]
    assert (save_dir / "baseline.csv").read_bytes().decode("utf-8") == (_good_csv(6))


def test_baseline_failure_is_unscorable_and_never_reuses_previous(
    thresholds, source_samples, profile, tmp_path
):
    save_dir = tmp_path / "baseline"
    first = run_baseline(
        FakeModel(_good_csv(6)),
        profile,
        source_samples,
        thresholds,
        6,
        save_dir=save_dir,
    )
    assert first["report"] is not None
    second = run_baseline(
        RefusingModel(),
        profile,
        source_samples,
        thresholds,
        6,
        save_dir=save_dir,
    )
    assert second.get("unscorable") is True
    assert second["report"] is None
    assert second["verdict"] == "error"
    assert "invocation_id" in second
    assert second["invocation_id"] != first["invocation_id"]


def test_summarize_counts_unscorable_in_denominator():
    records = [_hole_record("H1", "pass", True)]
    records.append(
        {
            "hole": "H2",
            "pipeline": {"unscorable": True, "reason": "exit 2, no fresh run"},
            "baseline": {"unscorable": True, "reason": "refused"},
        }
    )
    summary = summarize(records)
    pipe = summary["arms"]["pipeline"]
    assert pipe["holes"] == 1
    assert pipe["holes_total"] == 2
    assert pipe["unscorable"] == 1
    assert pipe["verdict_pass"] == 1


def test_recompute_from_disk_matches_summary(tmp_path):
    from evaluate import recompute_summary  # noqa: E402

    out_dir = tmp_path / "eval"
    out_dir.mkdir()
    records = [_hole_record("H1", "pass", True)]
    summary = summarize(records)
    (out_dir / "H1.json").write_text(json.dumps(records[0]), encoding="utf-8")
    summary["out"] = "eval"
    (out_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    recomputed = recompute_summary(out_dir)
    assert recomputed["arms"]["pipeline"]["verdict_pass"] == 1


def test_recompute_detects_tampered_summary(tmp_path):
    from evaluate import summary_mismatch  # noqa: E402

    out_dir = tmp_path / "eval"
    out_dir.mkdir()
    records = [_hole_record("H1", "fail", False)]
    summary = summarize(records)
    (out_dir / "H1.json").write_text(json.dumps(records[0]), encoding="utf-8")
    summary["arms"]["pipeline"]["verdict_pass"] = 99
    (out_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    assert summary_mismatch(out_dir)


def test_render_results_table_from_summary_json(tmp_path):
    records = [_hole_record("H1", "pass", True)]
    summary = summarize(records)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    table = render_results_table(json.loads(summary_path.read_text(encoding="utf-8")))
    assert "H1" in table
    assert "Constraints pass" in table
    assert "Fidelity pass" in table
    assert "Leakage pass" in table
    assert "Reproducible" in table
    assert "Held-out holes pass" in table
    assert "Attempts used (total)" in table
