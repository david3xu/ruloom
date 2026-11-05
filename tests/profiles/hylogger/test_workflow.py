"""Tests for profiles.hylogger.validation.workflow."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from models.base import Completion, Usage
from profiles.hylogger.validation.cases import (
    REVIEWED_CASES_CATALOGUE_SHA256,
    get_evidence_pack,
)
from profiles.hylogger.validation.workflow import (
    CASE_CHECK_IDS,
    CASE_MEASUREMENT_KEYS,
    PROHIBITED_PROSE_PATTERNS,
    _canonical_bytes,
    assess_case_source,
    build_case_members,
    case_check_results,
    evaluate_case_context,
    prepare_case_context,
    prohibited_prose_contract,
    repair_case_plan,
    research_case,
    review_case_candidate,
    verify_case_members,
)


def _make_source_csv(
    tmp_path: Path,
    n_rows: int = 120,
    depth_start: float = 0.0,
    spacing: float = 0.0075,
    all_valid: bool = False,
    all_invalid: bool = False,
) -> Path:
    """Generate a controlled test source CSV."""
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
            if all_valid:
                is_valid = True
            elif all_invalid:
                is_valid = False
            else:
                is_valid = (i % 4) != 0  # 75% valid

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
    return path


def _make_candidate_csv(
    tmp_path: Path,
    n_rows: int = 200,
    depth_start: float = 0.0,
    spacing: float = 0.0075,
    valid_fraction: float = 0.75,
    unordered: bool = False,
) -> bytes:
    """Generate candidate CSV bytes matching teaching contract."""
    output = []
    output.append("depth_m,valid,swir_group,tir_group\n")
    depth = depth_start
    for i in range(n_rows):
        is_val = (i % 4) != 0  # 75% valid, same pattern as source
        sw = "CHLORITE" if (is_val and (i % 5 != 0)) else ""
        ti = "AMPHIBOLE" if (is_val and (i % 6 != 0)) else ""
        d_val = (depth - 0.02) if (unordered and i == 10) else depth
        flag = "true" if is_val else "false"
        output.append(f"{d_val:.4f},{flag},{sw},{ti}\n")
        depth += spacing
    return "".join(output).encode("utf-8")


class MockStructuredModel:
    """Mock structured model for test validation."""

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.recorded_prompts: list[str] = []
        self.recorded_schemas: list[Mapping[str, Any]] = []

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Any:
        self.recorded_prompts.append(prompt)
        self.recorded_schemas.append(schema)
        if callable(self.responder):
            return self.responder(prompt, schema)
        return self.responder


# =====================================================================
# prepare_case_context Tests
# =====================================================================


def test_prepare_case_context_positive(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_bytes = src.read_bytes()
    src_sha = hashlib.sha256(src_bytes).hexdigest()

    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }

    ctx = prepare_case_context(src, spec, licence_meta="disclosed: CC-BY-4.0")

    assert isinstance(ctx, dict)
    assert "binding" in ctx
    assert "contract" in ctx
    assert "reference" in ctx
    assert "admitted_evidence" in ctx
    assert "source_fit_receipt" in ctx

    binding = ctx["binding"]
    assert binding["case_id"] == "hylogger_scalar_teaching_v1"
    assert binding["case_version"] == 1
    assert binding["source_sha256"] == src_sha
    assert binding["catalogue_sha256"] == REVIEWED_CASES_CATALOGUE_SHA256
    assert len(binding["thresholds_sha256"]) == 64
    assert len(binding["checker_sha256"]) == 64
    assert len(binding["contract_sha256"]) == 64
    assert len(binding["reference_sha256"]) == 64

    contract = ctx["contract"]
    assert contract["target_rows"] == 200
    assert contract["source_sha256"] == src_sha


def test_prepare_case_context_refuses_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_source_csv(tmp_path)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    monkeypatch.setenv("RULOOM_THRESHOLDS", "/tmp/fake-thresholds.json")

    with pytest.raises(
        RuntimeError, match="RULOOM_THRESHOLDS environment override is forbidden"
    ):
        prepare_case_context(src, spec)


def test_prepare_case_context_spec_validation_negatives(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()

    # Missing case mapping
    with pytest.raises(ValueError, match="spec must contain a 'case' mapping"):
        prepare_case_context(
            src,
            {"target_rows": 200, "source": {"sha256": src_sha}, "outputs": ["scalar"]},
        )

    # Case version float/bool alias
    with pytest.raises(ValueError, match="positive integer"):
        prepare_case_context(
            src,
            {
                "case": {"id": "hylogger_scalar_teaching_v1", "version": True},
                "target_rows": 200,
                "source": {"sha256": src_sha},
                "outputs": ["scalar"],
            },
        )

    # Infeasible target rows (< min_satisfiable_rows 25)
    with pytest.raises(
        ValueError, match="below the admitted case satisfiability floor"
    ):
        prepare_case_context(
            src,
            {
                "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
                "target_rows": 10,
                "source": {"sha256": src_sha},
                "outputs": ["scalar"],
            },
        )

    # Source hash mismatch
    with pytest.raises(ValueError, match="source sha256 mismatch"):
        prepare_case_context(
            src,
            {
                "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
                "target_rows": 200,
                "source": {"sha256": "0" * 64},
                "outputs": ["scalar"],
            },
        )

    # Non-scalar outputs
    with pytest.raises(ValueError, match=r"spec\['outputs'\] must be \['scalar'\]"):
        prepare_case_context(
            src,
            {
                "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
                "target_rows": 200,
                "source": {"sha256": src_sha},
                "outputs": ["scalar", "image"],
            },
        )


def test_prepare_case_context_unsuitable_source(tmp_path: Path) -> None:
    # All invalid rows: missing both inclusion states
    src = _make_source_csv(tmp_path, n_rows=50, all_invalid=True)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 50,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    with pytest.raises(ValueError, match="source is not suitable"):
        prepare_case_context(src, spec)


# =====================================================================
# evaluate_case_context Tests
# =====================================================================


def test_evaluate_case_context_positive(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec, licence_meta="disclosed: CC-BY-4.0")

    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200, valid_fraction=0.75)
    report = evaluate_case_context(cand_bytes, ctx)

    assert report["passed"] is True
    assert not report["failures"]
    assert len(report["measurements"]) == len(CASE_MEASUREMENT_KEYS)


def test_evaluate_case_context_tamper_detection(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)

    # Tampered contract digest in binding
    ctx_tampered = json.loads(json.dumps(ctx))
    ctx_tampered["binding"]["contract_sha256"] = "a" * 64
    with pytest.raises(
        ValueError, match="context contract does not match binding contract_sha256"
    ):
        evaluate_case_context(cand_bytes, ctx_tampered)

    # Tampered reference digest in binding
    ctx_tampered = json.loads(json.dumps(ctx))
    ctx_tampered["binding"]["reference_sha256"] = "b" * 64
    with pytest.raises(
        ValueError, match="context reference does not match binding reference_sha256"
    ):
        evaluate_case_context(cand_bytes, ctx_tampered)


def test_evaluate_case_context_invalid_utf8(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    bad_bytes = b"\xff\xfe\x00\x00corrupt"
    report = evaluate_case_context(bad_bytes, ctx)
    assert report["passed"] is False
    assert "invalid_utf8_encoding" in report["failures"]


# =====================================================================
# case_check_results Tests
# =====================================================================


def test_case_check_results_all_13_pass(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200, valid_fraction=0.75)
    report = evaluate_case_context(cand_bytes, ctx)

    results = case_check_results(report)
    assert len(results) == 13
    emitted_ids = tuple(r["check_id"] for r in results)
    assert emitted_ids == CASE_CHECK_IDS

    for r in results:
        assert r["status"] == "pass"
        assert r["kind"] == "constraint"
        assert r["threshold_source"] == "case_contract"
        assert r["band_position"] is None
        assert isinstance(r["detail"], str)


def test_case_check_results_with_failure(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    # Deliberately unordered depth to trigger ordered_depth failure
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200, unordered=True)
    report = evaluate_case_context(cand_bytes, ctx)

    results = case_check_results(report)
    assert len(results) == 13

    by_id = {r["check_id"]: r for r in results}
    assert by_id["case.ordered_depth"]["status"] == "fail"
    # Other checks should pass
    assert by_id["case.row_count"]["status"] == "pass"


def test_case_check_results_early_return_emits_all_13_fail() -> None:
    # Simulating an early return where measurements dictionary is empty
    early_report = {
        "passed": False,
        "failures": ["invalid_utf8_encoding"],
        "measurements": {},
        "candidate_sha256": "0" * 64,
        "limitations": ["Case-level teaching-table utility only."],
    }

    results = case_check_results(early_report)
    assert len(results) == 13
    for r in results:
        assert r["status"] == "fail"
        assert (
            "overall evaluation failed" in r["detail"]
            or "missing from evaluator output" in r["detail"]
        )


# =====================================================================
# build_case_members Tests
# =====================================================================


def test_build_case_members_positive(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)
    spec_digest = "e" * 64

    members = build_case_members(cand_bytes, ctx, report, spec_digest)

    assert set(members.keys()) == {
        "case-binding.json",
        "case-contract.json",
        "case-reference.json",
        "case-utility.json",
        "case-evidence.json",
    }

    # Verify binding payload
    binding_obj = json.loads(members["case-binding.json"].decode("utf-8"))
    assert binding_obj["spec_digest"] == spec_digest
    assert binding_obj["candidate_sha256"] == report["candidate_sha256"]


def test_build_case_members_rejects_mismatched_candidate_report(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)

    different_cand_bytes = cand_bytes + b"\n"
    with pytest.raises(ValueError, match="does not match candidate bytes sha256"):
        build_case_members(different_cand_bytes, ctx, report, "e" * 64)


# =====================================================================
# verify_case_members Tests
# =====================================================================


def test_verify_case_members_positive(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)
    members = build_case_members(cand_bytes, ctx, report, "f" * 64)
    members["data.csv"] = cand_bytes

    # 1. Verification without local source path
    res = verify_case_members(
        members, expected_case={"id": "hylogger_scalar_teaching_v1", "version": 1}
    )
    assert res["verified"] is True
    assert not res["discrepancies"]
    assert res["source_derivation"] == "unproven (aggregate consistency only)"

    # 2. Verification with explicit local source path
    res_src = verify_case_members(members, source_path=src)
    assert res_src["verified"] is True
    assert res_src["source_derivation"].startswith("proven")


def test_verify_case_members_expected_case_mismatch(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)
    members = build_case_members(cand_bytes, ctx, report, "f" * 64)
    members["data.csv"] = cand_bytes

    res = verify_case_members(
        members, expected_case={"id": "hylogger_scalar_teaching_v1", "version": 2}
    )
    assert res["verified"] is False
    assert any("expected case version" in d for d in res["discrepancies"])


def test_verify_case_members_tampered_contract(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)
    members = build_case_members(cand_bytes, ctx, report, "f" * 64)
    members["data.csv"] = cand_bytes

    # Tamper with the contract payload inside members
    contract_data = json.loads(members["case-contract.json"].decode("utf-8"))
    contract_data["checks"]["absolute_valid_fraction_difference_max"] = 0.50
    members["case-contract.json"] = json.dumps(contract_data).encode("utf-8")

    res = verify_case_members(members)
    assert res["verified"] is False
    assert any(
        "canonical hash does not match" in d or "tampering" in d
        for d in res["discrepancies"]
    )


def test_verify_case_members_missing_binding_member(tmp_path: Path) -> None:
    members = {"data.csv": b"depth_m,valid\n1.0,true\n"}
    res = verify_case_members(members)
    assert res["verified"] is False
    assert any(
        "case-binding.json" in d and "missing required release" in d
        for d in res["discrepancies"]
    )


# =====================================================================
# repair_case_plan Tests
# =====================================================================


def test_repair_case_plan_causal_strategy_move(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    context = prepare_case_context(src, spec)
    plan = {
        "strategy": "fitted_markov_joint",
        "seed": 42,
        "n_rows": 200,
    }
    failed_ids = ["case.inclusion_fraction", "other_check"]

    repaired = repair_case_plan(plan, failed_ids, context)
    assert repaired["strategy"] == "fitted_markov_balanced"
    # Seed and n_rows MUST be identical
    assert repaired["seed"] == 42
    assert repaired["n_rows"] == 200


def test_repair_case_plan_no_op_for_balanced_or_unrelated(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    context = prepare_case_context(src, spec)
    plan = {
        "strategy": "fitted_markov_balanced",
        "seed": 42,
        "n_rows": 200,
    }
    # Already balanced
    repaired = repair_case_plan(plan, ["case.inclusion_fraction"], context)
    assert repaired["strategy"] == "fitted_markov_balanced"
    assert repaired["seed"] == 42

    # Unrelated failure with joint
    plan_joint = {
        "strategy": "fitted_markov_joint",
        "seed": 42,
        "n_rows": 200,
    }
    repaired_unrelated = repair_case_plan(plan_joint, ["case.output_columns"], context)
    assert repaired_unrelated["strategy"] == "fitted_markov_joint"
    assert repaired_unrelated["seed"] == 42


# =====================================================================
# Runtime AI Roles Tests
# =====================================================================


def test_research_case_positive(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": src_sha},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    epack = get_evidence_pack()
    nvcl_sha = epack["auscope_nvcl"]["passage_sha256"]

    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "passage_citations": [
            {
                "evidence_id": "auscope_nvcl",
                "passage_sha256": nvcl_sha,
                "relevance": "Directly supports data quality control activities.",
            }
        ],
        "supported_requirements": ["scalar column checking", "depth ordering"],
        "unsupported_requirements": ["live web search"],
        "research_summary": "Auscope NVCL establishes quality checking as a real practice.",
        "limitations": ["Does not establish industry standard tolerances."],
    }
    model = MockStructuredModel(mock_resp)

    advisory = research_case(model, "teaching depth-linked data inspection", ctx)
    assert advisory["advisory_kind"] == "case_research"
    assert advisory["case_id"] == "hylogger_scalar_teaching_v1"
    assert advisory["cited_evidence_ids"] == ["auscope_nvcl"]


def test_research_case_rejects_hallucinated_citation(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    # Hallucinating an unadmitted evidence ID
    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["invented_nature_paper_2026"],
        "passage_citations": [],
        "supported_requirements": [],
        "unsupported_requirements": [],
        "research_summary": "Invented research",
        "limitations": [],
    }
    model = MockStructuredModel(mock_resp)

    with pytest.raises(ValueError, match="unknown or fabricated evidence id"):
        research_case(model, "testing hallucination", ctx)


def test_assess_case_source_positive(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "source_sha256": ctx["binding"]["source_sha256"],
        "cited_evidence_ids": ["auscope_nvcl"],
        "fit_assessment": "Source exhibits 75% valid rows and typical median spacing.",
        "source_limitations": ["Limited to drilled intervals."],
        "teaching_suitability": "Well-suited for teaching flag inspections.",
    }
    model = MockStructuredModel(mock_resp)

    advisory = assess_case_source(model, ctx)
    assert advisory["advisory_kind"] == "source_assessment"
    assert advisory["case_id"] == "hylogger_scalar_teaching_v1"
    # Verify no raw rows were passed in prompt
    assert "StartDepth" not in model.recorded_prompts[0]
    assert "CHLORITE" not in model.recorded_prompts[0]


def test_review_case_candidate_positive(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)

    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "candidate_sha256": report["candidate_sha256"],
        "code_verdict_acknowledged": "pass",
        "operator_recommendation": "recommend_proceed_to_human_gate",
        "evidence_facts": [
            {"measurement": "row_count", "value": 200},
            {"measurement": "inclusion_fraction.candidate", "value": 0.75},
        ],
        "evaluator_opinion": "Candidate adheres strictly to contract constraints.",
        "strengths": ["Even spacing maintained."],
        "concerns": [],
    }
    model = MockStructuredModel(mock_resp)

    advisory = review_case_candidate(model, cand_bytes, ctx, report)
    assert advisory["advisory_kind"] == "candidate_review"
    assert advisory["code_verdict_acknowledged"] == "pass"
    assert advisory["evidence_facts"] == [
        {"measurement": "row_count", "value": 200},
        {"measurement": "inclusion_fraction.candidate", "value": 0.75},
    ]


def test_review_case_candidate_refuses_contradictory_verdict(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    # Generate failing candidate (unordered depth)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200, unordered=True)
    report = evaluate_case_context(cand_bytes, ctx)
    assert report["passed"] is False

    # Reviewer attempts to claim the code verdict passed
    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "candidate_sha256": report["candidate_sha256"],
        "code_verdict_acknowledged": "pass",
        "operator_recommendation": "recommend_proceed_to_human_gate",
        "evidence_facts": [{"measurement": "row_count", "value": 200}],
        "evaluator_opinion": "False claim of pass",
        "strengths": [],
        "concerns": [],
    }
    model = MockStructuredModel(mock_resp)

    with pytest.raises(ValueError, match="contradictory verdict claims are refused"):
        review_case_candidate(model, cand_bytes, ctx, report)


def test_research_case_rejects_invented_passage_hash(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    # Valid admitted evidence ID, but invented / tampered passage hash
    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "passage_citations": [
            {
                "evidence_id": "auscope_nvcl",
                "passage_sha256": "0" * 64,
                "relevance": "Invented passage hash",
            }
        ],
        "supported_requirements": ["depth checking"],
        "unsupported_requirements": [],
        "research_summary": "Summary",
        "limitations": [],
    }
    model = MockStructuredModel(mock_resp)

    with pytest.raises(ValueError, match="tampered or invented passage hash"):
        research_case(model, "testing passage hash integrity", ctx)


def test_research_case_detects_unsupported_domains(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "passage_citations": [],
        "supported_requirements": ["depth checking"],
        "unsupported_requirements": [],
        "research_summary": "Summary",
        "limitations": [],
    }
    model = MockStructuredModel(mock_resp)

    advisory = research_case(
        model,
        "Generating realistic drill core photograph images and geological prediction",
        ctx,
    )
    unsupported_text = " ".join(advisory["unsupported_requirements"]).lower()
    assert "core photograph" in unsupported_text
    assert "geological prediction" in unsupported_text


def test_assess_case_source_rejects_identity_mismatches(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    # Wrong case ID
    model_wrong_case = MockStructuredModel(
        {
            "case_id": "wrong_case_id",
            "source_sha256": ctx["binding"]["source_sha256"],
            "cited_evidence_ids": ["auscope_nvcl"],
            "fit_assessment": "assessment",
            "source_limitations": [],
            "teaching_suitability": "suitability",
        }
    )
    with pytest.raises(ValueError, match="model returned case_id"):
        assess_case_source(model_wrong_case, ctx)

    # Wrong source hash
    model_wrong_src = MockStructuredModel(
        {
            "case_id": "hylogger_scalar_teaching_v1",
            "source_sha256": "1" * 64,
            "cited_evidence_ids": ["auscope_nvcl"],
            "fit_assessment": "assessment",
            "source_limitations": [],
            "teaching_suitability": "suitability",
        }
    )
    with pytest.raises(ValueError, match="model returned source_sha256"):
        assess_case_source(model_wrong_src, ctx)


def test_verify_case_members_tampered_thresholds_hash(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)
    members = build_case_members(cand_bytes, ctx, report, "f" * 64)
    members["data.csv"] = cand_bytes

    # Tamper with thresholds_sha256 in case-binding.json
    binding_data = json.loads(members["case-binding.json"].decode("utf-8"))
    binding_data["thresholds_sha256"] = "9" * 64
    members["case-binding.json"] = json.dumps(binding_data).encode("utf-8")

    res = verify_case_members(members)
    assert res["verified"] is False
    assert any("binding thresholds hash" in d for d in res["discrepancies"])


# =====================================================================
# Real Completion / Usage Positive Controls & Schema Negatives
# =====================================================================


def test_ai_helpers_with_real_completion_and_usage(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)

    epack = get_evidence_pack()
    nvcl_sha = epack["auscope_nvcl"]["passage_sha256"]

    # 1. research_case with real Completion and Usage
    research_usage = Usage(
        model="test-gemini", input_tokens=150, output_tokens=70, calls=1
    )
    research_completion = Completion(
        data={
            "case_id": "hylogger_scalar_teaching_v1",
            "case_version": 1,
            "cited_evidence_ids": ["auscope_nvcl"],
            "passage_citations": [
                {
                    "evidence_id": "auscope_nvcl",
                    "passage_sha256": nvcl_sha,
                    "relevance": "Supports QA/QC activities in NVCL.",
                }
            ],
            "supported_requirements": ["depth checking"],
            "unsupported_requirements": [],
            "research_summary": "Summary with real Completion object.",
            "limitations": ["Teaching tolerances only."],
        },
        usage=research_usage,
    )
    model_research = MockStructuredModel(research_completion)
    res_adv = research_case(model_research, "Teaching depth QA", ctx)
    assert res_adv["advisory_kind"] == "case_research"
    assert "usage" not in res_adv
    assert json.loads(json.dumps(res_adv)) == res_adv

    # 2. assess_case_source with real Completion and Usage
    source_usage = Usage(
        model="test-gemini", input_tokens=220, output_tokens=85, calls=1
    )
    source_completion = Completion(
        data={
            "case_id": "hylogger_scalar_teaching_v1",
            "source_sha256": ctx["binding"]["source_sha256"],
            "cited_evidence_ids": ["auscope_nvcl"],
            "fit_assessment": "Assessed with real Completion object.",
            "source_limitations": ["Drilled intervals only."],
            "teaching_suitability": "Well-suited.",
        },
        usage=source_usage,
    )
    model_source = MockStructuredModel(source_completion)
    src_adv = assess_case_source(model_source, ctx)
    assert src_adv["advisory_kind"] == "source_assessment"
    assert "usage" not in src_adv
    assert json.loads(json.dumps(src_adv)) == src_adv

    # 3. review_case_candidate with real Completion and Usage
    review_usage = Usage(
        model="test-gemini", input_tokens=310, output_tokens=95, calls=1
    )
    review_completion = Completion(
        data={
            "case_id": "hylogger_scalar_teaching_v1",
            "candidate_sha256": report["candidate_sha256"],
            "code_verdict_acknowledged": "pass",
            "operator_recommendation": "recommend_proceed_to_human_gate",
            "evidence_facts": [{"measurement": "row_count", "value": 200}],
            "evaluator_opinion": "Adheres to contract.",
            "strengths": ["Even depth spacing."],
            "concerns": [],
        },
        usage=review_usage,
    )
    model_review = MockStructuredModel(review_completion)
    rev_adv = review_case_candidate(model_review, cand_bytes, ctx, report)
    assert rev_adv["advisory_kind"] == "candidate_review"
    assert "usage" not in rev_adv
    assert json.loads(json.dumps(rev_adv)) == rev_adv


def test_ai_helpers_nested_schema_and_scalar_type_negatives(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    epack = get_evidence_pack()
    nvcl_sha = epack["auscope_nvcl"]["passage_sha256"]

    base_valid_data = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "passage_citations": [
            {
                "evidence_id": "auscope_nvcl",
                "passage_sha256": nvcl_sha,
                "relevance": "Supports QA.",
            }
        ],
        "supported_requirements": ["depth checking"],
        "unsupported_requirements": [],
        "research_summary": "Summary",
        "limitations": [],
    }

    # 1. Float scalar alias for integer case_version
    bad_float_version = dict(base_valid_data)
    bad_float_version["case_version"] = 1.0
    with pytest.raises(ValueError, match=r"must be.*integer, got float"):
        research_case(
            MockStructuredModel(Completion(data=bad_float_version, usage=Usage("m"))),
            "testing float version",
            ctx,
        )

    # 2. Boolean scalar alias for integer case_version
    bad_bool_version = dict(base_valid_data)
    bad_bool_version["case_version"] = True
    with pytest.raises(
        ValueError, match=r"(True is not of type 'integer'|must be.*integer, got bool)"
    ):
        research_case(
            MockStructuredModel(Completion(data=bad_bool_version, usage=Usage("m"))),
            "testing bool version",
            ctx,
        )

    # 3. Missing required property
    missing_req = dict(base_valid_data)
    del missing_req["research_summary"]
    with pytest.raises(
        ValueError,
        match=r"('research_summary' is a required property|missing required property)",
    ):
        research_case(
            MockStructuredModel(Completion(data=missing_req, usage=Usage("m"))),
            "testing missing req",
            ctx,
        )

    # 4. Unexpected additional property
    extra_prop = dict(base_valid_data)
    extra_prop["unwanted_field"] = "malicious_injection"
    with pytest.raises(
        ValueError,
        match=r"(Additional properties are not allowed|unexpected additional properties)",
    ):
        research_case(
            MockStructuredModel(Completion(data=extra_prop, usage=Usage("m"))),
            "testing extra prop",
            ctx,
        )

    # 5. Wrong scalar type in array
    bad_array_item = dict(base_valid_data)
    bad_array_item["cited_evidence_ids"] = [12345]  # int instead of str
    with pytest.raises(
        ValueError, match=r"(is not of type 'string'|must be a string, got int)"
    ):
        research_case(
            MockStructuredModel(Completion(data=bad_array_item, usage=Usage("m"))),
            "testing bad array item",
            ctx,
        )

    # 6. Unexpected extra property in nested object
    bad_nested = dict(base_valid_data)
    bad_nested["passage_citations"] = [
        {
            "evidence_id": "auscope_nvcl",
            "passage_sha256": nvcl_sha,
            "relevance": "Supports QA.",
            "unexpected_nested_key": True,
        }
    ]
    with pytest.raises(
        ValueError,
        match=r"(Additional properties are not allowed|unexpected additional properties)",
    ):
        research_case(
            MockStructuredModel(Completion(data=bad_nested, usage=Usage("m"))),
            "testing bad nested prop",
            ctx,
        )

    # 7. Completion data is not a Mapping
    with pytest.raises(TypeError, match="Completion dataclass with .data or a Mapping"):
        research_case(
            MockStructuredModel(Completion(data="not a dict", usage=Usage("m"))),
            "testing non-mapping data",
            ctx,
        )


def test_case_check_results_fail_closed_on_inconsistent_reports(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    valid_report = evaluate_case_context(cand_bytes, ctx)

    # Case 1: passed=False with unknown/global failure -> all 13 must fail
    crash_report = dict(valid_report)
    crash_report["passed"] = False
    crash_report["failures"] = ["unexpected_pipeline_crash"]
    results = case_check_results(crash_report)
    assert len(results) == 13
    assert all(r["status"] == "fail" for r in results)
    assert all("overall evaluation failed" in r["detail"] for r in results)

    # Case 2: passed=False with empty failures list -> all 13 must fail
    empty_failures_report = dict(valid_report)
    empty_failures_report["passed"] = False
    empty_failures_report["failures"] = []
    results_empty = case_check_results(empty_failures_report)
    assert len(results_empty) == 13
    assert all(r["status"] == "fail" for r in results_empty)
    assert all(
        "passed is False but failures list is empty" in r["detail"]
        for r in results_empty
    )

    # Case 3: passed=True with non-empty failures -> all 13 must fail
    contradictory_report = dict(valid_report)
    contradictory_report["passed"] = True
    contradictory_report["failures"] = ["row_count"]
    results_contra = case_check_results(contradictory_report)
    assert len(results_contra) == 13
    assert all(r["status"] == "fail" for r in results_contra)
    assert all(
        "passed is True but failures present" in r["detail"] for r in results_contra
    )

    # Case 4: passed is integer alias (1 instead of True) -> all 13 must fail
    int_alias_report = dict(valid_report)
    int_alias_report["passed"] = 1
    results_int = case_check_results(int_alias_report)
    assert len(results_int) == 13
    assert all(r["status"] == "fail" for r in results_int)
    assert all("must be boolean, got int" in r["detail"] for r in results_int)


def test_assess_case_source_regenerates_summary_ignoring_planted_canary(
    tmp_path: Path,
) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    # Plant a malicious canary into context["disclosed_model_summary"]
    canary_token = "LEAKED_PRIVATE_ROW_DATA_CANARY_xyz123"
    ctx["disclosed_model_summary"] = {
        "canary_secret": canary_token,
        "raw_rows": ["StartDepth,1.0,2.0,CHLORITE"],
    }

    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "source_sha256": ctx["binding"]["source_sha256"],
        "cited_evidence_ids": ["auscope_nvcl"],
        "fit_assessment": "Source assessed securely.",
        "source_limitations": [],
        "teaching_suitability": "Suitable.",
    }
    model = MockStructuredModel(mock_resp)

    assess_case_source(model, ctx)

    # Verify that the planted canary NEVER appeared in the model prompt
    recorded_prompt = model.recorded_prompts[0]
    assert canary_token not in recorded_prompt
    assert "LEAKED_PRIVATE_ROW_DATA" not in recorded_prompt

    # Verify legitimate disclosed keys ARE present
    assert "Median spacing" in recorded_prompt
    assert "Valid fraction" in recorded_prompt


def test_research_case_keyword_scan_handles_explicit_negatives(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "passage_citations": [],
        "supported_requirements": ["depth checking"],
        "unsupported_requirements": [],
        "research_summary": "Summary",
        "limitations": [],
    }

    # 1. Negative requests should NOT be flagged as unsupported
    negative_purpose = (
        "Teaching exercise for QA. No privacy guarantee requested. "
        "Without core photograph images. Does not require geological prediction."
    )
    model_neg = MockStructuredModel(mock_resp)
    adv_neg = research_case(model_neg, negative_purpose, ctx)
    unsupported_neg = " ".join(adv_neg["unsupported_requirements"]).lower()
    assert "privacy guarantee" not in unsupported_neg
    assert "core photograph" not in unsupported_neg
    assert "geological prediction" not in unsupported_neg

    # 2. Affirmative requests MUST be flagged as unsupported
    affirmative_purpose = (
        "Please provide core photograph images and privacy guarantee for students."
    )
    model_aff = MockStructuredModel(mock_resp)
    adv_aff = research_case(model_aff, affirmative_purpose, ctx)
    unsupported_aff = " ".join(adv_aff["unsupported_requirements"]).lower()
    assert "privacy guarantee" in unsupported_aff
    assert "core photograph" in unsupported_aff


def test_evidence_facts_measured_values_and_prose_screening(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)
    c_sha = hashlib.sha256(cand_bytes).hexdigest()

    base_review = {
        "case_id": "hylogger_scalar_teaching_v1",
        "candidate_sha256": c_sha,
        "code_verdict_acknowledged": "pass",
        "operator_recommendation": "recommend_proceed_to_human_gate",
        "evidence_facts": [
            {"measurement": "row_count", "value": 200},
            {"measurement": "inclusion_fraction.candidate", "value": 0.75},
        ],
        "evaluator_opinion": "Adheres strictly to contract constraints.",
        "strengths": [],
        "concerns": [],
    }

    # 1. Positive control: genuine structured measured facts pass
    adv = review_case_candidate(
        MockStructuredModel(base_review), cand_bytes, ctx, report
    )
    assert adv["evidence_facts"] == [
        {"measurement": "row_count", "value": 200},
        {"measurement": "inclusion_fraction.candidate", "value": 0.75},
    ]

    # 2. Negative control: old string format is rejected by schema
    bad_review_string = dict(
        base_review,
        evidence_facts=["row_count 200"],
    )
    with pytest.raises(ValueError):
        review_case_candidate(
            MockStructuredModel(bad_review_string), cand_bytes, ctx, report
        )

    # 3. Negative control: false integer value
    bad_review_num = dict(
        base_review,
        evidence_facts=[{"measurement": "row_count", "value": 12345}],
    )
    with pytest.raises(ValueError, match="does not match expected integer"):
        review_case_candidate(
            MockStructuredModel(bad_review_num), cand_bytes, ctx, report
        )

    # 4. Negative control: false float value
    bad_review_float = dict(
        base_review,
        evidence_facts=[{"measurement": "inclusion_fraction.candidate", "value": 0.99}],
    )
    with pytest.raises(ValueError, match="does not match expected float"):
        review_case_candidate(
            MockStructuredModel(bad_review_float), cand_bytes, ctx, report
        )

    # 5. Negative control: float alias for integer expected path
    bad_review_alias = dict(
        base_review,
        evidence_facts=[{"measurement": "row_count", "value": 200.0}],
    )
    with pytest.raises(ValueError, match="does not match expected integer"):
        review_case_candidate(
            MockStructuredModel(bad_review_alias), cand_bytes, ctx, report
        )

    # 6. Negative control: boolean alias
    bad_review_bool = dict(
        base_review,
        evidence_facts=[{"measurement": "row_count", "value": True}],
    )
    with pytest.raises(ValueError):
        review_case_candidate(
            MockStructuredModel(bad_review_bool), cand_bytes, ctx, report
        )

    # 7. Negative control: unknown measurement path
    bad_review_unknown = dict(
        base_review,
        evidence_facts=[{"measurement": "unknown_measurement_path", "value": 200}],
    )
    with pytest.raises(ValueError):
        review_case_candidate(
            MockStructuredModel(bad_review_unknown), cand_bytes, ctx, report
        )

    # 8. Negative control: extra keys in fact object
    bad_review_extra = dict(
        base_review,
        evidence_facts=[
            {"measurement": "row_count", "value": 200, "extra": "forbidden"}
        ],
    )
    with pytest.raises(ValueError):
        review_case_candidate(
            MockStructuredModel(bad_review_extra), cand_bytes, ctx, report
        )

    # 9. Negative control: duplicate measurement paths
    bad_review_dup = dict(
        base_review,
        evidence_facts=[
            {"measurement": "row_count", "value": 200},
            {"measurement": "row_count", "value": 200},
        ],
    )
    with pytest.raises(ValueError, match="duplicate evidence_fact measurement path"):
        review_case_candidate(
            MockStructuredModel(bad_review_dup), cand_bytes, ctx, report
        )

    # 10. Negative control: empty list
    bad_review_empty = dict(base_review, evidence_facts=[])
    with pytest.raises(ValueError):
        review_case_candidate(
            MockStructuredModel(bad_review_empty), cand_bytes, ctx, report
        )

    # 11. Negative control: prohibited prose in evaluator_opinion
    bad_review_op = dict(
        base_review,
        evaluator_opinion="Fully anonymised; predicts ore body",
    )
    with pytest.raises(ValueError, match="prohibited claim"):
        review_case_candidate(
            MockStructuredModel(bad_review_op), cand_bytes, ctx, report
        )

    # 12. Negative control: prohibited prose in research_summary
    research_payload = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "supported_requirements": ["scalar table"],
        "unsupported_requirements": [],
        "research_summary": "Fully anonymised and public domain; predicts grade",
        "limitations": [],
    }
    with pytest.raises(ValueError, match="prohibited claim"):
        research_case(MockStructuredModel(research_payload), "teach scalar QC", ctx)

    # 13. Negative control: prohibited prose in fit_assessment
    assess_payload = {
        "case_id": "hylogger_scalar_teaching_v1",
        "source_sha256": ctx["binding"]["source_sha256"],
        "cited_evidence_ids": ["auscope_nvcl"],
        "fit_assessment": "Public domain, anonymised, safe to republish everywhere",
        "source_limitations": [],
        "teaching_suitability": "ok",
    }
    with pytest.raises(ValueError, match="prohibited claim"):
        assess_case_source(MockStructuredModel(assess_payload), ctx)


def test_case_check_results_measurement_shape_validation(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    rep = evaluate_case_context(cand_bytes, ctx)

    def chk(mod: Any) -> list[str]:
        import copy

        r2 = copy.deepcopy(rep)
        mod(r2["measurements"])
        return [x["check_id"] for x in case_check_results(r2) if x["status"] == "fail"]

    # 1. Genuine all 13 pass positive control
    res_pass = case_check_results(rep)
    assert [x["status"] for x in res_pass].count("pass") == 13
    assert [x["check_id"] for x in res_pass if x["status"] == "fail"] == []

    # 2. Honest only inclusion failure positive control
    cbad = cand_bytes.replace(b"true", b"false", 50)
    rbad = evaluate_case_context(cbad, ctx)
    res_bad = case_check_results(rbad)
    assert [x["check_id"] for x in res_bad if x["status"] == "fail"] == [
        "case.inclusion_fraction"
    ]
    assert [x["status"] for x in res_bad].count("pass") == 12

    # 3. Native N10: structure included_rows is string (must not crash, returns 13 fails)
    assert (
        len(
            chk(
                lambda m: m["downstream_structure_both_states_present"].__setitem__(
                    "included_rows", "x"
                )
            )
        )
        == 13
    )

    # 4. Native N11: well_formed_rows malformed_detail str (check fails cleanly)
    assert chk(
        lambda m: m["well_formed_rows"].__setitem__("malformed_detail", "bad")
    ) == ["case.well_formed_rows"]

    # 5. Native N12: source_pin_match pins None (fails closed)
    assert (
        len(
            chk(
                lambda m: m["source_pin_match"].update(
                    {"contract_pin": None, "reference_pin": None}
                )
            )
        )
        == 13
    )

    # 6. Native N13: bool in downstream_filter_group count (fails closed)
    assert (
        len(chk(lambda m: m["downstream_filter_group"].__setitem__("included", True)))
        == 13
    )

    # 7. Native N14: limitations wrong/empty (0 passes, 13 fails)
    import copy

    r_n14 = copy.deepcopy(rep)
    r_n14["limitations"] = []
    assert [x["status"] for x in case_check_results(r_n14)].count("pass") == 0

    # 8. Native N15: candidate_sha256 int (0 passes, 13 fails)
    r_n15 = copy.deepcopy(rep)
    r_n15["candidate_sha256"] = 5
    assert [x["status"] for x in case_check_results(r_n15)].count("pass") == 0

    # 9. Native N16: candidate_sha256 missing (0 passes, 13 fails)
    r_n16 = copy.deepcopy(rep)
    del r_n16["candidate_sha256"]
    assert [x["status"] for x in case_check_results(r_n16)].count("pass") == 0

    # 10. Native N17: global failure visible (13 fails)
    r_n17 = copy.deepcopy(rep)
    r_n17["passed"] = False
    r_n17["failures"] = ["invalid_utf8_encoding"]
    r_n17["measurements"] = {}
    assert [x["status"] for x in case_check_results(r_n17)].count("fail") == 13

    # 11. Native N18: early report known + global (13 fails)
    r_n18 = copy.deepcopy(rep)
    r_n18["passed"] = False
    r_n18["failures"] = ["no_usable_rows", "row_count"]
    assert [x["status"] for x in case_check_results(r_n18)].count("fail") == 13

    # 12. Native N19: inclusion extra subkey (fails closed, 13 fails)
    assert len(chk(lambda m: m["inclusion_fraction"].__setitem__("extra", 1))) == 13

    # 13. Native N20: inclusion missing subkeys (fails closed, 13 fails)
    assert (
        len(chk(lambda m: m.__setitem__("inclusion_fraction", {"candidate": 0.75})))
        == 13
    )

    # 14. Native N21: depth_spacing string candidate (fails closed, 13 fails)
    assert len(chk(lambda m: m["depth_spacing"].__setitem__("candidate", "x"))) == 13

    # 15. Native N22: structure exc_rows None (fails closed, 13 fails)
    assert (
        len(
            chk(
                lambda m: m["downstream_structure_both_states_present"].__setitem__(
                    "excluded_rows", None
                )
            )
        )
        == 13
    )

    # 16. Native N23: well_formed total string (fails closed, 13 fails)
    assert len(chk(lambda m: m["well_formed_rows"].__setitem__("total", "200"))) == 13

    # 17. Native N24: swir_answer_rate scalar instead of mapping (fails closed, 13 fails)
    assert len(chk(lambda m: m.__setitem__("swir_answer_rate", 0.8))) == 13

    # 18. Native N25: source_pin_match str (fails closed, 13 fails)
    assert len(chk(lambda m: m.__setitem__("source_pin_match", "ok"))) == 13

    # 19. Native N26: ordered_depth None (fails closed, 13 fails)
    assert len(chk(lambda m: m.__setitem__("ordered_depth", None))) == 13

    # 20. Native N27: ordered_depth int 1 (fails closed, 13 fails)
    assert len(chk(lambda m: m.__setitem__("ordered_depth", 1))) == 13

    # 21. Unhashable failure list regression test (never crashes with TypeError, 13 fails)
    r_unhashable = copy.deepcopy(rep)
    r_unhashable["failures"] = [{"bad": 1}]
    assert [x["status"] for x in case_check_results(r_unhashable)].count("pass") == 0

    # 22. Extra measurement key fails all 13 checks
    assert len(chk(lambda m: m.__setitem__("zzz", 1))) == 13

    # 23. Extra report top-level key fails all 13 checks (0 passes)
    rep_extra = copy.deepcopy(dict(rep))
    rep_extra["extra"] = 1
    assert [x["status"] for x in case_check_results(rep_extra)].count("pass") == 0


def test_review_case_candidate_k9_absent_fact_raises_value_error(
    tmp_path: Path,
) -> None:
    """Test K9 and K9b: structured fact referencing absent path raises ValueError, while available path succeeds."""
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    # Early failure candidate: headers only, 0 usable rows
    cn = b"depth_m,valid,swir_group,tir_group\n"
    rn = evaluate_case_context(cn, ctx)
    ns = hashlib.sha256(cn).hexdigest()

    # Fact path absent in early-fail report raises ValueError (not KeyError)
    from models.base import Completion, Usage

    class StubModel:
        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data

        def complete(self, prompt: str, schema: Any) -> Completion:
            return Completion(data=copy.deepcopy(self.data), usage=Usage(model="m"))

    m_k9 = StubModel(
        {
            "case_id": "hylogger_scalar_teaching_v1",
            "candidate_sha256": ns,
            "code_verdict_acknowledged": "fail",
            "operator_recommendation": "recommend_reject",
            "strengths": [],
            "concerns": ["no usable rows"],
            "evidence_facts": [
                {"measurement": "inclusion_fraction.candidate", "value": 0.5}
            ],
            "evaluator_opinion": "reject",
        }
    )
    with pytest.raises(
        ValueError, match="path 'inclusion_fraction.candidate' not found"
    ):
        review_case_candidate(m_k9, cn, ctx, rn)

    # K9b: fact for available path (row_count: 0) on the same early-fail report succeeds
    m_k9b = StubModel(
        {
            "case_id": "hylogger_scalar_teaching_v1",
            "candidate_sha256": ns,
            "code_verdict_acknowledged": "fail",
            "operator_recommendation": "recommend_reject",
            "strengths": [],
            "concerns": ["no usable rows"],
            "evidence_facts": [{"measurement": "row_count", "value": 0}],
            "evaluator_opinion": "reject",
        }
    )
    res_k9b = review_case_candidate(m_k9b, cn, ctx, rn)
    assert res_k9b["evidence_facts"] == [{"measurement": "row_count", "value": 0}]


def test_source_fit_receipt_and_evidence_sha_strictness(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)
    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)

    import copy

    # Missing receipt
    f_missing = copy.deepcopy(ctx)
    f_missing.pop("source_fit_receipt")
    with pytest.raises(ValueError, match="source_fit_receipt"):
        evaluate_case_context(cand_bytes, f_missing)

    # String receipt
    f_str = copy.deepcopy(ctx)
    f_str["source_fit_receipt"] = "yes"
    with pytest.raises(ValueError, match="must be a mapping"):
        evaluate_case_context(cand_bytes, f_str)

    # Wrong source receipt (digest updated to isolate source mismatch)
    f_wrong_src = copy.deepcopy(ctx)
    f_wrong_src["source_fit_receipt"]["source_sha256"] = "f" * 64
    f_wrong_src["binding"]["source_fit_sha256"] = hashlib.sha256(
        _canonical_bytes(f_wrong_src["source_fit_receipt"])
    ).hexdigest()
    with pytest.raises(ValueError, match="does not match binding"):
        evaluate_case_context(cand_bytes, f_wrong_src)

    # Suitable not true (digest updated to isolate suitable check)
    f_not_suitable = copy.deepcopy(ctx)
    f_not_suitable["source_fit_receipt"]["suitable"] = "no"
    f_not_suitable["binding"]["source_fit_sha256"] = hashlib.sha256(
        _canonical_bytes(f_not_suitable["source_fit_receipt"])
    ).hexdigest()
    with pytest.raises(ValueError, match="not suitable or invalid"):
        evaluate_case_context(cand_bytes, f_not_suitable)

    # Tampered receipt content (digest mismatch)
    f_tamper = copy.deepcopy(ctx)
    f_tamper["source_fit_receipt"]["findings"] = "Tampered findings"
    with pytest.raises(ValueError, match="source_fit_sha256"):
        evaluate_case_context(cand_bytes, f_tamper)

    # Altered source_rows in receipt
    f_rows = copy.deepcopy(ctx)
    f_rows["source_fit_receipt"]["source_rows"] = 999
    with pytest.raises(ValueError, match="source_fit_sha256"):
        evaluate_case_context(cand_bytes, f_rows)

    # Altered aggregates in receipt
    f_aggs = copy.deepcopy(ctx)
    f_aggs["source_fit_receipt"]["aggregates"]["valid_fraction"] = 0.12
    with pytest.raises(ValueError, match="source_fit_sha256"):
        evaluate_case_context(cand_bytes, f_aggs)

    # Invalid source_fit_sha256
    for bad_fit_sha in ("", None, 0, "0" * 64):
        f_bsha = copy.deepcopy(ctx)
        f_bsha["binding"]["source_fit_sha256"] = bad_fit_sha
        with pytest.raises(ValueError):
            evaluate_case_context(cand_bytes, f_bsha)

    # Invalid evidence_sha256
    for bad_ev in ("", None, 0, "0" * 64):
        f_ev = copy.deepcopy(ctx)
        f_ev["binding"]["evidence_sha256"] = bad_ev
        with pytest.raises(ValueError):
            evaluate_case_context(cand_bytes, f_ev)

    # Licence status rewrite in reference
    f_lic = copy.deepcopy(ctx)
    f_lic["reference"]["licence_status"] = "disclosed: UPGRADED"
    with pytest.raises(ValueError, match="binding reference_sha256"):
        evaluate_case_context(cand_bytes, f_lic)


def test_repair_case_plan_target_and_strategy_strictness(tmp_path: Path) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    # Conflicting target_rows and n_rows
    with pytest.raises(ValueError, match="conflict or are invalid"):
        repair_case_plan(
            {
                "seed": 1,
                "target_rows": 200,
                "n_rows": 7,
                "strategy": "fitted_markov_joint",
            },
            ["case.inclusion_fraction"],
            ctx,
        )

    # target_rows = 0
    with pytest.raises(ValueError, match="conflict or are invalid"):
        repair_case_plan(
            {
                "seed": 1,
                "target_rows": 0,
                "n_rows": 200,
                "strategy": "fitted_markov_joint",
            },
            ["case.inclusion_fraction"],
            ctx,
        )

    # Unknown strategy
    with pytest.raises(ValueError, match="unsupported or unknown plan strategy"):
        repair_case_plan(
            {"seed": 1, "n_rows": 200, "strategy": "weird"},
            ["case.inclusion_fraction"],
            ctx,
        )

    # Positive control: valid repair move
    repaired = repair_case_plan(
        {
            "seed": 1,
            "target_rows": 200,
            "n_rows": 200,
            "strategy": "fitted_markov_joint",
        },
        ["case.inclusion_fraction"],
        ctx,
    )
    assert repaired["strategy"] == "fitted_markov_balanced"
    assert repaired["seed"] == 1


def test_research_case_paraphrase_and_negation_in_supported_reqs(
    tmp_path: Path,
) -> None:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    ctx = prepare_case_context(src, spec)

    mock_resp = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "passage_citations": [],
        "supported_requirements": ["no privacy guarantee"],
        "unsupported_requirements": [],
        "research_summary": "Summary",
        "limitations": [],
    }

    # Negated requirement in supported_requirements does not false-refuse
    res = research_case(MockStructuredModel(mock_resp), "Teaching depth QA", ctx)
    assert res["supported_requirements"] == ["no privacy guarantee"]

    # Affirmative unsupported requirement in supported_requirements raises ValueError
    bad_resp = dict(mock_resp, supported_requirements=["reflectance curve generation"])
    with pytest.raises(ValueError, match="unsupported requirement as supported"):
        research_case(MockStructuredModel(bad_resp), "Teaching depth QA", ctx)

    # Paraphrased unsupported request in purpose is detected
    paraphrase_purpose = "Please predict mineral grade from the cores and guarantee anonymity of the drillers"
    res_para = research_case(MockStructuredModel(mock_resp), paraphrase_purpose, ctx)
    unsupported_text = " ".join(res_para["unsupported_requirements"]).lower()
    assert "mineral grade" in unsupported_text
    assert "predict mineral" in unsupported_text
    assert "guarantee anonymity" in unsupported_text


# =====================================================================
# Prose screen output contract and diagnosability
# =====================================================================


class _StopAfterPrompt(Exception):
    pass


def _prompt_capturing_model() -> MockStructuredModel:
    def responder(prompt: str, schema: Mapping[str, Any]) -> Any:
        raise _StopAfterPrompt

    return MockStructuredModel(responder)


def _scalar_case_ctx(tmp_path: Path) -> dict[str, Any]:
    src = _make_source_csv(tmp_path, n_rows=120)
    spec = {
        "case": {"id": "hylogger_scalar_teaching_v1", "version": 1},
        "target_rows": 200,
        "source": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
        "outputs": ["scalar"],
    }
    return prepare_case_context(src, spec)


def _research_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "case_id": "hylogger_scalar_teaching_v1",
        "case_version": 1,
        "cited_evidence_ids": ["auscope_nvcl"],
        "supported_requirements": ["scalar table"],
        "unsupported_requirements": [],
        "research_summary": "Admitted evidence supports scalar quality checking.",
        "limitations": ["Does not establish industry standard tolerances."],
    }
    payload.update(overrides)
    return payload


def test_prose_contract_lists_every_screened_term() -> None:
    clause = prohibited_prose_contract(("research_summary", "limitations"))
    for term in PROHIBITED_PROSE_PATTERNS:
        assert repr(term) in clause
    assert "even in a negated sentence" in clause
    assert "research_summary, limitations" in clause


def test_all_three_screened_roles_publish_the_contract(tmp_path: Path) -> None:
    ctx = _scalar_case_ctx(tmp_path)
    research_model = _prompt_capturing_model()
    with pytest.raises(_StopAfterPrompt):
        research_case(research_model, "teach scalar QC", ctx)
    assert (
        prohibited_prose_contract(("research_summary", "limitations"))
        in research_model.recorded_prompts[0]
    )

    assess_model = _prompt_capturing_model()
    with pytest.raises(_StopAfterPrompt):
        assess_case_source(assess_model, ctx)
    assert (
        prohibited_prose_contract(
            ("fit_assessment", "teaching_suitability", "source_limitations")
        )
        in assess_model.recorded_prompts[0]
    )

    cand_bytes = _make_candidate_csv(tmp_path, n_rows=200)
    report = evaluate_case_context(cand_bytes, ctx)
    review_model = _prompt_capturing_model()
    with pytest.raises(_StopAfterPrompt):
        review_case_candidate(review_model, cand_bytes, ctx, report)
    assert (
        prohibited_prose_contract(("evaluator_opinion", "strengths", "concerns"))
        in review_model.recorded_prompts[0]
    )


def test_negated_disclaimer_is_still_rejected_and_diagnosable(
    tmp_path: Path,
) -> None:
    """Positive control: the screen is not weakened for negated text."""
    ctx = _scalar_case_ctx(tmp_path)
    payload = _research_payload(
        limitations=[
            "Does not establish industry standard tolerances.",
            "No privacy or anonymity guarantee is made.",
        ]
    )
    with pytest.raises(ValueError) as info:
        research_case(MockStructuredModel(payload), "teach scalar QC", ctx)
    message = str(info.value)
    assert message.startswith(
        "limitations[1] contains unverified or prohibited claim: 'anonymity'"
    )
    assert "negation marker nearby: yes" in message
    # The rejected sentence itself is not echoed.
    assert "guarantee is made" not in message


def test_affirmative_claim_rejected_with_no_negation(tmp_path: Path) -> None:
    ctx = _scalar_case_ctx(tmp_path)
    payload = _research_payload(research_summary="The output preserves anonymity.")
    with pytest.raises(ValueError, match="negation marker nearby: no"):
        research_case(MockStructuredModel(payload), "teach scalar QC", ctx)


@pytest.mark.parametrize("term", PROHIBITED_PROSE_PATTERNS)
def test_every_screened_term_still_rejects(tmp_path: Path, term: str) -> None:
    ctx = _scalar_case_ctx(tmp_path)
    payload = _research_payload(limitations=[f"Note: {term.upper()} here."])
    with pytest.raises(ValueError, match="prohibited claim"):
        research_case(MockStructuredModel(payload), "teach scalar QC", ctx)


def test_admitted_passage_digest_in_prose_is_not_a_claim(tmp_path: Path) -> None:
    """Negative control: an admitted SHA-256 whose hex contains 'cc0'."""
    ctx = _scalar_case_ctx(tmp_path)
    digest = get_evidence_pack()["gswa_hackathon_2026"]["passage_sha256"]
    assert "cc0" in digest
    payload = _research_payload(
        research_summary=f"Passage sha256:{digest} is cited for context."
    )
    advisory = research_case(MockStructuredModel(payload), "teach scalar QC", ctx)
    assert digest in advisory["research_summary"]


@pytest.mark.parametrize(
    "token",
    [
        "cc0",
        "CC0 licensed",
        "a" * 63 + "cc0",  # 66 hex chars: not a digest token
        "g" + "0" * 61 + "cc0",  # non-hex neighbour
        "x" + "0" * 61 + "cc0x",  # embedded in a word
    ],
)
def test_non_digest_cc0_still_rejected(tmp_path: Path, token: str) -> None:
    ctx = _scalar_case_ctx(tmp_path)
    payload = _research_payload(research_summary=f"Data is {token}.")
    with pytest.raises(ValueError, match="'cc0'"):
        research_case(MockStructuredModel(payload), "teach scalar QC", ctx)


def test_review_and_assess_errors_name_item_index(tmp_path: Path) -> None:
    ctx = _scalar_case_ctx(tmp_path)
    assess_payload = {
        "case_id": "hylogger_scalar_teaching_v1",
        "source_sha256": ctx["binding"]["source_sha256"],
        "cited_evidence_ids": ["auscope_nvcl"],
        "fit_assessment": "Aggregates fit the teaching table.",
        "source_limitations": ["One constant spacing.", "Not safe to publish."],
        "teaching_suitability": "ok",
    }
    with pytest.raises(ValueError, match=r"^source_limitations\[1\] contains"):
        assess_case_source(MockStructuredModel(assess_payload), ctx)


@pytest.mark.parametrize(
    "summary",
    [
        "Released as CC-0" + "a" * 63 + " now.",
        "Licensed " + "0" * 62 + "cc-0 waiver.",
        "Digest " + "0" * 61 + "cc0 is CC0.",
    ],
)
def test_term_touching_a_digest_token_is_still_rejected(
    tmp_path: Path, summary: str
) -> None:
    """Regression: only a match wholly inside a digest token is exempt."""
    ctx = _scalar_case_ctx(tmp_path)
    payload = _research_payload(research_summary=summary)
    with pytest.raises(ValueError, match="prohibited claim"):
        research_case(MockStructuredModel(payload), "teach scalar QC", ctx)


def test_research_prompt_states_case_scope_and_source_role(tmp_path: Path) -> None:
    ctx = _scalar_case_ctx(tmp_path)
    model = _prompt_capturing_model()
    with pytest.raises(_StopAfterPrompt):
        research_case(model, "teach scalar QC", ctx)
    prompt = model.recorded_prompts[0]
    assert "Case purpose: " in prompt and "Case purpose: None" not in prompt
    assert (
        "Case output columns: ['depth_m', 'valid', 'swir_group', 'tir_group']" in prompt
    )
    assert "they are not the data source" in prompt
    assert "fitted by deterministic code from the pinned source" in prompt


def test_research_still_appends_unsupported_domains_from_purpose(
    tmp_path: Path,
) -> None:
    """Scope wording in the prompt does not relax code-side domain detection."""
    ctx = _scalar_case_ctx(tmp_path)
    advisory = research_case(
        MockStructuredModel(_research_payload()),
        "teach scalar QC and include a core photograph with reflectance curve",
        ctx,
    )
    joined = " ".join(advisory["unsupported_requirements"]).lower()
    assert "core photograph" in joined and "reflectance curve" in joined
