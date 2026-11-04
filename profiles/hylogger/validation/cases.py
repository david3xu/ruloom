"""Admitted use case definitions, primary evidence records and consumer verification.

This module provides profile-owned case logic for the research-led workflow:
- validates installed case definitions and reviewed local evidence pack;
- verifies tamper-evident snapshot hashes, safe primary URLs and admission reviews;
- compiles and strictly validates typed case contracts with bounded target rows,
  integer interval feasibility and source pins;
- enforces deterministic source suitability pre-flight before any model call;
- builds bounded source aggregate references without shipping raw source rows;
- separates full code-side reference from rare-label-redacted model summaries;
- enforces downstream practical-use checking (read/filter/group, row accounting,
  per-channel structure presence, and fixed lesson tolerances from agreement);
- provides independent consumer recomputation and full measurement verification.

This is a profile foundation for the research-led workflow, not a runtime CLI
or publication integration (which bind into execute and package in subsequent slices).
Provenance reviewer names and admission notes are recorded metadata claims in
the catalogue, not cryptographic proofs or external certifications of independence.
Catalogue integrity relies on local file hash self-attestation; manifest digest
pinning is future integration.
This module imports no engine and no model code (FP-CODE-2).
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import math
import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..intake.reader import normalize_group, parse_depth

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

ALLOWED_DESIGN_CHOICE_KEYS = frozenset(
    {
        "activity",
        "min_satisfiable_rows",
        "max_supported_rows",
        "default_target_rows",
        "spacing_tolerance_relative_max",
        "valid_fraction_tolerance_absolute_max",
        "answer_rate_tolerance_absolute_max",
        "spacing_definition",
        "answer_rate_denominator",
    }
)

ALLOWED_SOURCE_REQUIREMENT_KEYS = frozenset(
    {
        "required_columns",
        "min_source_rows",
        "require_ordered_finite_depth",
        "require_positive_median_spacing",
        "require_both_inclusion_states",
        "require_both_label_states_in_included",
        "admitted_example_source",
    }
)

ALLOWED_TEMPLATE_KEYS = frozenset(
    {
        "output_columns",
        "recommended_strategy",
        "checks",
        "denominators",
        "flag_encoding",
        "spacing_definition",
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

ADMITTED_PRIMARY_DOMAINS = frozenset(
    {"www.auscope.org.au", "github.com", "wapims.dmp.wa.gov.au"}
)


def _cases_json_path() -> Path:
    return Path(__file__).resolve().parent.parent / "cases.json"


def _validate_safe_url(url: Any) -> None:
    """Validate that evidence URL uses secure https and belongs to admitted primary hosts."""
    if type(url) is not str:
        raise ValueError(f"URL must be string, got {type(url)}")
    if not url.startswith("https://"):
        raise ValueError(f"URL must use secure https protocol, got {url!r}")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"URL scheme must be https, got {parsed.scheme!r}")
    if not parsed.netloc or parsed.netloc not in ADMITTED_PRIMARY_DOMAINS:
        raise ValueError(
            f"URL domain {parsed.netloc!r} not in admitted primary domain allowlist: "
            f"{sorted(ADMITTED_PRIMARY_DOMAINS)}"
        )


def _parse_iso_date(d_str: Any) -> datetime.date:
    """Validate and parse ISO 8601 date YYYY-MM-DD."""
    if type(d_str) is not str or not re.match(r"^\d{4}-\d{2}-\d{2}$", d_str):
        raise ValueError(f"date must be ISO format YYYY-MM-DD, got {d_str!r}")
    try:
        return datetime.date.fromisoformat(d_str)
    except ValueError as err:
        raise ValueError(f"invalid ISO date {d_str!r}: {err}") from err


def _deep_numeric_match(
    a: Any, b: Any, rel_tol: float = 1e-9, abs_tol: float = 1e-7
) -> bool:
    """Deep equality check with explicit numeric tolerance policy for floats."""
    if type(a) is not type(b):
        if (
            isinstance(a, (int, float))
            and isinstance(b, (int, float))
            and not isinstance(a, bool)
            and not isinstance(b, bool)
        ):
            return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
        return False
    if isinstance(a, float):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(a, Mapping):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_deep_numeric_match(a[k], b[k], rel_tol, abs_tol) for k in a)
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_deep_numeric_match(x, y, rel_tol, abs_tol) for x, y in zip(a, b))
    return a == b


def load_cases_catalogue() -> dict[str, Any]:
    """Load and strictly validate the profile's admitted cases catalogue.

    Enforces code-checkable provenance for every evidence entry:
    - strict schema with no unexpected top-level, evidence, or case keys;
    - top-level version must be integer 1;
    - required evidence fields: id, url, title, section, verbatim_passage,
      passage_sha256, supports, does_not_establish, retrieved_date,
      reviewed_date, admission_reviewer, admission_kind;
    - URLs must be secure HTTPS from admitted primary domains;
    - ISO dates must be valid YYYY-MM-DD and reviewed_date >= retrieved_date;
    - passage_sha256 must match sha256 of verbatim_passage;
    - admission_reviewer cannot be implementer;
    - entry id must match its dictionary key;
    - case id must match its dictionary key and version must be integer 1;
    - cases must separate researched_facts, source_licence_evidence,
      design_choices, source_requirements, and contract_template with full typed schemas;
    - primary evidence citations must exist in evidence_pack;
    - tolerance keys in design_choices and contract_template must match frozen bounds.
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
    missing_top = ALLOWED_TOP_KEYS - set(raw.keys())
    if missing_top:
        raise ValueError(
            f"cases catalogue missing required top-level keys: {sorted(missing_top)}"
        )

    cat_version = raw.get("version")
    if type(cat_version) is not int or cat_version != 1:
        raise ValueError(
            f"cases catalogue version must be integer 1, got {cat_version!r}"
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
        missing_e = ALLOWED_EVIDENCE_KEYS - set(entry.keys())
        if missing_e:
            raise ValueError(
                f"evidence entry {eid!r} missing required keys: {sorted(missing_e)}"
            )

        if entry.get("id") != eid:
            raise ValueError(
                f"evidence key {eid!r} differs from entry id {entry.get('id')!r}"
            )

        _validate_safe_url(entry["url"])

        for str_field in ("title", "section", "supports", "admission_kind"):
            val = entry.get(str_field)
            if type(val) is not str or not val.strip():
                raise ValueError(
                    f"evidence entry {eid!r} {str_field} must be non-empty string, got {val!r}"
                )

        passage = entry.get("verbatim_passage")
        if type(passage) is not str or not passage.strip():
            raise ValueError(
                f"evidence entry {eid!r} verbatim_passage must be non-empty string, got {type(passage)}"
            )

        dne = entry.get("does_not_establish")
        if (
            not isinstance(dne, list)
            or not dne
            or not all(type(x) is str and x.strip() for x in dne)
        ):
            raise ValueError(
                f"evidence entry {eid!r} does_not_establish must be non-empty list of non-empty strings"
            )

        actual_hash = hashlib.sha256(passage.encode("utf-8")).hexdigest()
        sha_field = entry.get("passage_sha256")
        if (
            type(sha_field) is not str
            or len(sha_field) != 64
            or not all(c in "0123456789abcdef" for c in sha_field)
            or sha_field != actual_hash
        ):
            raise ValueError(
                f"tampered evidence passage for {eid!r}: hash mismatch "
                f"(expected {sha_field}, got {actual_hash})"
            )

        ret_d = _parse_iso_date(entry.get("retrieved_date"))
        rev_d = _parse_iso_date(entry.get("reviewed_date"))
        if rev_d < ret_d:
            raise ValueError(
                f"evidence entry {eid!r} reviewed_date ({rev_d}) cannot be before retrieved_date ({ret_d})"
            )

        reviewer = entry.get("admission_reviewer")
        if type(reviewer) is not str or not reviewer.strip():
            raise ValueError(
                f"evidence entry {eid!r} admission_reviewer must be non-empty string"
            )
        if "gemini" in reviewer.lower():
            raise ValueError(
                f"evidence entry {eid!r} reviewer cannot be implementer: {reviewer!r}"
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
        missing_c = ALLOWED_CASE_KEYS - set(case_data.keys())
        if missing_c:
            raise ValueError(f"case {cid!r} missing required keys: {sorted(missing_c)}")

        if case_data.get("id") != cid:
            raise ValueError(
                f"case key {cid!r} differs from case id {case_data.get('id')!r}"
            )

        c_version = case_data.get("version")
        if type(c_version) is not int or c_version != 1:
            raise ValueError(
                f"case {cid!r} version must be integer 1, got {c_version!r}"
            )

        c_title = case_data.get("title")
        if type(c_title) is not str or not c_title.strip():
            raise ValueError(f"case {cid!r} title must be non-empty string")

        # Researched facts
        rf = case_data.get("researched_facts")
        if not isinstance(rf, dict):
            raise ValueError(f"case {cid!r} researched_facts must be an object")
        citations = rf.get("primary_evidence_ids")
        if (
            not isinstance(citations, list)
            or not citations
            or not all(type(x) is str and x.strip() for x in citations)
        ):
            raise ValueError(
                f"case {cid!r} primary_evidence_ids must be non-empty list of strings"
            )
        for cite_id in citations:
            if cite_id not in evidence_pack:
                raise ValueError(
                    f"case {cid!r} cites unadmitted evidence id: {cite_id!r}"
                )

        # Design choices
        dc = case_data.get("design_choices")
        if not isinstance(dc, dict):
            raise ValueError(f"case {cid!r} design_choices must be an object")
        extra_dc = set(dc.keys()) - ALLOWED_DESIGN_CHOICE_KEYS
        missing_dc = ALLOWED_DESIGN_CHOICE_KEYS - set(dc.keys())
        if extra_dc or missing_dc:
            raise ValueError(
                f"case {cid!r} design_choices schema mismatch: extra={sorted(extra_dc)}, missing={sorted(missing_dc)}"
            )
        if (
            type(dc["min_satisfiable_rows"]) is not int
            or dc["min_satisfiable_rows"] != 25
        ):
            raise ValueError(f"case {cid!r} min_satisfiable_rows must be integer 25")
        if (
            type(dc["max_supported_rows"]) is not int
            or dc["max_supported_rows"] != 200000
        ):
            raise ValueError(f"case {cid!r} max_supported_rows must be integer 200000")
        if (
            type(dc["valid_fraction_tolerance_absolute_max"]) is not float
            or dc["valid_fraction_tolerance_absolute_max"] != 0.02
        ):
            raise ValueError(
                f"case {cid!r} valid_fraction_tolerance_absolute_max must be 0.02"
            )
        if (
            type(dc["answer_rate_tolerance_absolute_max"]) is not float
            or dc["answer_rate_tolerance_absolute_max"] != 0.02
        ):
            raise ValueError(
                f"case {cid!r} answer_rate_tolerance_absolute_max must be 0.02"
            )
        if (
            type(dc["spacing_tolerance_relative_max"]) is not float
            or dc["spacing_tolerance_relative_max"] != 0.01
        ):
            raise ValueError(
                f"case {cid!r} spacing_tolerance_relative_max must be 0.01"
            )

        # Source requirements
        sr = case_data.get("source_requirements")
        if not isinstance(sr, dict):
            raise ValueError(f"case {cid!r} source_requirements must be an object")
        extra_sr = set(sr.keys()) - ALLOWED_SOURCE_REQUIREMENT_KEYS
        missing_sr = ALLOWED_SOURCE_REQUIREMENT_KEYS - set(sr.keys())
        if extra_sr or missing_sr:
            raise ValueError(
                f"case {cid!r} source_requirements schema mismatch: extra={sorted(extra_sr)}, missing={sorted(missing_sr)}"
            )
        for bool_key in (
            "require_ordered_finite_depth",
            "require_positive_median_spacing",
            "require_both_inclusion_states",
            "require_both_label_states_in_included",
        ):
            if type(sr[bool_key]) is not bool:
                raise ValueError(
                    f"case {cid!r} source_requirements {bool_key} must be bool"
                )

        # Contract template
        ct = case_data.get("contract_template")
        if not isinstance(ct, dict):
            raise ValueError(f"case {cid!r} contract_template must be an object")
        extra_ct = set(ct.keys()) - ALLOWED_TEMPLATE_KEYS
        missing_ct = ALLOWED_TEMPLATE_KEYS - set(ct.keys())
        if extra_ct or missing_ct:
            raise ValueError(
                f"case {cid!r} contract_template schema mismatch: extra={sorted(extra_ct)}, missing={sorted(missing_ct)}"
            )
        checks = ct.get("checks")
        if not isinstance(checks, dict):
            raise ValueError(f"case {cid!r} contract_template checks must be an object")
        for ck, expected_t, expected_val in (
            ("exact_rows", bool, True),
            ("valid_binary", bool, True),
            ("depth_finite_ordered_nonnegative", bool, True),
            ("relative_median_spacing_difference_max", float, 0.01),
            ("absolute_valid_fraction_difference_max", float, 0.02),
            ("absolute_answer_rate_difference_max", float, 0.02),
            ("nonblank_labels_source_supported", bool, True),
            ("downstream_read_filter_group_accounts_for_rows", bool, True),
            ("downstream_structure_both_states_present", bool, True),
        ):
            if ck not in checks:
                raise ValueError(f"case {cid!r} contract_template checks missing {ck}")
            if type(checks[ck]) is not expected_t or checks[ck] != expected_val:
                raise ValueError(
                    f"case {cid!r} check {ck!r} loosened or wrong type: expected {expected_val!r} ({expected_t.__name__}), "
                    f"got {checks[ck]!r} ({type(checks[ck]).__name__})"
                )

        # Limitations
        limits = case_data.get("limitations")
        if (
            not isinstance(limits, list)
            or not limits
            or not all(type(x) is str and x.strip() for x in limits)
        ):
            raise ValueError(
                f"case {cid!r} limitations must be non-empty list of non-empty strings"
            )

    return raw


def get_case(case_id_or_alias: str) -> dict[str, Any]:
    """Return the admitted case definition by id or registered alias."""
    if not isinstance(case_id_or_alias, str):
        raise ValueError(
            f"case_id_or_alias must be string, got {type(case_id_or_alias).__name__}"
        )
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
    """Check if target_rows can jointly satisfy inclusion, answer rates and blank presence.

    Uses integer interval arithmetic: for each channel requiring both answered
    and blank states, intersects the valid answer rate tolerance interval
    [k*(r - tol), k*(r + tol)] with the integer structural presence range
    [1, k - 1] to check whether a feasible integer count exists in O(1) time.
    """
    validate_source_reference(source_reference)
    if type(target_rows) is not int or isinstance(target_rows, bool):
        raise TypeError(f"target_rows must be integer, got {type(target_rows)}")

    design = case_def["design_choices"]
    vf_tol = float(design["valid_fraction_tolerance_absolute_max"])
    swir_tol = float(design["answer_rate_tolerance_absolute_max"])
    tir_tol = float(design["answer_rate_tolerance_absolute_max"])

    vf = float(source_reference["valid_fraction"])
    swir_rate = float(source_reference["swir_answer_rate"])
    tir_rate = float(source_reference["tir_answer_rate"])

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

    # Check each channel using interval arithmetic
    for channel, rate, tol in (
        ("SWIR", swir_rate, swir_tol),
        ("TIR", tir_rate, tir_tol),
    ):
        req_answered = rate > 0.0
        req_blanks = rate < 1.0

        if req_answered and req_blanks:
            if k < 2:
                return (
                    False,
                    f"target row count {target_rows} gives k={k} valid rows, but {channel} "
                    "requires at least 1 answered and 1 blank valid row (needs k >= 2)",
                )
            a_min = 1
            a_max = k - 1
        elif req_answered and not req_blanks:
            a_min = k
            a_max = k
        elif not req_answered and req_blanks:
            a_min = 0
            a_max = 0
        else:
            a_min = 0
            a_max = k

        # Integer continuous bounds for |a/k - rate| <= tol
        low = max(a_min, math.ceil(k * (rate - tol) - 1e-12))
        high = min(a_max, math.floor(k * (rate + tol) + 1e-12))
        if low > high:
            max_rate_with_blank = (k - 1) / k if k > 0 else 0.0
            return (
                False,
                f"target row count {target_rows} (k={k} valid rows) cannot jointly satisfy "
                f"{channel} answer rate ({rate:.4f} +/- {tol}) and required blank presence "
                f"(feasible integer range empty: low={low} > high={high}; "
                f"max rate with at least one blank is {k - 1}/{k} = {max_rate_with_blank:.4f})",
            )

    return True, None


def compute_minimum_feasible_rows(
    case_def: Mapping[str, Any],
    source_reference: Mapping[str, Any],
) -> int:
    """Find smallest target row count >= floor that is jointly satisfiable for this source.

    Searches across [min_satisfiable_rows, max_supported_rows] using O(1) interval arithmetic.
    Refuses with an explicit ValueError if no feasible row count exists within supported bounds.
    """
    validate_source_reference(source_reference)
    design = case_def["design_choices"]
    floor = int(design["min_satisfiable_rows"])
    ceiling = int(design["max_supported_rows"])

    for n in range(floor, ceiling + 1):
        ok, _ = check_source_joint_satisfiability(n, case_def, source_reference)
        if ok:
            return n

    raise ValueError(
        f"no jointly feasible row count exists within supported bounds [{floor}, {ceiling}] "
        f"for this source (valid_fraction={source_reference.get('valid_fraction')})"
    )


def validate_plan_satisfiability(
    n_rows: int,
    case_def: Mapping[str, Any],
    source_reference: Mapping[str, Any] | None = None,
) -> None:
    """Validate that requested row count satisfies tolerance floors at contract time."""
    if type(n_rows) is not int or isinstance(n_rows, bool):
        raise TypeError(f"target row count must be an integer, got {type(n_rows)}")
    design = case_def["design_choices"]
    floor = int(design["min_satisfiable_rows"])
    ceiling = int(design["max_supported_rows"])
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
        validate_source_reference(source_reference)
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
    - Locks canonical case checks, denominators, and thresholds from installed case definition.
    - Resulting contract shape is strictly typed with no extra keys.
    """
    if (
        type(source_sha256) is not str
        or len(source_sha256) != 64
        or not all(c in "0123456789abcdef" for c in source_sha256)
    ):
        raise ValueError(
            f"source_sha256 must be a 64-char lowercase hex string, got {source_sha256!r}"
        )

    if source_reference is not None:
        validate_source_reference(source_reference)
        ref_sha = source_reference.get("source_sha256")
        if ref_sha != source_sha256:
            raise ValueError(
                f"source_sha256 pin mismatch with source_reference: "
                f"{source_sha256} vs {ref_sha}"
            )

    validate_plan_satisfiability(target_rows, case_def, source_reference)

    template = case_def["contract_template"]
    canonical_checks = dict(template["checks"])
    canonical_checks["exact_rows"] = target_rows

    contract = {
        "version": 1,
        "case_id": case_def["id"],
        "target_rows": target_rows,
        "source_sha256": source_sha256,
        "output_columns": list(template["output_columns"]),
        "recommended_strategy": template["recommended_strategy"],
        "checks": canonical_checks,
        "denominators": dict(template["denominators"]),
        "flag_encoding": template["flag_encoding"],
        "spacing_definition": template["spacing_definition"],
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
        raise ValueError(f"contract must be a mapping, got {type(contract).__name__}")

    contract_keys = set(contract.keys())
    missing = ALLOWED_CONTRACT_KEYS - contract_keys
    if missing:
        raise ValueError(f"contract missing required keys: {sorted(missing)}")
    extra = contract_keys - ALLOWED_CONTRACT_KEYS
    if extra:
        raise ValueError(f"contract has unexpected keys: {sorted(extra)}")

    c_version = contract.get("version")
    if type(c_version) is not int or c_version != 1:
        raise ValueError(f"contract version must be integer 1, got {c_version!r}")

    if contract.get("case_id") != case_def.get("id"):
        raise ValueError(
            f"contract case_id mismatch: expected {case_def.get('id')}, "
            f"got {contract.get('case_id')}"
        )

    target_rows = contract.get("target_rows")
    if type(target_rows) is not int or isinstance(target_rows, bool):
        raise ValueError(
            f"contract target_rows must be integer, got {type(target_rows)}"
        )
    validate_plan_satisfiability(target_rows, case_def, source_reference)

    source_sha = contract.get("source_sha256")
    if (
        type(source_sha) is not str
        or len(source_sha) != 64
        or not all(c in "0123456789abcdef" for c in source_sha)
    ):
        raise ValueError(
            f"contract source_sha256 must be a valid 64-char lowercase hex string, got {source_sha!r}"
        )

    if source_reference is not None:
        validate_source_reference(source_reference)
        ref_sha = source_reference.get("source_sha256")
        if ref_sha != source_sha:
            raise ValueError(
                f"contract source_sha256 mismatch with reference: "
                f"{source_sha} vs {ref_sha}"
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
    contract_checks = contract.get("checks")
    if not isinstance(contract_checks, Mapping) or set(contract_checks.keys()) != set(
        canonical_checks.keys()
    ):
        raise ValueError(
            f"contract checks mismatch: expected canonical {canonical_checks}, "
            f"got {contract_checks}"
        )
    for ck, expected_v in canonical_checks.items():
        actual_v = contract_checks[ck]
        if type(actual_v) is not type(expected_v) or actual_v != expected_v:
            raise ValueError(
                f"contract check {ck!r} type/value mismatch: expected {expected_v!r} ({type(expected_v).__name__}), "
                f"got {actual_v!r} ({type(actual_v).__name__})"
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
      negative, malformed depth cell, or invalid Final_Mask (not '0' or '1')
      is rejected.
    - Detects CSV row width errors (extra or missing values).
    - Accepts file paths, sequences of dicts, or sequences of tuples.
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
    reqs = case_def["source_requirements"]
    required_cols = reqs.get("required_columns", ["StartDepth", "Final_Mask"])
    min_rows = reqs.get("min_source_rows", 20)

    source_sha: str | None = None
    headers: list[str] = []
    raw_count = 0
    rejected_count = 0
    parsed_rows: list[tuple[float, bool, str | None, str | None]] = []

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
            reader = csv.reader(f)
            header_row = next(reader, None)
            if not header_row:
                headers = []
            else:
                headers = [h.strip() for h in header_row]
            expected_width = len(headers)

            missing_cols = [c for c in required_cols if c not in headers]
            if missing_cols:
                reasons.append(
                    f"missing required source columns: {', '.join(missing_cols)}"
                )
                return {
                    "suitable": False,
                    "source_sha256": source_sha,
                    "source_rows": 0,
                    "usable_rows": 0,
                    "rejected_rows": 0,
                    "assessed_columns": headers,
                    "reasons": reasons,
                    "findings": "Source lacks required columns.",
                    "licence_status": "rights unknown",
                    "disclaimer": "A source-format fit is not a scientific or origin/rights certification.",
                }

            for line in reader:
                raw_count += 1
                if len(line) != expected_width:
                    rejected_count += 1
                    continue
                row_dict = dict(zip(headers, line))
                depth_val = parse_depth(row_dict.get("StartDepth"))
                if depth_val is None or not math.isfinite(depth_val) or depth_val < 0:
                    rejected_count += 1
                    continue
                mask_str = str(row_dict.get("Final_Mask", "")).strip()
                if mask_str not in {"0", "1"}:
                    rejected_count += 1
                    continue
                is_valid = mask_str == "1"
                swir = normalize_group(row_dict.get("Grp1_sTSAS"))
                tir = normalize_group(row_dict.get("Grp1_sjCLST"))
                parsed_rows.append((depth_val, is_valid, swir, tir))
    else:
        # Sequence in memory (list of dicts or list of tuples)
        raw_items = list(source_path_or_rows)
        repr_bytes = json.dumps(raw_items, sort_keys=True, default=str).encode("utf-8")
        source_sha = hashlib.sha256(repr_bytes).hexdigest()

        if expected_source_sha256 and source_sha != expected_source_sha256:
            reasons.append(
                f"source sha256 mismatch (expected {expected_source_sha256}, got {source_sha})"
            )

        if not raw_items:
            headers = list(required_cols)
        elif isinstance(raw_items[0], Mapping):
            headers = list(raw_items[0].keys())
        else:
            headers = ["StartDepth", "Final_Mask", "Grp1_sTSAS", "Grp1_sjCLST"]

        expected_width = len(headers)
        missing_cols = [c for c in required_cols if c not in headers]
        if missing_cols:
            reasons.append(
                f"missing required source columns: {', '.join(missing_cols)}"
            )
            return {
                "suitable": False,
                "source_sha256": source_sha,
                "source_rows": len(raw_items),
                "usable_rows": 0,
                "rejected_rows": len(raw_items),
                "assessed_columns": headers,
                "reasons": reasons,
                "findings": "Source lacks required columns.",
                "licence_status": "rights unknown",
                "disclaimer": "A source-format fit is not a scientific or origin/rights certification.",
            }

        for item in raw_items:
            raw_count += 1
            if isinstance(item, Mapping):
                if len(item) != expected_width:
                    rejected_count += 1
                    continue
                row_dict = dict(item)
            elif isinstance(item, (tuple, list)):
                if len(item) != expected_width:
                    rejected_count += 1
                    continue
                row_dict = dict(zip(headers, item))
            else:
                rejected_count += 1
                continue

            depth_val = parse_depth(row_dict.get("StartDepth"))
            if depth_val is None or not math.isfinite(depth_val) or depth_val < 0:
                rejected_count += 1
                continue
            mask_str = str(row_dict.get("Final_Mask", "")).strip()
            if mask_str not in {"0", "1"}:
                rejected_count += 1
                continue
            is_valid = mask_str == "1"
            swir = normalize_group(row_dict.get("Grp1_sTSAS"))
            tir = normalize_group(row_dict.get("Grp1_sjCLST"))
            parsed_rows.append((depth_val, is_valid, swir, tir))

    if raw_count < min_rows:
        reasons.append(
            f"source row count {raw_count} is below minimum requirement {min_rows}"
        )

    if rejected_count > 0:
        reasons.append(
            f"source contains {rejected_count} malformed, non-finite, negative depth or invalid mask rows "
            f"(raw: {raw_count}, usable: {len(parsed_rows)}, rejected: {rejected_count}); "
            "admitted case requires 100% clean finite depths and binary masks"
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
    """Validate that a source reference dictionary adheres strictly to schema and consistency rules."""
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

    ref_version = source_reference.get("version")
    if type(ref_version) is not int or ref_version != 1:
        raise ValueError(
            f"source reference version must be integer 1, got {ref_version!r}"
        )

    sha = source_reference.get("source_sha256")
    if (
        type(sha) is not str
        or len(sha) != 64
        or not all(c in "0123456789abcdef" for c in sha)
    ):
        raise ValueError(
            f"source reference source_sha256 must be 64-char lowercase hex, got {sha!r}"
        )

    s_rows = source_reference.get("source_rows")
    u_rows = source_reference.get("usable_rows")
    r_rows = source_reference.get("rejected_rows")

    if type(s_rows) is not int or isinstance(s_rows, bool) or s_rows <= 0:
        raise ValueError(
            f"source reference source_rows must be positive integer, got {s_rows!r}"
        )
    if (
        type(u_rows) is not int
        or isinstance(u_rows, bool)
        or u_rows <= 0
        or u_rows > s_rows
    ):
        raise ValueError(
            f"source reference usable_rows must be positive integer <= source_rows, got {u_rows!r}"
        )
    if type(r_rows) is not int or isinstance(r_rows, bool) or r_rows < 0:
        raise ValueError(
            f"source reference rejected_rows must be non-negative integer, got {r_rows!r}"
        )
    if u_rows + r_rows != s_rows:
        raise ValueError(
            f"source reference row accounting mismatch: usable ({u_rows}) + "
            f"rejected ({r_rows}) != source_rows ({s_rows})"
        )

    vf = source_reference.get("valid_fraction")
    if type(vf) is not float or not math.isfinite(vf) or not (0.0 < vf < 1.0):
        raise ValueError(
            f"source reference valid_fraction must be finite float in (0, 1), got {vf!r}"
        )

    for rate_key in ("swir_answer_rate", "tir_answer_rate"):
        r = source_reference.get(rate_key)
        if type(r) is not float or not math.isfinite(r) or not (0.0 <= r <= 1.0):
            raise ValueError(
                f"source reference {rate_key} must be finite float in [0, 1], got {r!r}"
            )

    sp = source_reference.get("median_forward_spacing")
    if type(sp) is not float or not math.isfinite(sp) or sp <= 0:
        raise ValueError(
            f"source reference median_forward_spacing must be positive finite float, got {sp!r}"
        )

    swir_labels = source_reference.get("disclosed_swir_labels")
    if not isinstance(swir_labels, list) or not all(
        type(item) is str and item.strip() for item in swir_labels
    ):
        raise ValueError(
            "source reference disclosed_swir_labels must be list of non-empty strings"
        )
    if len(set(swir_labels)) != len(swir_labels):
        raise ValueError("source reference disclosed_swir_labels contains duplicates")
    if float(source_reference["swir_answer_rate"]) > 0.0 and len(swir_labels) == 0:
        raise ValueError(
            "source reference disclosed_swir_labels cannot be empty when swir_answer_rate > 0"
        )

    tir_labels = source_reference.get("disclosed_tir_labels")
    if not isinstance(tir_labels, list) or not all(
        type(item) is str and item.strip() for item in tir_labels
    ):
        raise ValueError(
            "source reference disclosed_tir_labels must be list of non-empty strings"
        )
    if len(set(tir_labels)) != len(tir_labels):
        raise ValueError("source reference disclosed_tir_labels contains duplicates")
    if float(source_reference["tir_answer_rate"]) > 0.0 and len(tir_labels) == 0:
        raise ValueError(
            "source reference disclosed_tir_labels cannot be empty when tir_answer_rate > 0"
        )

    has_blanks = source_reference.get("has_blanks")
    if type(has_blanks) is not bool:
        raise ValueError("source reference has_blanks must be boolean")
    if (
        float(source_reference["swir_answer_rate"]) < 1.0
        or float(source_reference["tir_answer_rate"]) < 1.0
    ) and not has_blanks:
        raise ValueError(
            "source reference has_blanks must be True when answer rates < 1.0"
        )

    lic = source_reference.get("licence_status")
    if type(lic) is not str or not lic.strip():
        raise ValueError("source reference licence_status must be non-empty string")

    notice = source_reference.get("notice")
    if type(notice) is not str or not notice.strip():
        raise ValueError("source reference notice must be non-empty string")


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

    aggs = suit["aggregates"]
    ref = {
        "version": 1,
        "source_sha256": suit["source_sha256"],
        "source_rows": suit["source_rows"],
        "usable_rows": suit["usable_rows"],
        "rejected_rows": suit["rejected_rows"],
        "valid_fraction": float(aggs["valid_fraction"]),
        "median_forward_spacing": float(aggs["median_forward_spacing"]),
        "swir_answer_rate": float(aggs["swir_answer_rate"]),
        "tir_answer_rate": float(aggs["tir_answer_rate"]),
        "disclosed_swir_labels": list(aggs["disclosed_swir_labels"]),
        "disclosed_tir_labels": list(aggs["disclosed_tir_labels"]),
        "has_blanks": bool(aggs["has_blanks"]),
        "licence_status": suit["licence_status"],
        "notice": (
            "Bounded aggregate reference for code-side utility checking and verification. "
            "Model-disclosed summaries apply a frequency floor (MIN_LABEL_COUNT_FOR_MODEL_VIEW = 2) "
            "to redact singletons; code-side utility checking uses the full code-side vocabulary "
            "disclosed in this reference. Contains no raw measurements or individual source rows."
        ),
    }
    validate_source_reference(ref)
    return ref


def build_disclosed_model_summary(
    source_reference: Mapping[str, Any],
) -> dict[str, Any]:
    """Return model-facing summary with rare singleton labels redacted."""
    validate_source_reference(source_reference)
    return {
        "version": 1,
        "valid_fraction": source_reference["valid_fraction"],
        "median_forward_spacing": source_reference["median_forward_spacing"],
        "swir_answer_rate": source_reference["swir_answer_rate"],
        "tir_answer_rate": source_reference["tir_answer_rate"],
        "has_blanks": source_reference["has_blanks"],
        "licence_status": source_reference["licence_status"],
        "disclosed_swir_labels": list(source_reference["disclosed_swir_labels"]),
        "disclosed_tir_labels": list(source_reference["disclosed_tir_labels"]),
        "notice": (
            "Model-facing disclosed summary. Singletons are withheld from the model view "
            "per disclosure rules; code-side utility checking retains the full code-side reference."
        ),
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
    - structure presence of both inclusion states and blank labels per channel.
    """
    # 1. Resolve and validate source reference
    if isinstance(source_reference_or_path, Mapping):
        source_ref = dict(source_reference_or_path)
    else:
        source_ref = build_source_reference(source_reference_or_path)
    validate_source_reference(source_ref)

    # 2. Strict contract validation
    if not isinstance(contract, Mapping):
        raise ValueError(f"contract must be a mapping, got {type(contract).__name__}")
    case_id = contract.get("case_id")
    if not isinstance(case_id, str):
        raise ValueError(
            f"contract case_id must be a string, got {type(case_id).__name__}"
        )
    try:
        case_def = get_case(case_id)
    except KeyError as err:
        raise ValueError(f"unknown case_id in contract: {err}") from err
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

    def check(name: str, condition: bool, measurement: Any) -> None:
        measurements[name] = measurement
        if not condition:
            failures.append(name)

    # Decode bytes safely
    try:
        text = candidate_bytes.decode("utf-8")
    except UnicodeDecodeError as err:
        return {
            "passed": False,
            "failures": ["invalid_utf8_encoding"],
            "measurements": {},
            "candidate_sha256": candidate_sha,
            "limitations": list(case_def.get("limitations", [])),
            "detail": f"candidate bytes are not valid UTF-8: {err}",
        }

    # 4. Source pin check
    expected_src_sha = contract.get("source_sha256")
    ref_sha = source_ref.get("source_sha256")
    check(
        "source_pin_match",
        expected_src_sha == ref_sha,
        {"contract_pin": expected_src_sha, "reference_pin": ref_sha},
    )

    # 5. CSV structure and row parsing
    raw_reader = csv.reader(io.StringIO(text))
    header_row = next(raw_reader, [])
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
        depth_str = row.get("depth_m", "")
        if (
            not isinstance(depth_str, str)
            or " " in depth_str
            or "\t" in depth_str
            or "_" in depth_str
            or not depth_str
        ):
            well_formed_ok = False
            continue
        try:
            x = float(depth_str)
            if not math.isfinite(x) or x < 0:
                well_formed_ok = False
                continue
        except ValueError:
            well_formed_ok = False
            continue

        flag_raw = row.get("valid", "")
        if flag_raw not in {"true", "false", "0", "1"}:
            well_formed_ok = False
            continue
        is_valid = flag_raw in {"true", "1"}
        sw = row.get("swir_group") or None
        ti = row.get("tir_group") or None
        normalized.append((x, is_valid, sw, ti))

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

    # Design choice limits
    design = case_def["design_choices"]
    spacing_limit = float(design["spacing_tolerance_relative_max"])
    vf_limit = float(design["valid_fraction_tolerance_absolute_max"])
    rate_limit = float(design["answer_rate_tolerance_absolute_max"])

    # 7. Median forward spacing (even-length averaged)
    diffs = [b[0] - a[0] for a, b in zip(normalized, normalized[1:]) if b[0] > a[0]]
    diffs.sort()
    cand_sp = float(statistics.median(diffs)) if diffs else None
    src_sp = float(source_ref["median_forward_spacing"])
    spacing_delta = (
        abs(cand_sp - src_sp) / src_sp if (cand_sp is not None and src_sp > 0) else None
    )
    check(
        "depth_spacing",
        spacing_delta is not None and spacing_delta <= spacing_limit,
        {
            "source_reference": src_sp,
            "candidate": cand_sp,
            "relative_difference": spacing_delta,
            "limit": spacing_limit,
            "definition": "median of positive adjacent differences (averaged for even count)",
        },
    )

    # 8. Inclusion fraction
    src_vf = float(source_ref["valid_fraction"])
    cand_vf = sum(1 for r in normalized if r[1]) / len(normalized)
    vf_delta = abs(cand_vf - src_vf)
    check(
        "inclusion_fraction",
        vf_delta <= vf_limit,
        {
            "source_reference": src_vf,
            "candidate": cand_vf,
            "absolute_difference": vf_delta,
            "limit": vf_limit,
        },
    )

    # 9. Channel answer rates (included rows only)
    cvalid = [r for r in normalized if r[1]]
    for i, name in ((2, "swir"), (3, "tir")):
        src_rate = float(source_ref[f"{name}_answer_rate"])
        cand_rate = (
            sum(1 for r in cvalid if r[i] is not None) / len(cvalid) if cvalid else None
        )
        delta = abs(cand_rate - src_rate) if (cand_rate is not None) else None
        check(
            f"{name}_answer_rate",
            delta is not None and delta <= rate_limit,
            {
                "source_reference": src_rate,
                "candidate": cand_rate,
                "absolute_difference": delta,
                "limit": rate_limit,
                "denominator": "included rows only",
            },
        )

    # 10. Nonblank supported vocabulary across ALL rows
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

    # 12. Structure presence per channel and inclusion states
    swir_rate = float(source_ref["swir_answer_rate"])
    tir_rate = float(source_ref["tir_answer_rate"])

    swir_answered = sum(1 for r in cvalid if r[2] is not None)
    swir_blanks = sum(1 for r in cvalid if r[2] is None)
    tir_answered = sum(1 for r in cvalid if r[3] is not None)
    tir_blanks = sum(1 for r in cvalid if r[3] is None)

    structure_ok = (included_count > 0) and (excluded_count > 0)
    structure_detail: list[str] = []

    if included_count == 0 or excluded_count == 0:
        structure_detail.append("both included and excluded rows required")

    if swir_rate < 1.0:
        if swir_blanks == 0:
            structure_ok = False
            structure_detail.append("SWIR missing required blank labels in valid rows")
        if swir_answered == 0:
            structure_ok = False
            structure_detail.append(
                "SWIR missing required answered labels in valid rows"
            )

    if tir_rate < 1.0:
        if tir_blanks == 0:
            structure_ok = False
            structure_detail.append("TIR missing required blank labels in valid rows")
        if tir_answered == 0:
            structure_ok = False
            structure_detail.append(
                "TIR missing required answered labels in valid rows"
            )

    check(
        "downstream_structure_both_states_present",
        structure_ok,
        {
            "included_rows": included_count,
            "excluded_rows": excluded_count,
            "swir_blanks_in_valid": swir_blanks,
            "tir_blanks_in_valid": tir_blanks,
            "swir_answered_in_valid": swir_answered,
            "tir_answered_in_valid": tir_answered,
            "structure_detail": structure_detail,
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

    A reported PASS over failing data, tampered measurements, altered limitations,
    missing measurement records, extra keys, or forged receipts is detected and refused.
    Does NOT require raw source rows when given a valid source aggregate reference.
    """
    recomputed = evaluate_case_utility(
        candidate_path_or_bytes, source_reference_or_path, contract
    )

    if reported_utility is None:
        return {
            "case_verified": False,
            "recomputed": recomputed,
            "matches_reported": "not_compared",
            "discrepancies": ["no reported_utility provided for comparison"],
        }

    matches_reported = True
    discrepancies: list[str] = []

    if not isinstance(reported_utility, Mapping):
        return {
            "case_verified": False,
            "recomputed": recomputed,
            "matches_reported": False,
            "discrepancies": ["reported_utility must be a mapping"],
        }

    # 1. Require equal top-level key sets
    expected_top_keys = set(recomputed.keys())
    reported_top_keys = set(reported_utility.keys())
    if reported_top_keys != expected_top_keys:
        matches_reported = False
        extra = sorted(reported_top_keys - expected_top_keys)
        missing = sorted(expected_top_keys - reported_top_keys)
        discrepancies.append(
            f"top-level keys mismatch: extra={extra}, missing={missing}"
        )

    # 2. Compare passed and failures
    if reported_utility.get("passed") != recomputed["passed"]:
        matches_reported = False
        discrepancies.append(
            f"verdict mismatch: reported {reported_utility.get('passed')}, "
            f"recomputed {recomputed['passed']}"
        )

    if reported_utility.get("failures") != recomputed["failures"]:
        matches_reported = False
        discrepancies.append(
            f"failures mismatch: reported {reported_utility.get('failures')}, "
            f"recomputed {recomputed['failures']}"
        )

    # 3. Compare candidate_sha256
    if reported_utility.get("candidate_sha256") != recomputed["candidate_sha256"]:
        matches_reported = False
        discrepancies.append(
            f"candidate_sha256 mismatch: reported {reported_utility.get('candidate_sha256')}, "
            f"recomputed {recomputed['candidate_sha256']}"
        )

    # 4. Compare limitations
    if reported_utility.get("limitations") != recomputed["limitations"]:
        matches_reported = False
        discrepancies.append(
            f"limitations mismatch: reported {reported_utility.get('limitations')}, "
            f"recomputed {recomputed['limitations']}"
        )

    # 5. Canonically compare required measurements
    reported_meas = reported_utility.get("measurements")
    recomp_meas = recomputed["measurements"]

    if not isinstance(reported_meas, Mapping) or not reported_meas:
        matches_reported = False
        discrepancies.append("reported measurements missing, empty, or non-mapping")
    else:
        if set(reported_meas.keys()) != set(recomp_meas.keys()):
            matches_reported = False
            missing_m = sorted(set(recomp_meas.keys()) - set(reported_meas.keys()))
            extra_m = sorted(set(reported_meas.keys()) - set(recomp_meas.keys()))
            discrepancies.append(
                f"measurement keys mismatch: missing {missing_m}, unexpected {extra_m}"
            )
        else:
            for k, recomp_val in recomp_meas.items():
                rep_val = reported_meas[k]
                if not _deep_numeric_match(rep_val, recomp_val):
                    matches_reported = False
                    discrepancies.append(
                        f"measurement value mismatch for {k!r}: "
                        f"reported {rep_val!r}, recomputed {recomp_val!r}"
                    )
                    break

    case_verified = bool(recomputed["passed"] and (matches_reported is True))
    return {
        "case_verified": case_verified,
        "recomputed": recomputed,
        "matches_reported": matches_reported,
        "discrepancies": discrepancies,
    }
