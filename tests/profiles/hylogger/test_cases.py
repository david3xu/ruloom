"""Tests for profiles.hylogger.validation.cases."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

import profiles.hylogger.validation.cases as cm
from profiles.hylogger.validation.cases import (
    assess_source_suitability,
    build_disclosed_model_summary,
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
    invalid_mask_at_index: int | None = None,
    missing_cell_at_index: int | None = None,
    extra_cell_at_index: int | None = None,
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
                    if invalid_mask_at_index is not None and i == invalid_mask_at_index:
                        row_data.append("maybe")
                    else:
                        row_data.append("1" if is_valid else "0")
                elif c == "Grp1_sTSAS":
                    row_data.append(swir)
                elif c == "Grp1_sjCLST":
                    row_data.append(tir)
                else:
                    row_data.append("x")
            if missing_cell_at_index is not None and i == missing_cell_at_index:
                row_data = row_data[:-1]
            elif extra_cell_at_index is not None and i == extra_cell_at_index:
                row_data.append("EXTRA")
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
    padded_depth: bool = False,
    underscore_depth: bool = False,
    padded_valid: bool = False,
    uppercase_valid: bool = False,
    all_valid: bool = False,
    extra_column_at_index: int | None = None,
    missing_column_at_index: int | None = None,
    no_tir_blanks: bool = False,
    no_swir_blanks: bool = False,
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
                if no_swir_blanks:
                    swir = "CHLORITE"
                elif swir_rate_shift < 0:
                    swir = "CHLORITE" if ((i % 5 != 0) and (i % 3 != 0)) else ""
                else:
                    swir = "CHLORITE" if (i % 5 != 0) else ""
            elif unknown_label_on_excluded and not is_valid and i == 4:
                swir = "ALIEN_MINERAL"
            else:
                swir = ""

            if is_valid:
                if no_tir_blanks:
                    tir = "AMPHIBOLE"
                elif tir_rate_shift < 0:
                    tir = "AMPHIBOLE" if ((i % 6 != 0) and (i % 3 != 0)) else ""
                else:
                    tir = "AMPHIBOLE" if (i % 6 != 0) else ""
            else:
                tir = ""

            d_val = (depth - 0.01) if (unordered and i == 10) else depth
            if invalid_depth and i == 5:
                d_val_str = "-1.0"
            elif padded_depth and i == 5:
                d_val_str = f" {d_val:.4f} "
            elif underscore_depth and i == 5:
                d_val_str = "10_0.0"
            else:
                d_val_str = f"{d_val:.4f}"

            if nonbinary_valid and i == 3:
                v_str = "MAYBE"
            elif padded_valid and i == 3:
                v_str = " true " if is_valid else " false "
            elif uppercase_valid and i == 3:
                v_str = "TRUE" if is_valid else "FALSE"
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
    assert cat["version"] == 1
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
        assert item["url"].startswith("https://")

    case = cat["cases"]["hylogger_scalar_teaching_v1"]
    assert case["id"] == "hylogger_scalar_teaching_v1"
    assert case["version"] == 1
    assert "researched_facts" in case
    assert "source_licence_evidence" in case
    assert "design_choices" in case
    assert case["design_choices"]["min_satisfiable_rows"] == 25
    assert case["design_choices"]["max_supported_rows"] == 200000

    # Assert equality between design_choices tolerances and contract_template checks
    dc = case["design_choices"]
    checks = case["contract_template"]["checks"]
    assert (
        dc["spacing_tolerance_relative_max"]
        == checks["relative_median_spacing_difference_max"]
    )
    assert (
        dc["valid_fraction_tolerance_absolute_max"]
        == checks["absolute_valid_fraction_difference_max"]
    )
    assert (
        dc["answer_rate_tolerance_absolute_max"]
        == checks["absolute_answer_rate_difference_max"]
    )


def test_catalogue_strict_schema_mutations_rejection(monkeypatch):
    """F4: each catalogue mutation is refused with a validation error and never silently defaulted."""
    cat = cm.load_cases_catalogue()

    class FakePath:
        def __init__(self, data):
            self.data = data

        def is_file(self):
            return True

        def read_text(self, encoding="utf-8"):
            return json.dumps(self.data)

    def _assert_catalogue_raises(mutated, match):
        monkeypatch.setattr(cm, "_cases_json_path", lambda: FakePath(mutated))
        with pytest.raises(ValueError, match=match):
            cm.load_cases_catalogue()

    # 1. Top-level version missing, 99 or 'x'
    m = json.loads(json.dumps(cat))
    del m["version"]
    _assert_catalogue_raises(m, "missing required top-level keys")

    m = json.loads(json.dumps(cat))
    m["version"] = 99
    _assert_catalogue_raises(m, "cases catalogue version must be integer 1")

    m = json.loads(json.dumps(cat))
    m["version"] = "x"
    _assert_catalogue_raises(m, "cases catalogue version must be integer 1")

    # 2. Evidence key different from its entry id
    m = json.loads(json.dumps(cat))
    m["evidence_pack"]["diff_key"] = dict(m["evidence_pack"]["auscope_nvcl"])
    _assert_catalogue_raises(m, "evidence key 'diff_key' differs from entry id")

    # 3. http://evil.example URL
    m = json.loads(json.dumps(cat))
    m["evidence_pack"]["auscope_nvcl"]["url"] = "http://evil.example/data"
    _assert_catalogue_raises(m, "URL must use secure https protocol")

    # 4. Non-URL text as URL
    m = json.loads(json.dumps(cat))
    m["evidence_pack"]["auscope_nvcl"]["url"] = "not-a-url"
    _assert_catalogue_raises(m, "URL must use secure https protocol")

    # 5. Date 'yesterday'
    m = json.loads(json.dumps(cat))
    m["evidence_pack"]["auscope_nvcl"]["retrieved_date"] = "yesterday"
    _assert_catalogue_raises(m, "date must be ISO format")

    # 6. Review date before retrieval date
    m = json.loads(json.dumps(cat))
    m["evidence_pack"]["auscope_nvcl"]["retrieved_date"] = "2026-11-04"
    m["evidence_pack"]["auscope_nvcl"]["reviewed_date"] = "2026-11-01"
    _assert_catalogue_raises(m, "reviewed_date .* cannot be before retrieved_date")

    # 7. does_not_establish as string
    m = json.loads(json.dumps(cat))
    m["evidence_pack"]["auscope_nvcl"]["does_not_establish"] = "not a list"
    _assert_catalogue_raises(m, "does_not_establish must be non-empty list")

    # 8. Reviewer containing 'gemini'
    for rev_name in ("gemini", "Gemini Flash (AGY)"):
        m = json.loads(json.dumps(cat))
        m["evidence_pack"]["auscope_nvcl"]["admission_reviewer"] = rev_name
        _assert_catalogue_raises(m, "reviewer cannot be implementer")

    # 9. Case key different from id
    m = json.loads(json.dumps(cat))
    m["cases"]["other_case_key"] = dict(m["cases"]["hylogger_scalar_teaching_v1"])
    _assert_catalogue_raises(m, "case key 'other_case_key' differs from case id")

    # 10. Non-integer case version
    m = json.loads(json.dumps(cat))
    m["cases"]["hylogger_scalar_teaching_v1"]["version"] = 1.0
    _assert_catalogue_raises(
        m, "case 'hylogger_scalar_teaching_v1' version must be integer 1"
    )

    # 11. Empty primary_evidence_ids
    m = json.loads(json.dumps(cat))
    m["cases"]["hylogger_scalar_teaching_v1"]["researched_facts"][
        "primary_evidence_ids"
    ] = []
    _assert_catalogue_raises(m, "primary_evidence_ids must be non-empty list")

    # 12. Non-string verbatim_passage raises ValueError (not AttributeError)
    m = json.loads(json.dumps(cat))
    m["evidence_pack"]["auscope_nvcl"]["verbatim_passage"] = 12345
    _assert_catalogue_raises(m, "verbatim_passage must be non-empty string")

    # 13. Removal of design_choices tolerance keys
    m = json.loads(json.dumps(cat))
    del m["cases"]["hylogger_scalar_teaching_v1"]["design_choices"][
        "valid_fraction_tolerance_absolute_max"
    ]
    _assert_catalogue_raises(m, "design_choices schema mismatch")

    # 14. Loosening template valid-fraction threshold
    m = json.loads(json.dumps(cat))
    m["cases"]["hylogger_scalar_teaching_v1"]["contract_template"]["checks"][
        "absolute_valid_fraction_difference_max"
    ] = 0.5
    _assert_catalogue_raises(m, "loosened or wrong type")


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


def test_strict_contract_validation_aliases_and_types():
    """F2: type aliases in strict contract validation are refused."""
    case_def = get_case("hylogger_scalar_teaching_v1")
    fake_sha = "a" * 64
    canonical = compile_case_contract(case_def, 1000, fake_sha)
    validate_case_contract(canonical, case_def)

    # 1. version = 1.0 (float)
    bad_ver = dict(canonical)
    bad_ver["version"] = 1.0
    with pytest.raises(ValueError, match="contract version must be integer 1"):
        validate_case_contract(bad_ver, case_def)

    # 2. valid_binary = 1 (int instead of bool)
    bad_bin = dict(canonical)
    bad_bin["checks"] = dict(canonical["checks"])
    bad_bin["checks"]["valid_binary"] = 1
    with pytest.raises(ValueError, match="type/value mismatch"):
        validate_case_contract(bad_bin, case_def)

    # 3. exact_rows = 10000.0 (float instead of int)
    bad_rows = dict(canonical)
    bad_rows["checks"] = dict(canonical["checks"])
    bad_rows["checks"]["exact_rows"] = 1000.0
    with pytest.raises(ValueError, match="type/value mismatch"):
        validate_case_contract(bad_rows, case_def)

    # 4. Uppercase source_sha256
    bad_sha = dict(canonical)
    bad_sha["source_sha256"] = "A" * 64
    with pytest.raises(ValueError, match="valid 64-char lowercase hex string"):
        validate_case_contract(bad_sha, case_def)


def test_source_reference_strict_schema_aliases_and_consistency():
    """F2 & F6: type aliases and internal consistency in validate_source_reference."""
    clean_ref = {
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
        "notice": "code-side reference",
    }
    validate_source_reference(clean_ref)

    # 1. version 1.0 (float)
    bad = dict(clean_ref)
    bad["version"] = 1.0
    with pytest.raises(ValueError, match="source reference version must be integer 1"):
        validate_source_reference(bad)

    # 2. uppercase sha256
    bad = dict(clean_ref)
    bad["source_sha256"] = clean_ref["source_sha256"].upper()
    with pytest.raises(ValueError, match="64-char lowercase hex"):
        validate_source_reference(bad)

    # 3. usable_rows > source_rows
    bad = dict(clean_ref)
    bad["usable_rows"] = 10001
    with pytest.raises(
        ValueError, match="usable_rows must be positive integer <= source_rows"
    ):
        validate_source_reference(bad)

    # 4. rejected_rows = 'hidden' or negative
    bad = dict(clean_ref)
    bad["rejected_rows"] = "hidden"
    with pytest.raises(ValueError, match="rejected_rows must be non-negative integer"):
        validate_source_reference(bad)

    bad = dict(clean_ref)
    bad["rejected_rows"] = -1
    with pytest.raises(ValueError, match="rejected_rows must be non-negative integer"):
        validate_source_reference(bad)

    # 5. row accounting mismatch: usable + rejected != source_rows
    bad = dict(clean_ref)
    bad["usable_rows"] = 9000
    bad["rejected_rows"] = 500  # sum = 9500 != 10000
    with pytest.raises(ValueError, match="row accounting mismatch"):
        validate_source_reference(bad)

    # 6. valid_fraction 0.0 or 1.0
    bad = dict(clean_ref)
    bad["valid_fraction"] = 0.0
    with pytest.raises(ValueError, match="valid_fraction must be finite float in"):
        validate_source_reference(bad)

    bad = dict(clean_ref)
    bad["valid_fraction"] = 1.0
    with pytest.raises(ValueError, match="valid_fraction must be finite float in"):
        validate_source_reference(bad)

    # 7. duplicate labels
    bad = dict(clean_ref)
    bad["disclosed_swir_labels"] = ["CHLORITE", "CHLORITE"]
    with pytest.raises(ValueError, match="disclosed_swir_labels contains duplicates"):
        validate_source_reference(bad)

    # 8. empty string in labels
    bad = dict(clean_ref)
    bad["disclosed_tir_labels"] = [""]
    with pytest.raises(
        ValueError, match="disclosed_tir_labels must be list of non-empty strings"
    ):
        validate_source_reference(bad)

    # 9. empty labels when rate > 0
    bad = dict(clean_ref)
    bad["disclosed_swir_labels"] = []
    with pytest.raises(ValueError, match="disclosed_swir_labels cannot be empty"):
        validate_source_reference(bad)

    # 10. has_blanks = False when rates < 1.0
    bad = dict(clean_ref)
    bad["has_blanks"] = False
    with pytest.raises(
        ValueError, match="has_blanks must be True when answer rates < 1.0"
    ):
        validate_source_reference(bad)

    # 11. non-string licence_status or notice
    bad = dict(clean_ref)
    bad["licence_status"] = ""
    with pytest.raises(ValueError, match="licence_status must be non-empty string"):
        validate_source_reference(bad)


def test_assess_source_suitability_loss_and_formatting(tmp_path):
    """F7: source suitability detects invalid mask, row width errors, and accepts list of tuples."""
    case_def = get_case("hylogger_scalar_teaching_v1")

    # 1. Final_Mask='maybe' is counted as rejected, suitable=False
    mask_src = _make_source_csv(
        tmp_path / "mask_src", n_rows=120, invalid_mask_at_index=5
    )
    res_mask = assess_source_suitability(mask_src, case_def)
    assert res_mask["suitable"] is False
    assert res_mask["rejected_rows"] == 1
    assert any("invalid mask rows" in r for r in res_mask["reasons"])

    # 2. Missing cell in CSV line
    miss_cell_src = _make_source_csv(
        tmp_path / "miss_cell_src", n_rows=120, missing_cell_at_index=10
    )
    res_miss = assess_source_suitability(miss_cell_src, case_def)
    assert res_miss["suitable"] is False
    assert res_miss["rejected_rows"] == 1

    # 3. Extra cell in CSV line
    extra_cell_src = _make_source_csv(
        tmp_path / "extra_cell_src", n_rows=120, extra_cell_at_index=10
    )
    res_extra = assess_source_suitability(extra_cell_src, case_def)
    assert res_extra["suitable"] is False
    assert res_extra["rejected_rows"] == 1

    # 4. List of tuples does not raise AttributeError and produces source_sha256
    tuple_rows = []
    d = 0.0
    for i in range(120):
        is_v = "1" if (i % 4 != 0) else "0"
        sw = "CHLORITE" if is_v == "1" and i % 5 != 0 else ""
        ti = "AMPHIBOLE" if is_v == "1" and i % 6 != 0 else ""
        tuple_rows.append((d, is_v, sw, ti))
        d += 0.0075

    res_tuples = assess_source_suitability(tuple_rows, case_def)
    assert res_tuples["suitable"] is True
    assert res_tuples["source_rows"] == 120
    assert res_tuples["usable_rows"] == 120
    assert res_tuples["rejected_rows"] == 0
    assert type(res_tuples["source_sha256"]) is str
    assert len(res_tuples["source_sha256"]) == 64


def test_even_length_median_averaging(tmp_path):
    """Probe 3: median forward spacing averages two central steps on even counts."""
    case_def = get_case("hylogger_scalar_teaching_v1")

    path = tmp_path / "even_steps.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["StartDepth", "EndDepth", "Final_Mask", "Grp1_sTSAS", "Grp1_sjCLST"]
        )
        depths = [0.0, 0.006, 0.014, 0.024, 0.036]
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
    assert isinstance(res["aggregates"]["median_forward_spacing"], float)
    assert res["aggregates"]["median_forward_spacing"] > 0


def test_source_specific_joint_satisfiability_and_edge_cases():
    """F5: interval arithmetic feasibility with k=1, k=2, and impossible bounds."""
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

    # Minimum feasible rows for HD012 is exactly 44
    min_hd012 = compute_minimum_feasible_rows(case_def, hd012_ref)
    assert min_hd012 == 44

    # 25 rows fails joint satisfiability
    ok_25, reason_25 = check_source_joint_satisfiability(25, case_def, hd012_ref)
    assert ok_25 is False
    assert "cannot jointly satisfy" in reason_25

    with pytest.raises(ValueError, match="not jointly satisfiable for this source"):
        validate_plan_satisfiability(25, case_def, hd012_ref)

    validate_plan_satisfiability(44, case_def, hd012_ref)
    validate_plan_satisfiability(10000, case_def, hd012_ref)

    # k=1 edge case: target_rows giving k=1 with rates < 1.0 must fail
    ref_small = dict(hd012_ref)
    ref_small["valid_fraction"] = 0.0004
    ok_k1, reason_k1 = check_source_joint_satisfiability(2500, case_def, ref_small)
    assert ok_k1 is False
    assert "needs k >= 2" in reason_k1

    # Impossible target within bounds raises ValueError and does not return floor 25
    ref_impossible = dict(hd012_ref)
    ref_impossible["valid_fraction"] = 0.000001
    with pytest.raises(
        ValueError, match="no jointly feasible row count exists within supported bounds"
    ):
        compute_minimum_feasible_rows(case_def, ref_impossible)


def test_per_channel_blank_presence_in_downstream_structure(tmp_path):
    """F3: removing all TIR blanks or all SWIR blanks fails with per-channel counts."""
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)

    # 1. No TIR blanks fails
    cand_no_tir = _make_candidate_csv(
        tmp_path / "no_tir", n_rows=1000, no_tir_blanks=True
    )
    res_tir = evaluate_case_utility(cand_no_tir, ref, contract)
    assert res_tir["passed"] is False
    assert "downstream_structure_both_states_present" in res_tir["failures"]
    assert any(
        "TIR missing required blank labels" in d
        for d in res_tir["measurements"]["downstream_structure_both_states_present"][
            "structure_detail"
        ]
    )

    # 2. No SWIR blanks fails
    cand_no_swir = _make_candidate_csv(
        tmp_path / "no_swir", n_rows=1000, no_swir_blanks=True
    )
    res_swir = evaluate_case_utility(cand_no_swir, ref, contract)
    assert res_swir["passed"] is False
    assert "downstream_structure_both_states_present" in res_swir["failures"]
    assert any(
        "SWIR missing required blank labels" in d
        for d in res_swir["measurements"]["downstream_structure_both_states_present"][
            "structure_detail"
        ]
    )


def test_verify_case_candidate_comprehensive_checks(tmp_path):
    """F1: verify_case_candidate canonically compares all fields, rejecting forged/missing records."""
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)

    cand = _make_candidate_csv(tmp_path / "cand", n_rows=1000, spacing=0.0075)
    eval_res = evaluate_case_utility(cand, ref, contract)
    assert eval_res["passed"] is True

    # 1. Matching reported utility succeeds
    ver = verify_case_candidate(cand, ref, contract, reported_utility=eval_res)
    assert ver["case_verified"] is True
    assert ver["matches_reported"] is True

    # 2. Missing report returns not_compared and case_verified=False
    ver_none = verify_case_candidate(cand, ref, contract, reported_utility=None)
    assert ver_none["case_verified"] is False
    assert ver_none["matches_reported"] == "not_compared"

    # 3. Non-mapping report
    ver_str = verify_case_candidate(cand, ref, contract, reported_utility="string")
    assert ver_str["case_verified"] is False
    assert ver_str["matches_reported"] is False

    # 4. Extra top-level key
    extra_top = dict(eval_res)
    extra_top["extra_unapproved"] = 123
    ver_extra = verify_case_candidate(cand, ref, contract, reported_utility=extra_top)
    assert ver_extra["case_verified"] is False
    assert ver_extra["matches_reported"] is False

    # 5. Missing top-level key (e.g. limitations)
    miss_top = dict(eval_res)
    del miss_top["limitations"]
    ver_miss = verify_case_candidate(cand, ref, contract, reported_utility=miss_top)
    assert ver_miss["case_verified"] is False
    assert ver_miss["matches_reported"] is False

    # 6. Altered limitations
    bad_limits = dict(eval_res)
    bad_limits["limitations"] = ["Altered limitation claim"]
    ver_limits = verify_case_candidate(cand, ref, contract, reported_utility=bad_limits)
    assert ver_limits["case_verified"] is False
    assert ver_limits["matches_reported"] is False

    # 7. Empty measurements {}
    empty_meas = dict(eval_res)
    empty_meas["measurements"] = {}
    ver_empty = verify_case_candidate(cand, ref, contract, reported_utility=empty_meas)
    assert ver_empty["case_verified"] is False
    assert ver_empty["matches_reported"] is False

    # 8. Altered measurement value
    altered_val = json.loads(json.dumps(eval_res))
    altered_val["measurements"]["inclusion_fraction"]["candidate"] = 0.50
    ver_val = verify_case_candidate(cand, ref, contract, reported_utility=altered_val)
    assert ver_val["case_verified"] is False
    assert ver_val["matches_reported"] is False


def test_candidate_parsing_robustness_and_refusal(tmp_path):
    """F8: candidate parsing handles UTF-8 decode errors, non-mapping contracts, and padded/underscore tokens."""
    case_def = get_case("hylogger_scalar_teaching_v1")
    src = _make_source_csv(tmp_path / "src", n_rows=1000, spacing=0.0075)
    ref = build_source_reference(src)
    contract = compile_case_contract(case_def, 1000, ref["source_sha256"], ref)

    # 1. Invalid UTF-8 bytes returns reported failure instead of crashing
    res_utf8 = evaluate_case_utility(b"\xff\xfe\x00\x00", ref, contract)
    assert res_utf8["passed"] is False
    assert "invalid_utf8_encoding" in res_utf8["failures"]

    # 2. Non-mapping contract raises ValueError consistently
    with pytest.raises(ValueError, match="contract must be a mapping"):
        evaluate_case_utility(b"depth_m,valid,swir_group,tir_group\n", ref, "not_a_map")

    # 3. Unknown case_id in contract raises ValueError consistently
    with pytest.raises(ValueError, match="unknown case_id in contract"):
        evaluate_case_utility(
            b"depth_m,valid,swir_group,tir_group\n", ref, {"case_id": "unknown_case"}
        )

    # 4. Padded depth cell is rejected as malformed
    cand_pad_d = _make_candidate_csv(tmp_path / "pad_d", n_rows=1000, padded_depth=True)
    res_pad_d = evaluate_case_utility(cand_pad_d, ref, contract)
    assert res_pad_d["passed"] is False
    assert "well_formed_rows" in res_pad_d["failures"]

    # 5. Depth with underscore is rejected
    cand_und_d = _make_candidate_csv(
        tmp_path / "und_d", n_rows=1000, underscore_depth=True
    )
    res_und_d = evaluate_case_utility(cand_und_d, ref, contract)
    assert res_und_d["passed"] is False
    assert "well_formed_rows" in res_und_d["failures"]

    # 6. Padded valid flag is rejected
    cand_pad_v = _make_candidate_csv(tmp_path / "pad_v", n_rows=1000, padded_valid=True)
    res_pad_v = evaluate_case_utility(cand_pad_v, ref, contract)
    assert res_pad_v["passed"] is False
    assert "well_formed_rows" in res_pad_v["failures"]

    # 7. Uppercase valid flag is rejected
    cand_up_v = _make_candidate_csv(
        tmp_path / "up_v", n_rows=1000, uppercase_valid=True
    )
    res_up_v = evaluate_case_utility(cand_up_v, ref, contract)
    assert res_up_v["passed"] is False
    assert "well_formed_rows" in res_up_v["failures"]


def test_build_disclosed_model_summary(tmp_path):
    """Separates code-side reference from model-facing summary."""
    src = _make_source_csv(tmp_path, n_rows=120)
    ref = build_source_reference(src)
    summary = build_disclosed_model_summary(ref)
    assert summary["version"] == 1
    assert "disclosed_swir_labels" in summary
    assert "disclosed_tir_labels" in summary
    assert "Model-facing disclosed summary" in summary["notice"]


def test_no_parallel_wrappers_in_cases():
    """Probe 7: verify unused parallel generation/check wrappers were removed."""
    assert not hasattr(cm, "build_profile_stats")
    assert not hasattr(cm, "generate_synthetic")
    assert not hasattr(cm, "run_profile_checks")
    assert not hasattr(cm, "compute_expected_valid_count")


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

    # Negative 11: source pin mismatch refused early by strict contract validation
    contract_wrong_pin = compile_case_contract(case_def, 1000, "b" * 64)
    with pytest.raises(ValueError, match="source_sha256 mismatch with reference"):
        evaluate_case_utility(
            _make_candidate_csv(tmp_path / "pin", n_rows=1000), ref, contract_wrong_pin
        )
