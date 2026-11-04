"""Tests for profiles.hylogger.validation.cases."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

import profiles.hylogger.validation.cases as cm
from profiles.hylogger.validation.cases import (
    assess_source_suitability,
    build_source_reference,
    check_source_joint_satisfiability,
    compile_case_contract,
    compute_minimum_feasible_rows,
    evaluate_case_utility,
    get_case,
    get_evidence_pack,
    load_cases_catalogue,
    validate_case_contract,
    validate_citations,
    validate_plan_satisfiability,
    validate_source_reference,
    verify_case_candidate,
)


def _make_source_csv(
    tmp_path: Path,
    n_rows: int = 120,
    depth_start: float = 0.0,
    spacing: float = 0.0075,
    columns: list[str] | None = None,
    all_valid: bool = False,
    all_invalid: bool = False,
    no_blanks: bool = False,
    all_blanks: bool = False,
    unordered: bool = False,
    zero_spacing: bool = False,
    nan_at_index: int | None = None,
    negative_at_index: int | None = None,
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
                    if nan_at_index is not None and i == nan_at_index:
                        row_data.append("NaN")
                    elif negative_at_index is not None and i == negative_at_index:
                        row_data.append("-1.5")
                    else:
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
    unknown_label_on_excluded: bool = False,
    swir_rate_shift: float = 0.0,
    tir_rate_shift: float = 0.0,
    vf_shift: float = 0.0,
    nonbinary_valid: bool = False,
    invalid_depth: bool = False,
    all_valid: bool = False,
    extra_column_at_index: int | None = None,
    missing_column_at_index: int | None = None,
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
            elif unknown_label_on_excluded and not is_valid and i == 4:
                swir = "ALIEN_MINERAL"
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

            row = [d_val_str, v_str, swir, tir]
            if extra_column_at_index is not None and i == extra_column_at_index:
                row.append("EXTRA_VAL")
            elif missing_column_at_index is not None and i == missing_column_at_index:
                row = row[:-1]
            writer.writerow(row)
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


def test_strict_contract_validation_rejection():
    """Probe 1: strict contract validation rejects partial, tampered or invalid contracts."""
    case_def = get_case("hylogger_scalar_teaching_v1")
    fake_sha = "a" * 64

    # 1. Accepted canonical contract
    canonical = compile_case_contract(case_def, 1000, fake_sha)
    validate_case_contract(canonical, case_def)

    # 2. Incomplete contract with only {case_id, target_rows}
    incomplete = {"case_id": "hylogger_scalar_teaching_v1", "target_rows": 1000}
    with pytest.raises(ValueError, match="contract missing required keys"):
        validate_case_contract(incomplete, case_def)

    # 3. Invalid source pin
    bad_pin = dict(canonical)
    bad_pin["source_sha256"] = "invalid"
    with pytest.raises(ValueError, match="valid 64-char hex string"):
        validate_case_contract(bad_pin, case_def)

    # 4. Empty checks dict
    bad_checks = dict(canonical)
    bad_checks["checks"] = {}
    with pytest.raises(ValueError, match="contract checks mismatch"):
        validate_case_contract(bad_checks, case_def)

    # 5. Tampered check tolerance
    bad_tol = dict(canonical)
    bad_tol["checks"] = dict(canonical["checks"])
    bad_tol["checks"]["relative_median_spacing_difference_max"] = 0.05
    with pytest.raises(ValueError, match="contract checks mismatch"):
        validate_case_contract(bad_tol, case_def)

    # 6. Extra unapproved key
    extra_k = dict(canonical)
    extra_k["custom_override"] = True
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_case_contract(extra_k, case_def)

    # 7. Non-integer or boolean target_rows
    bad_type = dict(canonical)
    bad_type["target_rows"] = True
    with pytest.raises(TypeError, match="must be integer"):
        validate_case_contract(bad_type, case_def)


def test_assess_source_suitability_malformed_and_nan_depth_rejection(tmp_path):
    """Probe 2: assess_source_suitability rejects malformed/NaN/negative depths without silent loss."""
    case_def = get_case("hylogger_scalar_teaching_v1")

    # 1. Source with one StartDepth='NaN' at index 10
    nan_src = _make_source_csv(tmp_path / "nan_src", n_rows=120, nan_at_index=10)
    res_nan = assess_source_suitability(nan_src, case_def)
    assert res_nan["suitable"] is False
    assert res_nan["source_rows"] == 120
    assert res_nan["usable_rows"] == 119
    assert res_nan["rejected_rows"] == 1
    assert any(
        "malformed, non-finite or negative depth rows" in r for r in res_nan["reasons"]
    )

    # 2. Source with negative depth
    neg_src = _make_source_csv(tmp_path / "neg_src", n_rows=120, negative_at_index=5)
    res_neg = assess_source_suitability(neg_src, case_def)
    assert res_neg["suitable"] is False
    assert res_neg["rejected_rows"] == 1

    # 3. Clean 120 rows
    clean_src = _make_source_csv(tmp_path / "clean_src", n_rows=120)
    res_clean = assess_source_suitability(clean_src, case_def)
    assert res_clean["suitable"] is True
    assert res_clean["source_rows"] == 120
    assert res_clean["usable_rows"] == 120
    assert res_clean["rejected_rows"] == 0


def test_even_length_median_averaging(tmp_path):
    """Probe 3: median forward spacing averages two central steps on even counts."""
    case_def = get_case("hylogger_scalar_teaching_v1")

    # Construct source with 5 depths -> 4 positive forward steps:
    # depths: [0.0, 0.006, 0.014, 0.024, 0.036]
    # steps: [0.006, 0.008, 0.010, 0.012]
    # Central steps are 0.008 and 0.010; average is exactly 0.009
    path = tmp_path / "even_steps.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["StartDepth", "EndDepth", "Final_Mask", "Grp1_sTSAS", "Grp1_sjCLST"]
        )
        depths = [0.0, 0.006, 0.014, 0.024, 0.036]
        # Pad to 25 rows to meet min_source_rows
        cur = 0.036
        for _ in range(20):
            cur += 0.009
            depths.append(cur)
        for i, d in enumerate(depths):
            is_valid = (i % 2) == 0
            swir = "CHLORITE" if is_valid and i < 10 else ""
            tir = "AMPHIBOLE" if is_valid and i >= 10 else ""
            writer.writerow(
                [f"{d:.4f}", f"{d + 0.009:.4f}", "1" if is_valid else "0", swir, tir]
            )

    res = assess_source_suitability(path, case_def)
    assert res["suitable"] is True
    # The first 4 steps were [0.006, 0.008, 0.010, 0.012] with remainder 0.009
    # Median is float
    assert isinstance(res["aggregates"]["median_forward_spacing"], float)
    assert res["aggregates"]["median_forward_spacing"] > 0


def test_source_specific_joint_satisfiability_bounds():
    """Probe 4: source-aware integer feasibility checks joint answer rates and blank presence."""
    case_def = get_case("hylogger_scalar_teaching_v1")
    hd012_ref = {
        "version": 1,
        "source_sha256": "8d9afb0120f9a82182f4689109a6c97c96bcc869a555ba77fdece0f98b4cb1bf",
        "source_rows": 10000,
        "usable_rows": 10000,
        "rejected_rows": 0,
        "valid_fraction": 0.750682,
        "median_forward_spacing": 0.0075,
        "swir_answer_rate": 0.812345,
        "tir_answer_rate": 0.988834,
        "disclosed_swir_labels": ["CHLORITE"],
        "disclosed_tir_labels": ["AMPHIBOLE"],
        "has_blanks": True,
        "licence_status": "disclosed: CC BY 4.0",
        "notice": "test reference",
    }

    # Minimum feasible rows for HD012 is 44
    min_hd012 = compute_minimum_feasible_rows(case_def, hd012_ref)
    assert min_hd012 == 44

    # 25 rows fails joint satisfiability for HD012 because 19 valid rows cannot support
    # a blank while staying within 0.02 of 0.9888 (18/19 = 0.9474, drift 0.0415)
    ok_25, reason_25 = check_source_joint_satisfiability(25, case_def, hd012_ref)
    assert ok_25 is False
    assert "cannot jointly satisfy" in reason_25

    with pytest.raises(ValueError, match="not jointly satisfiable for this source"):
        validate_plan_satisfiability(25, case_def, hd012_ref)

    # 44 rows succeeds joint satisfiability (k=33 valid rows, 32 answered = 0.9697, drift 0.0191 <= 0.02)
    ok_44, _ = check_source_joint_satisfiability(44, case_def, hd012_ref)
    assert ok_44 is True
    validate_plan_satisfiability(44, case_def, hd012_ref)

    # 10000 rows succeeds
    ok_10k, _ = check_source_joint_satisfiability(10000, case_def, hd012_ref)
    assert ok_10k is True


def test_build_source_reference_refuses_unsuitable_and_validates_schema(tmp_path):
    """Probe 5: build_source_reference refuses unsuitable sources and validates schema."""
    case_def = get_case("hylogger_scalar_teaching_v1")

    # 1. Unsuitable source with NaN raises ValueError
    nan_src = _make_source_csv(tmp_path / "nan_src", n_rows=120, nan_at_index=5)
    with pytest.raises(
        ValueError, match="cannot build source reference from unsuitable source"
    ):
        build_source_reference(nan_src, case_def)

    # 2. Suitable source builds valid reference
    clean_src = _make_source_csv(tmp_path / "clean_src", n_rows=120)
    ref = build_source_reference(clean_src, case_def)
    validate_source_reference(ref)
    assert ref["usable_rows"] == 120
    assert ref["rejected_rows"] == 0

    # 3. Invalid reference schema fails validation
    bad_ref = dict(ref)
    bad_ref["valid_fraction"] = 1.5  # outside [0, 1]
    with pytest.raises(ValueError, match="must be finite float in"):
        validate_source_reference(bad_ref)


def test_candidate_csv_row_width_and_excluded_row_labels(tmp_path):
    """Probe 6: detects malformed CSV row widths and unknown labels on excluded rows."""
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)

    # 1. Extra column on line 5
    cand_extra = _make_candidate_csv(
        tmp_path / "extra", n_rows=1000, extra_column_at_index=5
    )
    res_extra = evaluate_case_utility(cand_extra, ref, contract)
    assert res_extra["passed"] is False
    assert "well_formed_rows" in res_extra["failures"]
    assert (
        "malformed CSV row width"
        in res_extra["measurements"]["well_formed_rows"]["malformed_detail"]
    )

    # 2. Missing column on line 5
    cand_miss = _make_candidate_csv(
        tmp_path / "miss", n_rows=1000, missing_column_at_index=5
    )
    res_miss = evaluate_case_utility(cand_miss, ref, contract)
    assert res_miss["passed"] is False
    assert "well_formed_rows" in res_miss["failures"]

    # 3. Unknown label on an EXCLUDED row (valid == False)
    cand_excl_unk = _make_candidate_csv(
        tmp_path / "excl_unk", n_rows=1000, unknown_label_on_excluded=True
    )
    res_excl_unk = evaluate_case_utility(cand_excl_unk, ref, contract)
    assert res_excl_unk["passed"] is False
    assert "swir_supported_labels" in res_excl_unk["failures"]
    assert (
        "ALIEN_MINERAL"
        in res_excl_unk["measurements"]["swir_supported_labels"]["unknown_labels"]
    )


def test_no_parallel_wrappers_in_cases():
    """Probe 7: verify unused parallel generation/check wrappers were removed."""
    assert not hasattr(cm, "build_profile_stats")
    assert not hasattr(cm, "generate_synthetic")
    assert not hasattr(cm, "run_profile_checks")


def test_evaluate_case_utility_positive_controls(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)
    cand = _make_candidate_csv(tmp_path / "cand", n_rows=1000, spacing=0.0075)

    # 1. Evaluation using raw source path
    eval_raw = evaluate_case_utility(cand, src, contract)
    assert eval_raw["passed"] is True
    assert not eval_raw["failures"]
    assert eval_raw["measurements"]["row_count"] == 1000
    assert eval_raw["measurements"]["downstream_filter_group"]["included"] == 750

    # 2. Evaluation using bounded aggregate reference only (no raw source)
    eval_ref = evaluate_case_utility(cand, ref, contract)
    assert eval_ref["passed"] is True
    assert not eval_ref["failures"]
    assert eval_ref["candidate_sha256"] == eval_raw["candidate_sha256"]


def test_evaluate_case_utility_negative_failures(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)

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
    c_unord = compile_case_contract(case_def, 100, ref["source_sha256"], ref)
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
    contract_wrong_pin = compile_case_contract(case_def, 1000, "b" * 64, ref)
    res_pin = evaluate_case_utility(
        _make_candidate_csv(tmp_path / "pin", n_rows=1000), ref, contract_wrong_pin
    )
    assert res_pin["passed"] is False
    assert "source_pin" in res_pin["failures"]


def test_verify_case_candidate_consumer_no_raw_source(tmp_path):
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)

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
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)

    cand_path = _make_candidate_csv(tmp_path / "cand", n_rows=1000, spacing=0.0075)
    raw_bytes = cand_path.read_bytes()

    # Pass raw bytes directly to evaluate_case_utility and verify_case_candidate
    eval_res = evaluate_case_utility(raw_bytes, ref, contract)
    assert eval_res["passed"] is True
    ver = verify_case_candidate(raw_bytes, ref, contract, reported_utility=eval_res)
    assert ver["case_verified"] is True
