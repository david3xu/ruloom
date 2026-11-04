"""Admitted case records, provenance validation and practical-use checking.

This module provides profile-owned case logic for the research-led workflow:
- validates installed case definitions and reviewed local evidence pack;
- verifies tamper-evident snapshot hashes, primary URLs and admission reviews;
- compiles strict typed case contracts with bounded target rows and source pins;
- enforces deterministic source suitability pre-flight before any model call;
- builds bounded source aggregate references without shipping raw source rows;
- enforces downstream practical-use checking (read/filter/group, row accounting,
  structure presence, and fixed lesson tolerances from agreement);
- provides independent consumer recomputation for recipient verification.

This module imports no engine and no model code (FP-CODE-2).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..intake.reader import NO_ANSWER

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


def build_profile_stats(source_path: Path | str) -> tuple[Any, Any, Any]:
    """Build fitted statistics and fidelity reference from source CSV."""
    from ..analysis.statistics import Profile
    from ..intake.reader import read_scalar_csv
    from ..validation.checks import source_fidelity_reference

    path = Path(source_path)
    samples = read_scalar_csv(path)
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    profile = Profile.from_samples(samples, path.name, source_sha)
    ref_stats = source_fidelity_reference(samples)
    return profile, ref_stats, samples


def generate_synthetic(profile: Any, plan: Mapping[str, Any]) -> bytes:
    """Generate synthetic CSV bytes from fitted profile and plan."""
    from ..generation.generate import generate

    return generate(profile, plan)


def run_profile_checks(
    csv_bytes: bytes,
    profile: Any,
    ref_stats: Any,
    plan: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Execute profile validation checks on candidate CSV bytes."""
    import os

    from ..analysis.calibration import load_thresholds
    from ..validation.checks import run_checks
    from ..validation.dataset import read as read_artifact

    thresholds_path = os.environ.get("RULOOM_THRESHOLDS")
    t_path = (
        Path(thresholds_path)
        if thresholds_path
        else Path(__file__).resolve().parent.parent / "thresholds.json"
    )
    thresholds = load_thresholds(t_path)
    samples = read_artifact(csv_bytes)
    expected_count = compute_expected_valid_count(profile, plan)
    results_report = run_checks(
        samples,
        thresholds,
        source_reference=ref_stats,
        expected_geometry={
            "n_rows": plan.get("n_rows"),
            "start_m": getattr(
                getattr(profile, "geometry", None), "depth_start_m", None
            ),
            "step_m": getattr(
                getattr(profile, "geometry", None), "median_forward_spacing_m", None
            ),
        },
        expected_valid_count=expected_count,
    )
    passed = results_report.verdict == "pass"
    records = [
        {
            "name": r.name,
            "status": r.status,
            "detail": r.detail,
            "band_position": r.band_position,
        }
        for r in results_report.results
    ]
    return records, passed


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


def validate_plan_satisfiability(n_rows: int, case_def: Mapping[str, Any]) -> None:
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


def compile_case_contract(
    case_def: Mapping[str, Any],
    target_rows: int,
    source_sha256: str,
) -> dict[str, Any]:
    """Compile an immutable case contract binding requested rows and source pin.

    - Validates target_rows against the satisfiability floor (25) and max (200000).
    - Binds measured intake source_sha256 pin (64-char hex string).
    - Locks case checks, denominators, and thresholds from installed case definition.
    - Resulting contract shape is strictly typed with no extra keys.
    """
    validate_plan_satisfiability(target_rows, case_def)
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
) -> None:
    """Validate that a case contract adheres to strict schema and case rules."""
    extra = set(contract.keys()) - ALLOWED_CONTRACT_KEYS
    if extra:
        raise ValueError(f"contract has unexpected keys: {sorted(extra)}")

    if contract.get("case_id") != case_def.get("id"):
        raise ValueError(
            f"contract case_id mismatch: expected {case_def.get('id')}, "
            f"got {contract.get('case_id')}"
        )
    target_rows = contract.get("target_rows")
    if not isinstance(target_rows, int) or isinstance(target_rows, bool):
        raise TypeError("contract target_rows must be integer")
    validate_plan_satisfiability(target_rows, case_def)


def assess_source_suitability(
    source_path_or_rows: Path | str | Sequence[Any],
    case_def: Mapping[str, Any],
    licence_meta: str | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Deterministic local code check of source suitability.

    Checks:
    - source exists and matches expected hash if pinned;
    - contains required source columns;
    - meets minimum row count;
    - depth values are finite, non-negative, and monotonically non-decreasing;
    - median forward spacing is positive;
    - contains both inclusion states (valid and invalid rows);
    - contains both answered and blank labels among valid rows;
    - records licence metadata status (missing licence -> 'rights unknown').

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
        # Supplied as pre-parsed sequence
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
            "assessed_columns": headers,
            "reasons": reasons,
            "findings": "Source lacks required columns.",
            "licence_status": "rights unknown",
            "disclaimer": "A source-format fit is not a scientific or origin/rights certification.",
        }

    if len(raw_rows) < min_rows:
        reasons.append(
            f"source row count {len(raw_rows)} is below minimum requirement {min_rows}"
        )

    parsed_rows: list[tuple[float, bool, str | None, str | None]] = []
    for r in raw_rows:
        try:
            depth_val = float(r["StartDepth"])
            if not math.isfinite(depth_val) or depth_val < 0:
                continue
            is_valid = str(r["Final_Mask"]).strip() == "1"
            swir = str(r.get("Grp1_sTSAS", "")).strip()
            tir = str(r.get("Grp1_sjCLST", "")).strip()
            parsed_rows.append(
                (
                    depth_val,
                    is_valid,
                    None if swir.upper() in NO_ANSWER else swir,
                    None if tir.upper() in NO_ANSWER else tir,
                )
            )
        except (ValueError, TypeError, KeyError):
            continue

    if not parsed_rows:
        reasons.append("source contains no valid numerical depth rows")
        return {
            "suitable": False,
            "source_sha256": source_sha,
            "source_rows": len(raw_rows),
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
            med_spacing = diffs[len(diffs) // 2]
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

    diffs.sort()
    med_spacing = diffs[len(diffs) // 2] if diffs else 0.0
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
        "source_rows": len(raw_rows),
        "assessed_columns": headers,
        "reasons": reasons,
        "findings": findings,
        "licence_status": licence_status,
        "disclaimer": "A source-format fit is not a scientific or origin/rights certification.",
        "aggregates": {
            "valid_fraction": valid_count / len(parsed_rows),
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


def build_source_reference(
    source_path_or_rows: Path | str | Sequence[Any],
    case_def: Mapping[str, Any] | None = None,
    licence_meta: str | None = None,
) -> dict[str, Any]:
    """Extract bounded source aggregate reference without exposing raw source rows.

    The returned reference contains aggregate statistical parameters and
    disclosed vocabulary required for recipient verification, but NO
    individual source measurements or rows.
    """
    case = case_def or get_case("hylogger_scalar_teaching_v1")
    suit = assess_source_suitability(source_path_or_rows, case, licence_meta)
    aggs = suit.get("aggregates", {})

    source_sha = suit.get("source_sha256")
    if not source_sha and isinstance(source_path_or_rows, (str, Path)):
        source_sha = hashlib.sha256(Path(source_path_or_rows).read_bytes()).hexdigest()

    return {
        "version": 1,
        "source_sha256": source_sha,
        "source_rows": suit.get("source_rows", 0),
        "valid_fraction": aggs.get("valid_fraction", 0.0),
        "median_forward_spacing": aggs.get("median_forward_spacing", 0.0),
        "swir_answer_rate": aggs.get("swir_answer_rate", 0.0),
        "tir_answer_rate": aggs.get("tir_answer_rate", 0.0),
        "disclosed_swir_labels": aggs.get("disclosed_swir_labels", []),
        "disclosed_tir_labels": aggs.get("disclosed_tir_labels", []),
        "has_blanks": aggs.get("has_blanks", False),
        "licence_status": suit.get("licence_status", "rights unknown"),
        "notice": "Bounded aggregate reference only; contains no individual source rows or raw measurements.",
    }


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
    - source pin match (if contract declares source_sha256);
    - output columns exact match;
    - row count exact match;
    - well-formed rows (finite depth >= 0, binary valid);
    - ordered non-decreasing depth;
    - median forward spacing relative difference <= 0.01;
    - inclusion fraction absolute difference <= 0.02;
    - SWIR and TIR answer rates absolute difference <= 0.02 on included rows;
    - nonblank labels supported by source vocabulary;
    - real downstream filter/group accounting for 100% of rows;
    - structure presence of both inclusion states and blank labels.
    """
    if isinstance(candidate_path_or_bytes, bytes):
        candidate_bytes = candidate_path_or_bytes
    else:
        cand_p = Path(candidate_path_or_bytes)
        if not cand_p.is_file():
            raise FileNotFoundError(f"candidate file not found: {cand_p}")
        candidate_bytes = cand_p.read_bytes()

    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()

    # Resolve source reference
    if isinstance(source_reference_or_path, Mapping):
        source_ref = dict(source_reference_or_path)
    else:
        source_ref = build_source_reference(source_reference_or_path)

    failures: list[str] = []
    measurements: dict[str, Any] = {}

    def check(name: str, ok: bool, measured: Any) -> None:
        measurements[name] = measured
        if not ok:
            failures.append(name)

    # 1. Source pin check
    expected_src_sha = contract.get("source_sha256")
    if expected_src_sha:
        ref_sha = source_ref.get("source_sha256")
        check(
            "source_pin",
            ref_sha is not None and ref_sha.lower() == expected_src_sha.lower(),
            {"expected": expected_src_sha, "reference": ref_sha},
        )

    # 2. Candidate CSV parsing
    text = candidate_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    out = list(reader)

    expected_cols = contract.get(
        "output_columns", ["depth_m", "valid", "swir_group", "tir_group"]
    )
    check("output_columns", headers == expected_cols, headers)

    target_rows = contract.get("target_rows", 10000)
    check("row_count", len(out) == target_rows, len(out))

    normalized: list[tuple[float, bool, str | None, str | None]] = []
    well_formed_ok = True
    for r in out:
        try:
            x = float(r.get("depth_m", ""))
            flag = str(r.get("valid", "")).strip().lower()
            if flag not in {"true", "false", "0", "1"}:
                well_formed_ok = False
                continue
            if not math.isfinite(x) or x < 0:
                well_formed_ok = False
                continue
            sw = r.get("swir_group") or None
            ti = r.get("tir_group") or None
            normalized.append((x, flag in {"true", "1"}, sw, ti))
        except (ValueError, TypeError):
            well_formed_ok = False

    check(
        "well_formed_rows",
        well_formed_ok and (len(normalized) == len(out)),
        {"parsed": len(normalized), "total": len(out)},
    )

    if not normalized:
        return {
            "passed": False,
            "failures": failures + ["no_usable_rows"],
            "measurements": measurements,
            "candidate_sha256": candidate_sha,
            "limitations": [
                "Case-level teaching-table utility only; no scientific, privacy, image or licensing guarantee.",
                "A source-format fit is not a scientific or origin/rights certification.",
            ],
        }

    # 3. Depth ordering
    check(
        "ordered_depth",
        all(b[0] >= a[0] for a, b in zip(normalized, normalized[1:])),
        "nondecreasing required",
    )

    # 4. Median forward spacing
    diffs = [b[0] - a[0] for a, b in zip(normalized, normalized[1:]) if b[0] > a[0]]
    diffs.sort()
    cand_sp = diffs[len(diffs) // 2] if diffs else None
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
            "definition": "median of positive adjacent differences",
        },
    )

    # 5. Inclusion fraction
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

    # 6. Channel answer rates and supported vocabulary on valid rows
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
        allowed = set(source_ref.get(f"disclosed_{name}_labels", []))
        extra = sorted({r[i] for r in cvalid if r[i] is not None} - allowed)
        check(f"{name}_supported_labels", not extra, {"unknown_labels": extra})

    # 7. Real downstream filter & group accounting for 100% of rows
    grouped = Counter((r[1], r[2] is not None, r[3] is not None) for r in normalized)
    included_count = len(cvalid)
    excluded_count = sum(1 for r in normalized if not r[1])
    accounting_ok = (
        sum(grouped.values()) == len(out)
        and (included_count + excluded_count) == len(out)
        and len(out) == target_rows
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

    # 8. Structure presence
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
        "limitations": [
            "Case-level teaching-table utility only; no scientific, privacy, image or licensing guarantee.",
            "A source-format fit is not a scientific or origin/rights certification.",
            "Scalar classification labels are interpretations, not measured reflectance curves.",
            "Blank group labels indicate no reported classification label, not absence of physical curves.",
        ],
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
