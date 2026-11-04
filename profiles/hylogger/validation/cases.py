"""Admitted case records, provenance validation and practical-use checking.

This module provides profile-owned case logic for the research-led workflow:
- validates installed case definitions and reviewed local evidence pack;
- verifies tamper-evident snapshot hashes, primary URLs and admission reviews;
- compiles and strictly validates typed case contracts with bounded target rows
  and source pins;
- enforces deterministic source suitability pre-flight before any model call;
- builds bounded source aggregate references without shipping raw source rows;
- enforces downstream practical-use checking (read/filter/group, row accounting,
  structure presence, and fixed lesson tolerances from agreement);
- provides independent consumer recomputation for recipient verification.

This is a profile foundation for the research-led workflow, not a runtime CLI
or publication integration (which bind into execute and package in subsequent slices).
This module imports no engine and no model code (FP-CODE-2).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..intake.reader import normalize_group, parse_depth, parse_valid

ALLOWED_TOP_KEYS = frozenset({"version", "evidence_pack", "cases"})

ALLOWED_EVIDENCE_KEYS = frozenset(
    {
        "id",
        "url",
        "title",
        "section",
        "verbatim_passage",
        "supports",
        "does_not_establish",
        "retrieved_date",
        "reviewed_date",
        "admission_reviewer",
        "admission_kind",
        "passage_sha256",
    }
)

ALLOWED_CASE_KEYS = frozenset(
    {
        "id",
        "version",
        "title",
        "aliases",
        "purpose_description",
        "researched_facts",
        "source_licence_evidence",
        "design_choices",
        "source_requirements",
        "contract_template",
        "limitations",
    }
)

ALLOWED_CONTRACT_KEYS = frozenset(
    {
        "version",
        "case_id",
        "target_rows",
        "source_sha256",
        "output_columns",
        "recommended_strategy",
        "checks",
        "denominators",
        "flag_encoding",
        "spacing_definition",
        "limitations",
    }
)

ALLOWED_REFERENCE_KEYS = frozenset(
    {
        "version",
        "source_sha256",
        "source_rows",
        "usable_rows",
        "rejected_rows",
        "valid_fraction",
        "median_forward_spacing",
        "swir_answer_rate",
        "tir_answer_rate",
        "disclosed_swir_labels",
        "disclosed_tir_labels",
        "has_blanks",
        "licence_status",
        "notice",
    }
)


def compute_expected_valid_count(stats_profile: Any, plan: Any) -> int | None:
    """Compute exact expected valid count for balanced strategy, or None."""
    if isinstance(plan, dict) and plan.get("strategy") == "fitted_markov_balanced":
        stats = getattr(stats_profile, "stats", None)
        validity = stats.get("validity") if isinstance(stats, dict) else None
        vf = validity.get("valid_fraction") if isinstance(validity, dict) else None
        n_rows = plan.get("n_rows")
        if (
            isinstance(n_rows, int)
            and not isinstance(n_rows, bool)
            and isinstance(vf, (int, float))
            and not isinstance(vf, bool)
        ):
            return round(n_rows * vf)
    return None


def _cases_json_path() -> Path:
    """Path to the sibling cases.json catalogue file."""
    return Path(__file__).resolve().parent.parent / "cases.json"


def load_cases_catalogue() -> dict[str, Any]:
    """Load and validate the profile's admitted cases catalogue.

    Enforces code-checkable provenance for every evidence entry:
    - strict schema with no unexpected top-level or entry keys;
    - required fields: id, url, title, section, verbatim_passage,
      passage_sha256, supports, does_not_establish, retrieved_date,
      reviewed_date, admission_reviewer, admission_kind;
    - passage_sha256 must match hashlib.sha256 of verbatim_passage;
    - admission_reviewer must be recorded and cannot be implementer;
    - cases must separate researched_facts, source_licence_evidence and
      design_choices;
    - primary evidence citations must exist in evidence_pack.
    """
    path = _cases_json_path()
    if not path.is_file():
        raise FileNotFoundError(f"cases catalogue not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("cases catalogue must be a JSON object")

    extra_top = set(raw.keys()) - ALLOWED_TOP_KEYS
    if extra_top:
        raise ValueError(
            f"cases catalogue has unexpected top-level keys: {sorted(extra_top)}"
        )

    evidence_pack = raw.get("evidence_pack")
    if not isinstance(evidence_pack, dict) or not evidence_pack:
        raise ValueError("cases catalogue missing non-empty evidence_pack")

    for eid, entry in evidence_pack.items():
        if not isinstance(entry, dict):
            raise ValueError(f"evidence entry {eid!r} must be an object")
        extra_e = set(entry.keys()) - ALLOWED_EVIDENCE_KEYS
        if extra_e:
            raise ValueError(
                f"evidence entry {eid!r} has unexpected keys: {sorted(extra_e)}"
            )
        for req in (
            "id",
            "url",
            "title",
            "section",
            "verbatim_passage",
            "passage_sha256",
            "supports",
            "does_not_establish",
            "retrieved_date",
            "reviewed_date",
            "admission_reviewer",
            "admission_kind",
        ):
            if req not in entry or not entry[req]:
                raise ValueError(
                    f"evidence entry {eid!r} missing required field: {req}"
                )
        actual_hash = hashlib.sha256(
            entry["verbatim_passage"].encode("utf-8")
        ).hexdigest()
        if actual_hash != entry["passage_sha256"]:
            raise ValueError(
                f"tampered evidence passage for {eid!r}: hash mismatch "
                f"(expected {entry['passage_sha256']}, got {actual_hash})"
            )
        if entry["admission_reviewer"] == "Gemini":
            raise ValueError(
                f"evidence entry {eid!r} reviewer must differ from implementer"
            )

    cases = raw.get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError("cases catalogue missing non-empty cases dict")

    for cid, case_data in cases.items():
        if not isinstance(case_data, dict):
            raise ValueError(f"case {cid!r} must be an object")
        extra_c = set(case_data.keys()) - ALLOWED_CASE_KEYS
        if extra_c:
            raise ValueError(f"case {cid!r} has unexpected keys: {sorted(extra_c)}")
        if (
            "researched_facts" not in case_data
            or "source_licence_evidence" not in case_data
            or "design_choices" not in case_data
        ):
            raise ValueError(
                f"case {cid!r} must separate researched_facts, "
                "source_licence_evidence and design_choices"
            )
        citations = case_data.get("researched_facts", {}).get(
            "primary_evidence_ids", []
        )
        for cite_id in citations:
            if cite_id not in evidence_pack:
                raise ValueError(
                    f"case {cid!r} cites unadmitted evidence id: {cite_id!r}"
                )

    return raw


def get_case(case_id_or_alias: str) -> dict[str, Any]:
    """Return the admitted case definition by id or registered alias."""
    catalogue = load_cases_catalogue()
    cases = catalogue["cases"]
    target = case_id_or_alias.strip().lower()
    if target in cases:
        return cases[target]
    for case_data in cases.values():
        if case_data.get("id", "").lower() == target:
            return case_data
        aliases = [a.lower() for a in case_data.get("aliases", [])]
        if target in aliases:
            return case_data
    known = sorted(
        list(cases.keys()) + [a for c in cases.values() for a in c.get("aliases", [])]
    )
    raise KeyError(
        f"unknown case {case_id_or_alias!r}; admitted cases: {', '.join(known)}"
    )


def get_evidence_pack() -> dict[str, Any]:
    """Return the validated evidence pack dictionary."""
    catalogue = load_cases_catalogue()
    return catalogue["evidence_pack"]


def validate_citations(
    cited_ids: Sequence[str],
    allowed_ids: Sequence[str] | None = None,
) -> list[str]:
    """Validate that every cited id exists in the closed evidence pack.

    Fabricated or unadmitted evidence ids fail loudly.
    """
    pack = get_evidence_pack()
    validated: list[str] = []
    for item in cited_ids:
        clean = item.strip()
        if clean not in pack:
            raise ValueError(f"unknown or fabricated evidence id: {clean!r}")
        if allowed_ids is not None and clean not in allowed_ids:
            raise ValueError(
                f"evidence id {clean!r} is not admitted for the selected case"
            )
        if clean not in validated:
            validated.append(clean)
    if not validated:
        raise ValueError("at least one valid evidence citation is required")
    return validated


def check_source_joint_satisfiability(
    target_rows: int,
    case_def: Mapping[str, Any],
    source_reference: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """Check if target_rows can jointly satisfy inclusion, answer rates and blank presence."""
    design = case_def.get("design_choices", {})
    vf_tol = design.get("valid_fraction_tolerance_absolute_max", 0.02)
    swir_tol = design.get("answer_rate_tolerance_absolute_max", 0.02)
    tir_tol = design.get("answer_rate_tolerance_absolute_max", 0.02)

    vf = source_reference.get("valid_fraction", 0.0)
    swir_rate = source_reference.get("swir_answer_rate", 0.0)
    tir_rate = source_reference.get("tir_answer_rate", 0.0)
    has_blanks = source_reference.get("has_blanks", True)

    k = round(target_rows * vf)
    if k < 1 or (target_rows - k) < 1:
        return (
            False,
            f"target_rows {target_rows} gives {k} included and {target_rows - k} excluded rows; "
            "both states must be present",
        )

    if abs(k / target_rows - vf) > vf_tol:
        return (
            False,
            f"target_rows {target_rows} valid fraction {k / target_rows:.4f} drifts "
            f"{abs(k / target_rows - vf):.4f} from source {vf:.4f} (max {vf_tol})",
        )

    if has_blanks and k > 1:
        for channel, rate, tol in (
            ("SWIR", swir_rate, swir_tol),
            ("TIR", tir_rate, tir_tol),
        ):
            if rate < 1.0:
                feasible_a = [a for a in range(1, k) if abs(a / k - rate) <= tol]
                if not feasible_a:
                    max_rate = (k - 1) / k
                    return (
                        False,
                        f"target row count {target_rows} (k={k} valid rows) cannot jointly satisfy "
                        f"{channel} answer rate ({rate:.4f} +/- {tol}) and required blank presence "
                        f"(max feasible rate with at least one blank is {k - 1}/{k} = {max_rate:.4f})",
                    )

    return True, None


def compute_minimum_feasible_rows(
    case_def: Mapping[str, Any],
    source_reference: Mapping[str, Any],
) -> int:
    """Find smallest target row count >= floor that is jointly satisfiable for this source."""
    floor = case_def.get("design_choices", {}).get("min_satisfiable_rows", 25)
    ceiling = case_def.get("design_choices", {}).get("max_supported_rows", 200000)
    for n in range(floor, min(ceiling, 5000) + 1):
        ok, _ = check_source_joint_satisfiability(n, case_def, source_reference)
        if ok:
            return n
    return floor


def validate_plan_satisfiability(
    n_rows: int,
    case_def: Mapping[str, Any],
    source_reference: Mapping[str, Any] | None = None,
) -> None:
    """Validate that requested row count satisfies tolerance floors at contract time."""
    if not isinstance(n_rows, int) or isinstance(n_rows, bool):
        raise TypeError(f"target row count must be an integer, got {type(n_rows)}")
    design = case_def.get("design_choices", {})
    floor = design.get("min_satisfiable_rows", 25)
    ceiling = design.get("max_supported_rows", 200000)
    if n_rows < floor:
        raise ValueError(
            f"requested row count {n_rows} is below the admitted case "
            f"satisfiability floor ({floor}); refuses early"
        )
    if n_rows > ceiling:
        raise ValueError(
            f"requested row count {n_rows} exceeds maximum supported rows ({ceiling})"
        )

    if source_reference is not None:
        ok, reason = check_source_joint_satisfiability(
            n_rows, case_def, source_reference
        )
        if not ok:
            min_feasible = compute_minimum_feasible_rows(case_def, source_reference)
            raise ValueError(
                f"target row count {n_rows} is not jointly satisfiable for this source: {reason}; "
                f"minimum feasible rows for this source is {min_feasible}"
            )


def compile_case_contract(
    case_def: Mapping[str, Any],
    target_rows: int,
    source_sha256: str,
    source_reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile an immutable case contract binding requested rows and source pin.

    - Validates target_rows against satisfiability floor (25), max (200000),
      and source-aware joint satisfiability when source_reference is supplied.
    - Binds measured intake source_sha256 pin (64-char lowercase hex string).
    - Locks case checks, denominators, and thresholds from installed case definition.
    - Resulting contract shape is strictly typed with no extra keys.
    """
    validate_plan_satisfiability(target_rows, case_def, source_reference)
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or not all(c in "0123456789abcdefABCDEF" for c in source_sha256)
    ):
        raise ValueError(
            f"invalid source_sha256 pin: must be 64-char hex string, got {source_sha256!r}"
        )

    template = case_def.get("contract_template", {})
    checks = dict(template.get("checks", {}))
    checks["exact_rows"] = target_rows

    contract = {
        "version": 1,
        "case_id": case_def["id"],
        "target_rows": target_rows,
        "source_sha256": source_sha256.lower(),
        "output_columns": list(template.get("output_columns", [])),
        "recommended_strategy": template.get(
            "recommended_strategy", "fitted_markov_balanced"
        ),
        "checks": checks,
        "denominators": dict(template.get("denominators", {})),
        "flag_encoding": template.get("flag_encoding", "true/false or 0/1"),
        "spacing_definition": template.get(
            "spacing_definition", "median of positive adjacent depth differences"
        ),
        "limitations": list(case_def.get("limitations", [])),
    }
    return contract


def validate_case_contract(
    contract: Mapping[str, Any],
    case_def: Mapping[str, Any],
    source_reference: Mapping[str, Any] | None = None,
) -> None:
    """Validate that a case contract strictly matches canonical schema and case rules."""
    if not isinstance(contract, Mapping):
        raise TypeError(f"contract must be a mapping, got {type(contract)}")

    contract_keys = set(contract.keys())
    missing = ALLOWED_CONTRACT_KEYS - contract_keys
    if missing:
        raise ValueError(f"contract missing required keys: {sorted(missing)}")
    extra = contract_keys - ALLOWED_CONTRACT_KEYS
    if extra:
        raise ValueError(f"contract has unexpected keys: {sorted(extra)}")

    if contract.get("version") != 1 or isinstance(contract.get("version"), bool):
        raise ValueError(
            f"contract version must be integer 1, got {contract.get('version')!r}"
        )

    if contract.get("case_id") != case_def.get("id"):
        raise ValueError(
            f"contract case_id mismatch: expected {case_def.get('id')}, "
            f"got {contract.get('case_id')}"
        )

    target_rows = contract.get("target_rows")
    if not isinstance(target_rows, int) or isinstance(target_rows, bool):
        raise TypeError(
            f"contract target_rows must be integer, got {type(target_rows)}"
        )
    validate_plan_satisfiability(target_rows, case_def, source_reference)

    source_sha = contract.get("source_sha256")
    if (
        not isinstance(source_sha, str)
        or len(source_sha) != 64
        or not all(c in "0123456789abcdef" for c in source_sha.lower())
    ):
        raise ValueError(
            f"contract source_sha256 must be a valid 64-char hex string, got {source_sha!r}"
        )

    expected_cols = list(case_def["contract_template"]["output_columns"])
    if contract.get("output_columns") != expected_cols:
        raise ValueError(
            f"contract output_columns mismatch: expected {expected_cols}, "
            f"got {contract.get('output_columns')}"
        )

    expected_strat = case_def["contract_template"]["recommended_strategy"]
    if contract.get("recommended_strategy") != expected_strat:
        raise ValueError(
            f"contract recommended_strategy mismatch: expected {expected_strat!r}, "
            f"got {contract.get('recommended_strategy')!r}"
        )

    canonical_checks = dict(case_def["contract_template"]["checks"])
    canonical_checks["exact_rows"] = target_rows
    if contract.get("checks") != canonical_checks:
        raise ValueError(
            f"contract checks mismatch: expected canonical {canonical_checks}, "
            f"got {contract.get('checks')}"
        )

    expected_denoms = dict(case_def["contract_template"]["denominators"])
    if contract.get("denominators") != expected_denoms:
        raise ValueError(
            f"contract denominators mismatch: expected {expected_denoms}, "
            f"got {contract.get('denominators')}"
        )

    expected_flag = case_def["contract_template"]["flag_encoding"]
    if contract.get("flag_encoding") != expected_flag:
        raise ValueError(
            f"contract flag_encoding mismatch: expected {expected_flag!r}, "
            f"got {contract.get('flag_encoding')!r}"
        )

    expected_spacing = case_def["contract_template"]["spacing_definition"]
    if contract.get("spacing_definition") != expected_spacing:
        raise ValueError(
            f"contract spacing_definition mismatch: expected {expected_spacing!r}, "
            f"got {contract.get('spacing_definition')!r}"
        )

    expected_limits = list(case_def.get("limitations", []))
    if contract.get("limitations") != expected_limits:
        raise ValueError("contract limitations mismatch with installed case definition")


def assess_source_suitability(
    source_path_or_rows: Path | str | Sequence[Any],
    case_def: Mapping[str, Any],
    licence_meta: str | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Deterministic local code check of source suitability.

    Reuses existing HyLogger reader semantics:
    - Verifies 100% clean numerical rows for this admitted case:
      records raw, usable, and rejected row counts. Any non-finite,
      negative, or malformed depth cell is rejected.
    - Required source columns: StartDepth, Final_Mask, Grp1_sTSAS, Grp1_sjCLST.
    - Non-decreasing finite depth ordering.
    - Positive median forward spacing (computed with even-length averaging).
    - Both inclusion states present.
    - Both answered and blank labels present among valid rows.
    - Disclosed licence status recorded.

    A source-format fit is not a scientific or origin/rights certification.
    A model cannot upgrade an unsuitable code verdict.
    """
    reasons: list[str] = []
    reqs = case_def.get("source_requirements", {})
    required_cols = reqs.get("required_columns", ["StartDepth", "Final_Mask"])
    min_rows = reqs.get("min_source_rows", 20)

    source_sha: str | None = None
    headers: list[str] = []
    raw_rows: list[dict[str, Any]] = []

    if isinstance(source_path_or_rows, (str, Path)):
        path = Path(source_path_or_rows)
        if not path.is_file():
            raise FileNotFoundError(f"source file not found: {path}")
        source_bytes = path.read_bytes()
        source_sha = hashlib.sha256(source_bytes).hexdigest()

        if expected_source_sha256 and source_sha != expected_source_sha256:
            reasons.append(
                f"source sha256 mismatch (expected {expected_source_sha256}, got {source_sha})"
            )

        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            raw_rows = list(reader)
    else:
        raw_rows = list(source_path_or_rows)
        if raw_rows and isinstance(raw_rows[0], dict):
            headers = list(raw_rows[0].keys())
        else:
            headers = list(required_cols)

    missing_cols = [c for c in required_cols if c not in headers]
    if missing_cols:
        reasons.append(f"missing required source columns: {', '.join(missing_cols)}")
        return {
            "suitable": False,
            "source_sha256": source_sha,
            "source_rows": len(raw_rows),
            "usable_rows": 0,
            "rejected_rows": len(raw_rows),
            "assessed_columns": headers,
            "reasons": reasons,
            "findings": "Source lacks required columns.",
            "licence_status": "rights unknown",
            "disclaimer": "A source-format fit is not a scientific or origin/rights certification.",
        }

    raw_count = len(raw_rows)
    if raw_count < min_rows:
        reasons.append(
            f"source row count {raw_count} is below minimum requirement {min_rows}"
        )

    parsed_rows: list[tuple[float, bool, str | None, str | None]] = []
    rejected_count = 0
    for r in raw_rows:
        depth_val = parse_depth(r.get("StartDepth"))
        if depth_val is None or depth_val < 0:
            rejected_count += 1
            continue
        is_valid = parse_valid(r.get("Final_Mask"))
        swir = normalize_group(r.get("Grp1_sTSAS"))
        tir = normalize_group(r.get("Grp1_sjCLST"))
        parsed_rows.append((depth_val, is_valid, swir, tir))

    if rejected_count > 0:
        reasons.append(
            f"source contains {rejected_count} malformed, non-finite or negative depth rows "
            f"(raw: {raw_count}, usable: {len(parsed_rows)}, rejected: {rejected_count}); "
            "admitted case requires 100% clean finite depths"
        )

    if not parsed_rows:
        reasons.append("source contains no valid numerical depth rows")
        return {
            "suitable": False,
            "source_sha256": source_sha,
            "source_rows": raw_count,
            "usable_rows": 0,
            "rejected_rows": rejected_count,
            "assessed_columns": headers,
            "reasons": reasons,
            "findings": "Source contains no usable numerical rows.",
            "licence_status": "rights unknown",
            "disclaimer": "A source-format fit is not a scientific or origin/rights certification.",
        }

    if reqs.get("require_ordered_finite_depth", True):
        depths = [r[0] for r in parsed_rows]
        if any(b < a for a, b in zip(depths, depths[1:])):
            reasons.append("source depth values are not monotonically non-decreasing")

    diffs = [b[0] - a[0] for a, b in zip(parsed_rows, parsed_rows[1:]) if b[0] > a[0]]
    if reqs.get("require_positive_median_spacing", True):
        if not diffs:
            reasons.append("source has no positive adjacent depth steps")
        else:
            diffs.sort()
            med_spacing = float(statistics.median(diffs))
            if med_spacing <= 0:
                reasons.append("source median forward spacing is non-positive")

    valid_count = sum(1 for r in parsed_rows if r[1])
    invalid_count = len(parsed_rows) - valid_count
    if reqs.get("require_both_inclusion_states", True):
        if valid_count == 0 or invalid_count == 0:
            reasons.append("source does not contain both included and excluded rows")

    if reqs.get("require_both_label_states_in_included", True) and valid_count > 0:
        valid_rows = [r for r in parsed_rows if r[1]]
        swir_answered = sum(1 for r in valid_rows if r[2] is not None)
        tir_answered = sum(1 for r in valid_rows if r[3] is not None)
        if swir_answered == 0 or swir_answered == len(valid_rows):
            reasons.append("source does not have both answered and blank SWIR labels")
        if tir_answered == 0 or tir_answered == len(valid_rows):
            reasons.append("source does not have both answered and blank TIR labels")

    # Licence check
    licence_status = "rights unknown"
    if licence_meta and licence_meta.strip():
        licence_status = f"disclosed: {licence_meta.strip()}"
    elif isinstance(source_path_or_rows, (str, Path)):
        src_path = Path(source_path_or_rows)
        for sibling_cand in (
            src_path.with_name(src_path.stem + "-source.json"),
            src_path.with_name(src_path.name.replace("-scalars.csv", "-source.json")),
        ):
            if sibling_cand.is_file():
                try:
                    sdata = json.loads(sibling_cand.read_text(encoding="utf-8"))
                    lic = sdata.get("licence") or sdata.get("license")
                    if lic:
                        licence_status = f"disclosed: {lic}"
                        break
                except Exception:
                    pass

    suitable = len(reasons) == 0
    findings = (
        "Source meets all deterministic suitability criteria for the admitted case."
        if suitable
        else f"Source failed suitability checks: {'; '.join(reasons)}"
    )

    diffs = [b[0] - a[0] for a, b in zip(parsed_rows, parsed_rows[1:]) if b[0] > a[0]]
    diffs.sort()
    med_spacing = float(statistics.median(diffs)) if diffs else 0.0
    valid_rows = [r for r in parsed_rows if r[1]]
    swir_rate = (
        sum(1 for r in valid_rows if r[2] is not None) / len(valid_rows)
        if valid_rows
        else 0.0
    )
    tir_rate = (
        sum(1 for r in valid_rows if r[3] is not None) / len(valid_rows)
        if valid_rows
        else 0.0
    )

    return {
        "suitable": suitable,
        "source_sha256": source_sha,
        "source_rows": raw_count,
        "usable_rows": len(parsed_rows),
        "rejected_rows": rejected_count,
        "assessed_columns": headers,
        "reasons": reasons,
        "findings": findings,
        "licence_status": licence_status,
        "disclaimer": "A source-format fit is not a scientific or origin/rights certification.",
        "aggregates": {
            "valid_fraction": valid_count / len(parsed_rows) if parsed_rows else 0.0,
            "median_forward_spacing": med_spacing,
            "swir_answer_rate": swir_rate,
            "tir_answer_rate": tir_rate,
            "disclosed_swir_labels": sorted(
                {r[2] for r in valid_rows if r[2] is not None}
            ),
            "disclosed_tir_labels": sorted(
                {r[3] for r in valid_rows if r[3] is not None}
            ),
            "has_blanks": any(r[2] is None or r[3] is None for r in valid_rows),
        },
    }


def validate_source_reference(source_reference: Mapping[str, Any]) -> None:
    """Validate that a source reference dictionary adheres strictly to schema."""
    if not isinstance(source_reference, Mapping):
        raise TypeError(
            f"source reference must be a mapping, got {type(source_reference)}"
        )
    keys = set(source_reference.keys())
    missing = ALLOWED_REFERENCE_KEYS - keys
    if missing:
        raise ValueError(f"source reference missing required keys: {sorted(missing)}")
    extra = keys - ALLOWED_REFERENCE_KEYS
    if extra:
        raise ValueError(f"source reference has unexpected keys: {sorted(extra)}")

    if source_reference.get("version") != 1 or isinstance(
        source_reference.get("version"), bool
    ):
        raise ValueError("source reference version must be integer 1")

    sha = source_reference.get("source_sha256")
    if (
        not isinstance(sha, str)
        or len(sha) != 64
        or not all(c in "0123456789abcdef" for c in sha.lower())
    ):
        raise ValueError(
            f"source reference source_sha256 must be 64-char hex, got {sha!r}"
        )

    for count_key in ("source_rows", "usable_rows"):
        v = source_reference.get(count_key)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"source reference {count_key} must be positive integer")

    for rate_key in ("valid_fraction", "swir_answer_rate", "tir_answer_rate"):
        r = source_reference.get(rate_key)
        if (
            not isinstance(r, (int, float))
            or isinstance(r, bool)
            or not math.isfinite(r)
            or not (0.0 <= r <= 1.0)
        ):
            raise ValueError(
                f"source reference {rate_key} must be finite float in [0, 1]"
            )

    sp = source_reference.get("median_forward_spacing")
    if (
        not isinstance(sp, (int, float))
        or isinstance(sp, bool)
        or not math.isfinite(sp)
        or sp <= 0
    ):
        raise ValueError(
            "source reference median_forward_spacing must be positive finite float"
        )

    for label_key in ("disclosed_swir_labels", "disclosed_tir_labels"):
        labels = source_reference.get(label_key)
        if not isinstance(labels, list) or not all(
            isinstance(item, str) for item in labels
        ):
            raise ValueError(f"source reference {label_key} must be a list of strings")

    if not isinstance(source_reference.get("has_blanks"), bool):
        raise ValueError("source reference has_blanks must be boolean")


def build_source_reference(
    source_path_or_rows: Path | str | Sequence[Any],
    case_def: Mapping[str, Any] | None = None,
    licence_meta: str | None = None,
) -> dict[str, Any]:
    """Extract bounded source aggregate reference without exposing raw source rows.

    Refuses unsuitable sources early. The returned reference contains aggregate
    statistical parameters and full code-side disclosed vocabulary required
    for recipient verification.
    """
    case = case_def or get_case("hylogger_scalar_teaching_v1")
    suit = assess_source_suitability(source_path_or_rows, case, licence_meta)
    if not suit["suitable"]:
        raise ValueError(
            f"cannot build source reference from unsuitable source: {'; '.join(suit['reasons'])}"
        )

    aggs = suit.get("aggregates", {})
    source_sha = suit.get("source_sha256")
    if not source_sha and isinstance(source_path_or_rows, (str, Path)):
        source_sha = hashlib.sha256(Path(source_path_or_rows).read_bytes()).hexdigest()

    ref = {
        "version": 1,
        "source_sha256": source_sha.lower() if source_sha else None,
        "source_rows": suit.get("source_rows", 0),
        "usable_rows": suit.get("usable_rows", 0),
        "rejected_rows": suit.get("rejected_rows", 0),
        "valid_fraction": aggs.get("valid_fraction", 0.0),
        "median_forward_spacing": aggs.get("median_forward_spacing", 0.0),
        "swir_answer_rate": aggs.get("swir_answer_rate", 0.0),
        "tir_answer_rate": aggs.get("tir_answer_rate", 0.0),
        "disclosed_swir_labels": aggs.get("disclosed_swir_labels", []),
        "disclosed_tir_labels": aggs.get("disclosed_tir_labels", []),
        "has_blanks": aggs.get("has_blanks", False),
        "licence_status": suit.get("licence_status", "rights unknown"),
        "notice": (
            "Bounded aggregate reference for code-side utility checking and verification. "
            "Model-disclosed summaries apply a frequency floor (MIN_LABEL_COUNT_FOR_MODEL_VIEW = 2) "
            "to redact singletons; code-side utility checking uses the full code-side vocabulary "
            "disclosed in this reference. Contains no raw measurements or individual source rows."
        ),
    }
    validate_source_reference(ref)
    return ref


def evaluate_case_utility(
    candidate_path_or_bytes: Path | str | bytes,
    source_reference_or_path: Mapping[str, Any] | Path | str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute real downstream table read/filter/group practical-use evaluation.

    Accepts either an explicit source CSV path OR a pre-computed bounded
    aggregate reference dictionary. Recipient verification works without
    access to raw source rows.

    Checks computed against contract:
    - strict contract validation and source reference schema validation;
    - source pin match;
    - output columns exact match;
    - row count exact match;
    - CSV row width consistency (no extra or truncated values);
    - well-formed rows (finite depth >= 0, binary valid);
    - ordered non-decreasing depth;
    - median forward spacing relative difference <= 0.01 (even-length averaged);
    - inclusion fraction absolute difference <= 0.02;
    - SWIR and TIR answer rates absolute difference <= 0.02 on included rows;
    - nonblank labels across ALL candidate rows supported by source vocabulary;
    - real downstream filter/group accounting for 100% of rows;
    - structure presence of both inclusion states and blank labels.
    """
    # 1. Resolve and validate source reference
    if isinstance(source_reference_or_path, Mapping):
        source_ref = dict(source_reference_or_path)
    else:
        source_ref = build_source_reference(source_reference_or_path)
    validate_source_reference(source_ref)

    # 2. Strict contract validation
    case_def = get_case(contract.get("case_id", "hylogger_scalar_teaching_v1"))
    validate_case_contract(contract, case_def, source_ref)

    # 3. Read candidate payload
    if isinstance(candidate_path_or_bytes, bytes):
        candidate_bytes = candidate_path_or_bytes
    else:
        cand_p = Path(candidate_path_or_bytes)
        if not cand_p.is_file():
            raise FileNotFoundError(f"candidate file not found: {cand_p}")
        candidate_bytes = cand_p.read_bytes()

    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()

    failures: list[str] = []
    measurements: dict[str, Any] = {}

    def check(name: str, ok: bool, measured: Any) -> None:
        measurements[name] = measured
        if not ok:
            failures.append(name)

    # 4. Source pin check
    expected_src_sha = contract.get("source_sha256")
    ref_sha = source_ref.get("source_sha256")
    check(
        "source_pin",
        ref_sha is not None and ref_sha.lower() == expected_src_sha.lower(),
        {"expected": expected_src_sha, "reference": ref_sha},
    )

    # 5. Parse candidate CSV with row-width checking
    text = candidate_bytes.decode("utf-8")
    raw_reader = csv.reader(io.StringIO(text))
    header_row = next(raw_reader, None) or []

    expected_cols = contract.get(
        "output_columns", ["depth_m", "valid", "swir_group", "tir_group"]
    )
    check("output_columns", header_row == expected_cols, header_row)

    target_rows = contract.get("target_rows", 10000)

    line_no = 1
    raw_lines: list[list[str]] = []
    row_width_ok = True
    malformed_detail: str | None = None
    for line in raw_reader:
        line_no += 1
        if len(line) != len(expected_cols):
            row_width_ok = False
            malformed_detail = (
                f"malformed CSV row width at line {line_no}: "
                f"expected {len(expected_cols)} columns, got {len(line)}"
            )
            break
        raw_lines.append(line)

    check("row_count", len(raw_lines) == target_rows, len(raw_lines))

    normalized: list[tuple[float, bool, str | None, str | None]] = []
    well_formed_ok = row_width_ok
    for line in raw_lines:
        row = dict(zip(header_row, line))
        try:
            x = float(row.get("depth_m", ""))
            flag = str(row.get("valid", "")).strip().lower()
            if flag not in {"true", "false", "0", "1"}:
                well_formed_ok = False
                continue
            if not math.isfinite(x) or x < 0:
                well_formed_ok = False
                continue
            sw = row.get("swir_group") or None
            ti = row.get("tir_group") or None
            normalized.append((x, flag in {"true", "1"}, sw, ti))
        except (ValueError, TypeError):
            well_formed_ok = False

    check(
        "well_formed_rows",
        well_formed_ok and (len(normalized) == len(raw_lines)),
        {
            "parsed": len(normalized),
            "total": len(raw_lines),
            "malformed_detail": malformed_detail,
        },
    )

    if not normalized:
        return {
            "passed": False,
            "failures": failures + ["no_usable_rows"],
            "measurements": measurements,
            "candidate_sha256": candidate_sha,
            "limitations": list(case_def.get("limitations", [])),
        }

    # 6. Depth ordering
    check(
        "ordered_depth",
        all(b[0] >= a[0] for a, b in zip(normalized, normalized[1:])),
        "nondecreasing required",
    )

    # 7. Median forward spacing (even-length averaged)
    diffs = [b[0] - a[0] for a, b in zip(normalized, normalized[1:]) if b[0] > a[0]]
    diffs.sort()
    cand_sp = float(statistics.median(diffs)) if diffs else None
    src_sp = source_ref.get("median_forward_spacing")
    spacing_delta = (
        abs(cand_sp - src_sp) / src_sp
        if (src_sp is not None and cand_sp is not None and src_sp > 0)
        else None
    )
    check(
        "depth_spacing",
        spacing_delta is not None and spacing_delta <= 0.01,
        {
            "source_reference": src_sp,
            "candidate": cand_sp,
            "relative_difference": spacing_delta,
            "limit": 0.01,
            "definition": "median of positive adjacent differences (averaged for even count)",
        },
    )

    # 8. Inclusion fraction
    src_vf = source_ref.get("valid_fraction", 0.0)
    cand_vf = sum(1 for r in normalized if r[1]) / len(normalized)
    vf_delta = abs(cand_vf - src_vf)
    check(
        "inclusion_fraction",
        vf_delta <= 0.02,
        {
            "source_reference": src_vf,
            "candidate": cand_vf,
            "absolute_difference": vf_delta,
            "limit": 0.02,
        },
    )

    # 9. Channel answer rates (included rows only)
    cvalid = [r for r in normalized if r[1]]
    for i, name in ((2, "swir"), (3, "tir")):
        src_rate = source_ref.get(f"{name}_answer_rate")
        cand_rate = (
            sum(1 for r in cvalid if r[i] is not None) / len(cvalid) if cvalid else None
        )
        delta = (
            abs(cand_rate - src_rate)
            if (src_rate is not None and cand_rate is not None)
            else None
        )
        check(
            f"{name}_answer_rate",
            delta is not None and delta <= 0.02,
            {
                "source_reference": src_rate,
                "candidate": cand_rate,
                "absolute_difference": delta,
                "limit": 0.02,
                "denominator": "included rows only",
            },
        )

    # 10. Nonblank supported vocabulary across ALL rows (neither included nor excluded may hide unknown labels)
    for i, name in ((2, "swir"), (3, "tir")):
        allowed = set(source_ref.get(f"disclosed_{name}_labels", []))
        all_nonblank = {r[i] for r in normalized if r[i] is not None}
        extra = sorted(all_nonblank - allowed)
        check(
            f"{name}_supported_labels",
            not extra,
            {
                "unknown_labels": extra,
                "population": "all candidate rows (included and excluded)",
            },
        )

    # 11. Downstream filter & group accounting for 100% of rows
    grouped = Counter((r[1], r[2] is not None, r[3] is not None) for r in normalized)
    included_count = len(cvalid)
    excluded_count = sum(1 for r in normalized if not r[1])
    accounting_ok = (
        sum(grouped.values()) == len(raw_lines)
        and (included_count + excluded_count) == len(raw_lines)
        and len(raw_lines) == target_rows
    )
    check(
        "downstream_filter_group",
        accounting_ok,
        {
            "total_rows": sum(grouped.values()),
            "included": included_count,
            "excluded": excluded_count,
            "group_count": len(grouped),
        },
    )

    # 12. Structure presence
    src_has_blanks = source_ref.get("has_blanks", True)
    cand_has_blanks = any(r[2] is None or r[3] is None for r in cvalid)
    structure_ok = (
        included_count > 0
        and excluded_count > 0
        and (cand_has_blanks if src_has_blanks else True)
    )
    check(
        "downstream_structure_both_states_present",
        structure_ok,
        {
            "included_rows": included_count,
            "excluded_rows": excluded_count,
            "source_has_blanks": src_has_blanks,
            "candidate_has_blanks": cand_has_blanks,
            "swir_blanks_in_valid": sum(1 for r in cvalid if r[2] is None),
            "tir_blanks_in_valid": sum(1 for r in cvalid if r[3] is None),
        },
    )

    return {
        "passed": not failures,
        "failures": failures,
        "measurements": measurements,
        "candidate_sha256": candidate_sha,
        "limitations": list(case_def.get("limitations", [])),
    }


def verify_case_candidate(
    candidate_path_or_bytes: Path | str | bytes,
    source_reference_or_path: Mapping[str, Any] | Path | str,
    contract: Mapping[str, Any],
    reported_utility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consumer verification recomputing all case utility checks independently.

    A reported PASS over failing data, or tampering with reported measurements,
    is detected and refused. Does NOT require raw source rows when given a
    source aggregate reference.
    """
    recomputed = evaluate_case_utility(
        candidate_path_or_bytes, source_reference_or_path, contract
    )
    matches_reported = True
    if reported_utility is not None:
        if reported_utility.get("passed") != recomputed["passed"]:
            matches_reported = False
        if reported_utility.get("failures") != recomputed["failures"]:
            matches_reported = False
        if reported_utility.get("candidate_sha256") != recomputed["candidate_sha256"]:
            matches_reported = False

    case_verified = recomputed["passed"] and matches_reported
    return {
        "case_verified": case_verified,
        "recomputed": recomputed,
        "matches_reported": matches_reported,
    }
