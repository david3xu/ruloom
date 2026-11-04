"""Tests for profiles.hylogger.validation.cases."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

from profiles.hylogger.validation.cases import (
    assess_source_suitability,
    build_source_reference,
    compile_case_contract,
    evaluate_case_utility,
    get_case,
    get_evidence_pack,
    load_cases_catalogue,
    validate_case_contract,
    validate_citations,
    verify_case_candidate,
)


def _make_source_csv(
    tmp_path: Path,
    n_rows: int = 120,
    depth_start: float = 0.0,
    spacing: float = 0.0075,
    valid_fraction: float = 0.75,
    columns: list[str] | None = None,
    all_valid: bool = False,
    all_invalid: bool = False,
    no_blanks: bool = False,
    all_blanks: bool = False,
    unordered: bool = False,
    zero_spacing: bool = False,
) -> Path:
    cols = columns or [
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

            if all_blanks:
                swir = ""
                tir = ""
            elif no_blanks:
                swir = "CHLORITE"
                tir = "AMPHIBOLE"
            else:
                swir = "CHLORITE" if (is_valid and (i % 5 != 0)) else ""
                tir = "AMPHIBOLE" if (is_valid and (i % 6 != 0)) else ""

            d_val = (depth - 0.01) if (unordered and i == 10) else depth
            row_data = []
            for c in cols:
                if c == "StartDepth":
                    row_data.append(f"{d_val:.4f}")
                elif c == "EndDepth":
                    row_data.append(f"{d_val + spacing:.4f}")
                elif c == "Final_Mask":
                    row_data.append("1" if is_valid else "0")
                elif c == "Grp1_sTSAS":
                    row_data.append(swir)
                elif c == "Grp1_sjCLST":
                    row_data.append(tir)
                else:
                    row_data.append("x")
            writer.writerow(row_data)
            if not zero_spacing:
                depth += spacing
    return path


def _make_candidate_csv(
    tmp_path: Path,
    n_rows: int = 1000,
    depth_start: float = 0.0,
    spacing: float = 0.0075,
    valid_fraction: float = 0.75,
    output_columns: list[str] | None = None,
    unordered: bool = False,
    unknown_labels: bool = False,
    swir_rate_shift: float = 0.0,
    tir_rate_shift: float = 0.0,
    vf_shift: float = 0.0,
    nonbinary_valid: bool = False,
    invalid_depth: bool = False,
    all_valid: bool = False,
) -> Path:
    cols = output_columns or ["depth_m", "valid", "swir_group", "tir_group"]
    path = tmp_path / "candidate.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        depth = depth_start
        for i in range(n_rows):
            if all_valid:
                is_valid = True
            elif vf_shift != 0.0:
                is_valid = (i / n_rows) < (valid_fraction + vf_shift)
            else:
                is_valid = (i % 4) != 0  # 75% valid

            if unknown_labels and is_valid:
                swir = "UNKNOWN_MINERAL"
            elif is_valid:
                if swir_rate_shift < 0:
                    swir = "CHLORITE" if ((i % 5 != 0) and (i % 3 != 0)) else ""
                else:
                    swir = "CHLORITE" if (i % 5 != 0) else ""
            else:
                swir = ""

            if is_valid:
                if tir_rate_shift < 0:
                    tir = "AMPHIBOLE" if ((i % 6 != 0) and (i % 3 != 0)) else ""
                else:
                    tir = "AMPHIBOLE" if (i % 6 != 0) else ""
            else:
                tir = ""

            d_val = (depth - 0.01) if (unordered and i == 10) else depth
            if invalid_depth and i == 5:
                d_val_str = "-1.0"
            else:
                d_val_str = f"{d_val:.4f}"

            if nonbinary_valid and i == 3:
                v_str = "MAYBE"
            else:
                v_str = "true" if is_valid else "false"

            writer.writerow([d_val_str, v_str, swir, tir])
            depth += spacing
    return path


def test_catalogue_integrity():
    cat = load_cases_catalogue()
    assert "evidence_pack" in cat
    assert "cases" in cat
    pack = cat["evidence_pack"]
    assert "auscope_nvcl" in pack
    assert "gswa_hackathon_2026" in pack
    assert "wapims_core_atlas" in pack
    for eid, item in pack.items():
        computed = hashlib.sha256(item["verbatim_passage"].encode("utf-8")).hexdigest()
        assert computed == item["passage_sha256"]
        assert item["admission_reviewer"] == "Codex coordinator"
        assert item["admission_kind"] != ""
        assert item["reviewed_date"] == "2026-11-04"

    case = cat["cases"]["hylogger_scalar_teaching_v1"]
    assert case["id"] == "hylogger_scalar_teaching_v1"
    assert "researched_facts" in case
    assert "source_licence_evidence" in case
    assert "design_choices" in case
    assert case["design_choices"]["min_satisfiable_rows"] == 25
    assert case["design_choices"]["max_supported_rows"] == 200000


def test_tampered_passage_and_schema_rejection(monkeypatch):
    import profiles.hylogger.validation.cases as cm

    cat = cm.load_cases_catalogue()

    # 1. Tampered passage hash
    bad_cat = json.loads(json.dumps(cat))
    bad_cat["evidence_pack"]["auscope_nvcl"]["verbatim_passage"] += " TAMPERED"

    class FakePath:
        def __init__(self, data):
            self.data = data

        def is_file(self):
            return True

        def read_text(self, encoding="utf-8"):
            return json.dumps(self.data)

    monkeypatch.setattr(cm, "_cases_json_path", lambda: FakePath(bad_cat))
    with pytest.raises(ValueError, match="tampered evidence passage"):
        cm.load_cases_catalogue()

    # 2. Unexpected top-level key
    bad_top = json.loads(json.dumps(cat))
    bad_top["unauthorized_top_level"] = 123
    monkeypatch.setattr(cm, "_cases_json_path", lambda: FakePath(bad_top))
    with pytest.raises(ValueError, match="unexpected top-level keys"):
        cm.load_cases_catalogue()

    # 3. Self-admission by implementer
    bad_rev = json.loads(json.dumps(cat))
    bad_rev["evidence_pack"]["auscope_nvcl"]["admission_reviewer"] = "Gemini"
    monkeypatch.setattr(cm, "_cases_json_path", lambda: FakePath(bad_rev))
    with pytest.raises(ValueError, match="reviewer must differ from implementer"):
        cm.load_cases_catalogue()

    # 4. Unadmitted evidence id in case
    bad_cite = json.loads(json.dumps(cat))
    bad_cite["cases"]["hylogger_scalar_teaching_v1"]["researched_facts"][
        "primary_evidence_ids"
    ].append("fake_evidence_id")
    monkeypatch.setattr(cm, "_cases_json_path", lambda: FakePath(bad_cite))
    with pytest.raises(ValueError, match="cites unadmitted evidence id"):
        cm.load_cases_catalogue()


def test_get_case_and_aliases():
    c1 = get_case("hylogger_scalar_teaching_v1")
    assert c1["id"] == "hylogger_scalar_teaching_v1"
    c2 = get_case("source-grounded scalar data-checking lesson")
    assert c2["id"] == c1["id"]
    c3 = get_case("scalar_teaching_v1")
    assert c3["id"] == c1["id"]
    with pytest.raises(KeyError, match="unknown case 'non_existent_case'"):
        get_case("non_existent_case")


def test_validate_citations():
    pack = get_evidence_pack()
    assert "auscope_nvcl" in pack
    validated = validate_citations(["auscope_nvcl", "gswa_hackathon_2026"])
    assert validated == ["auscope_nvcl", "gswa_hackathon_2026"]

    with pytest.raises(
        ValueError, match="unknown or fabricated evidence id: 'fake_citation'"
    ):
        validate_citations(["auscope_nvcl", "fake_citation"])

    with pytest.raises(ValueError, match="not admitted for the selected case"):
        validate_citations(["wapims_core_atlas"], allowed_ids=["auscope_nvcl"])

    with pytest.raises(ValueError, match="at least one valid evidence citation"):
        validate_citations([])


def test_contract_compilation_and_satisfiability_bounds():
    case_def = get_case("hylogger_scalar_teaching_v1")
    fake_sha = "a" * 64

    # 1. Valid compilation across varying row counts
    for count in (500, 1000, 10000):
        contract = compile_case_contract(case_def, count, fake_sha)
        assert contract["target_rows"] == count
        assert contract["source_sha256"] == fake_sha
        assert contract["checks"]["exact_rows"] == count
        validate_case_contract(contract, case_def)

    # 2. Refusal below satisfiability floor (25)
    with pytest.raises(
        ValueError, match="below the admitted case satisfiability floor"
    ):
        compile_case_contract(case_def, 7, fake_sha)

    with pytest.raises(
        ValueError, match="below the admitted case satisfiability floor"
    ):
        compile_case_contract(case_def, 24, fake_sha)

    # 3. Exactly at floor passes
    contract_25 = compile_case_contract(case_def, 25, fake_sha)
    assert contract_25["target_rows"] == 25

    # 4. Refusal above max supported rows (200000)
    with pytest.raises(ValueError, match="exceeds maximum supported rows"):
        compile_case_contract(case_def, 250000, fake_sha)

    # 5. Non-integer row count
    with pytest.raises(TypeError, match="must be an integer"):
        compile_case_contract(case_def, True, fake_sha)  # type: ignore

    with pytest.raises(TypeError, match="must be an integer"):
        compile_case_contract(case_def, 1000.5, fake_sha)  # type: ignore

    # 6. Invalid SHA-256 pin
    with pytest.raises(ValueError, match="invalid source_sha256 pin"):
        compile_case_contract(case_def, 1000, "not_a_64_hex_string")

    # 7. Tampered contract key rejection
    bad_contract = dict(contract_25)
    bad_contract["unauthorized_override"] = True
    with pytest.raises(ValueError, match="contract has unexpected keys"):
        validate_case_contract(bad_contract, case_def)


def test_assess_source_suitability_matrix(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")

    # 1. Valid source
    src = _make_source_csv(tmp_path, n_rows=150)
    res = assess_source_suitability(src, case_def)
    assert res["suitable"] is True
    assert res["aggregates"]["valid_fraction"] == pytest.approx(0.75, abs=0.01)
    assert res["licence_status"] == "rights unknown"

    # 2. Missing columns
    bad_cols_src = _make_source_csv(
        tmp_path / "bad_cols", columns=["StartDepth", "OtherCol"]
    )
    res_bad = assess_source_suitability(bad_cols_src, case_def)
    assert res_bad["suitable"] is False
    assert any("missing required source columns" in r for r in res_bad["reasons"])

    # 3. Pinned hash check
    res_pinned_ok = assess_source_suitability(
        src, case_def, expected_source_sha256=res["source_sha256"]
    )
    assert res_pinned_ok["suitable"] is True

    res_pinned_fail = assess_source_suitability(
        src, case_def, expected_source_sha256="0" * 64
    )
    assert res_pinned_fail["suitable"] is False
    assert any("source sha256 mismatch" in r for r in res_pinned_fail["reasons"])

    # 4. Unordered depth
    unord_src = _make_source_csv(tmp_path / "unord", n_rows=100, unordered=True)
    res_unord = assess_source_suitability(unord_src, case_def)
    assert res_unord["suitable"] is False
    assert any("not monotonically non-decreasing" in r for r in res_unord["reasons"])

    # 5. Non-positive spacing
    zero_sp_src = _make_source_csv(tmp_path / "zero_sp", n_rows=100, zero_spacing=True)
    res_zero = assess_source_suitability(zero_sp_src, case_def)
    assert res_zero["suitable"] is False
    assert any("positive adjacent depth steps" in r for r in res_zero["reasons"])

    # 6. All valid (missing invalid rows)
    all_val_src = _make_source_csv(tmp_path / "all_val", n_rows=100, all_valid=True)
    res_all_val = assess_source_suitability(all_val_src, case_def)
    assert res_all_val["suitable"] is False
    assert any("both included and excluded rows" in r for r in res_all_val["reasons"])

    # 7. All invalid (missing valid rows)
    all_inval_src = _make_source_csv(
        tmp_path / "all_inval", n_rows=100, all_invalid=True
    )
    res_all_inval = assess_source_suitability(all_inval_src, case_def)
    assert res_all_inval["suitable"] is False
    assert any("both included and excluded rows" in r for r in res_all_inval["reasons"])

    # 8. No blanks in valid rows
    no_blanks_src = _make_source_csv(tmp_path / "no_blanks", n_rows=100, no_blanks=True)
    res_no_blanks = assess_source_suitability(no_blanks_src, case_def)
    assert res_no_blanks["suitable"] is False
    assert any("answered and blank" in r for r in res_no_blanks["reasons"])

    # 9. All blanks in valid rows
    all_blanks_src = _make_source_csv(
        tmp_path / "all_blanks", n_rows=100, all_blanks=True
    )
    res_all_blanks = assess_source_suitability(all_blanks_src, case_def)
    assert res_all_blanks["suitable"] is False
    assert any("answered and blank" in r for r in res_all_blanks["reasons"])

    # 10. Licence disclosure via parameter or sibling
    res_lic = assess_source_suitability(src, case_def, licence_meta="CC BY 4.0")
    assert res_lic["licence_status"] == "disclosed: CC BY 4.0"

    sibling = src.with_name(src.stem + "-source.json")
    sibling.write_text(json.dumps({"licence": "CC-BY-4.0"}), encoding="utf-8")
    res_sib = assess_source_suitability(src, case_def)
    assert res_sib["licence_status"] == "disclosed: CC-BY-4.0"


def test_build_source_reference_no_raw_rows(tmp_path):
    src = _make_source_csv(tmp_path, n_rows=150)
    ref = build_source_reference(src)
    assert ref["version"] == 1
    assert ref["source_sha256"] is not None
    assert ref["source_rows"] == 150
    assert 0.74 <= ref["valid_fraction"] <= 0.76
    assert ref["median_forward_spacing"] == pytest.approx(0.0075, rel=1e-3)
    assert "CHLORITE" in ref["disclosed_swir_labels"]
    assert "AMPHIBOLE" in ref["disclosed_tir_labels"]
    assert ref["has_blanks"] is True
    # Verify no raw rows exist in the reference
    assert "rows" not in ref
    assert "samples" not in ref


def test_evaluate_case_utility_positive_controls(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    contract = compile_case_contract(case_def, 1000, src_sha)
    cand = _make_candidate_csv(tmp_path / "cand", n_rows=1000, spacing=0.0075)

    # 1. Evaluation using raw source path
    eval_raw = evaluate_case_utility(cand, src, contract)
    assert eval_raw["passed"] is True
    assert not eval_raw["failures"]
    assert eval_raw["measurements"]["row_count"] == 1000
    assert eval_raw["measurements"]["downstream_filter_group"]["included"] == 750

    # 2. Evaluation using bounded aggregate reference only (no raw source)
    ref = build_source_reference(src)
    eval_ref = evaluate_case_utility(cand, ref, contract)
    assert eval_ref["passed"] is True
    assert not eval_ref["failures"]
    assert eval_ref["candidate_sha256"] == eval_raw["candidate_sha256"]


def test_evaluate_case_utility_negative_failures(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    contract = compile_case_contract(case_def, 1000, src_sha)
    ref = build_source_reference(src)

    # Negative 1: wrong output columns
    cand_cols = _make_candidate_csv(
        tmp_path / "cols", n_rows=1000, output_columns=["a", "b", "c", "d"]
    )
    res_cols = evaluate_case_utility(cand_cols, ref, contract)
    assert res_cols["passed"] is False
    assert "output_columns" in res_cols["failures"]

    # Negative 2: wrong row count
    cand_rows = _make_candidate_csv(tmp_path / "rows", n_rows=500)
    res_rows = evaluate_case_utility(cand_rows, ref, contract)
    assert res_rows["passed"] is False
    assert "row_count" in res_rows["failures"]

    # Negative 3: nonbinary valid
    cand_nonbin = _make_candidate_csv(
        tmp_path / "nonbin", n_rows=1000, nonbinary_valid=True
    )
    res_nonbin = evaluate_case_utility(cand_nonbin, ref, contract)
    assert res_nonbin["passed"] is False
    assert "well_formed_rows" in res_nonbin["failures"]

    # Negative 4: invalid depth
    cand_inv_d = _make_candidate_csv(
        tmp_path / "inv_d", n_rows=1000, invalid_depth=True
    )
    res_inv_d = evaluate_case_utility(cand_inv_d, ref, contract)
    assert res_inv_d["passed"] is False
    assert "well_formed_rows" in res_inv_d["failures"]

    # Negative 5: unordered depth
    cand_unord = _make_candidate_csv(tmp_path / "unord", n_rows=100, unordered=True)
    c_unord = compile_case_contract(case_def, 100, src_sha)
    res_unord = evaluate_case_utility(cand_unord, ref, c_unord)
    assert res_unord["passed"] is False
    assert "ordered_depth" in res_unord["failures"]

    # Negative 6: spacing drift
    cand_drift = _make_candidate_csv(tmp_path / "drift", n_rows=1000, spacing=0.0100)
    res_drift = evaluate_case_utility(cand_drift, ref, contract)
    assert res_drift["passed"] is False
    assert "depth_spacing" in res_drift["failures"]

    # Negative 7: valid fraction drift beyond 0.02
    cand_vf = _make_candidate_csv(tmp_path / "vf", n_rows=1000, vf_shift=0.05)
    res_vf = evaluate_case_utility(cand_vf, ref, contract)
    assert res_vf["passed"] is False
    assert "inclusion_fraction" in res_vf["failures"]

    # Negative 8: answer rate drift beyond 0.02
    cand_swir = _make_candidate_csv(
        tmp_path / "swir", n_rows=1000, swir_rate_shift=-0.10
    )
    res_swir = evaluate_case_utility(cand_swir, ref, contract)
    assert res_swir["passed"] is False
    assert "swir_answer_rate" in res_swir["failures"]

    # Negative 9: unknown labels outside disclosed vocabulary
    cand_unk = _make_candidate_csv(tmp_path / "unk", n_rows=1000, unknown_labels=True)
    res_unk = evaluate_case_utility(cand_unk, ref, contract)
    assert res_unk["passed"] is False
    assert "swir_supported_labels" in res_unk["failures"]

    # Negative 10: structure collapse (all valid, no invalid rows)
    cand_all_val = _make_candidate_csv(
        tmp_path / "all_val", n_rows=1000, all_valid=True
    )
    res_all_val = evaluate_case_utility(cand_all_val, ref, contract)
    assert res_all_val["passed"] is False
    assert "downstream_structure_both_states_present" in res_all_val["failures"]

    # Negative 11: source pin mismatch
    contract_wrong_pin = compile_case_contract(case_def, 1000, "b" * 64)
    res_pin = evaluate_case_utility(
        _make_candidate_csv(tmp_path / "pin", n_rows=1000), ref, contract_wrong_pin
    )
    assert res_pin["passed"] is False
    assert "source_pin" in res_pin["failures"]


def test_verify_case_candidate_consumer_no_raw_source(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    contract = compile_case_contract(case_def, 1000, src_sha)
    ref = build_source_reference(src)

    cand = _make_candidate_csv(tmp_path / "cand", n_rows=1000, spacing=0.0075)
    eval_res = evaluate_case_utility(cand, ref, contract)
    assert eval_res["passed"] is True

    # 1. Matching reported result verified without raw source
    ver = verify_case_candidate(cand, ref, contract, reported_utility=eval_res)
    assert ver["case_verified"] is True
    assert ver["matches_reported"] is True

    # 2. Forged PASS report over failing data
    cand_drift = _make_candidate_csv(tmp_path / "drift", n_rows=1000, spacing=0.0100)
    forged_report = dict(eval_res)  # claims pass from cand
    ver_forged = verify_case_candidate(
        cand_drift, ref, contract, reported_utility=forged_report
    )
    assert ver_forged["case_verified"] is False
    assert ver_forged["matches_reported"] is False

    # 3. Tampered candidate sha256 in report
    tampered_sha_report = dict(eval_res)
    tampered_sha_report["candidate_sha256"] = "c" * 64
    ver_tampered = verify_case_candidate(
        cand, ref, contract, reported_utility=tampered_sha_report
    )
    assert ver_tampered["case_verified"] is False
    assert ver_tampered["matches_reported"] is False


def test_actual_candidate_bytes_roundtrip(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    contract = compile_case_contract(case_def, 1000, src_sha)
    ref = build_source_reference(src)

    cand_path = _make_candidate_csv(tmp_path / "cand", n_rows=1000, spacing=0.0075)
    raw_bytes = cand_path.read_bytes()

    # Pass raw bytes directly to evaluate_case_utility and verify_case_candidate
    eval_res = evaluate_case_utility(raw_bytes, ref, contract)
    assert eval_res["passed"] is True
    ver = verify_case_candidate(raw_bytes, ref, contract, reported_utility=eval_res)
    assert ver["case_verified"] is True
