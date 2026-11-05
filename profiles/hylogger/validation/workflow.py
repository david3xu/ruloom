"""Pure profile-owned case workflow adapter for HyLogger teaching cases.

This module implements the profile-side workflow logic for research-led cases:
1. prepare_case_context: deterministic source suitability, installed catalogue
   pin verification, double-read hash guard, canonical contract compilation,
   target feasibility check, and threshold/checker snapshot binding;
2. evaluate_case_context: evaluation of candidate bytes under validated context;
3. case_check_results: maps utility report measurements to all 13 namespaced
   case.* engine check items every time (fails closed on inconsistent or malformed reports);
4. build_case_members: packages case-binding.json, case-contract.json,
   case-reference.json, case-utility.json, and case-evidence.json for release;
5. verify_case_members: consumer verification comparing full installed bindings,
   recomputing candidate checks, matching expected cases, and verifying source
   derivation (or explicitly reporting aggregate consistency only);
6. repair_case_plan: bounded same-seed, same-target causal strategy move
   (fitted_markov_joint -> fitted_markov_balanced) when inclusion check fails;
7. research_case: bounded advisory AI role selecting/explaining admitted evidence;
8. assess_case_source: bounded advisory AI role assessing disclosed aggregates;
9. review_case_candidate: bounded advisory AI role providing operator recommendations.

Layer boundaries:
This module imports neither engine nor concrete model transports (FP-CODE-2).
No sha256 helper functions are defined here (FP-CODE-3).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import jsonschema

from .cases import (
    REVIEWED_CASES_CATALOGUE_SHA256,
    assess_source_suitability,
    build_disclosed_model_summary,
    build_source_reference,
    check_source_joint_satisfiability,
    compile_case_contract,
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

#: The 13 known namespaced check IDs for HyLogger case utility checks.
CASE_MEASUREMENT_KEYS: tuple[str, ...] = (
    "source_pin_match",
    "output_columns",
    "row_count",
    "well_formed_rows",
    "ordered_depth",
    "depth_spacing",
    "inclusion_fraction",
    "swir_answer_rate",
    "tir_answer_rate",
    "swir_supported_labels",
    "tir_supported_labels",
    "downstream_filter_group",
    "downstream_structure_both_states_present",
)

CASE_CHECK_IDS: tuple[str, ...] = tuple(f"case.{k}" for k in CASE_MEASUREMENT_KEYS)

#: Known numeric measurement dotted paths for structured evidence facts.
EVIDENCE_FACT_MEASUREMENT_PATHS: tuple[str, ...] = (
    "row_count",
    "downstream_filter_group.total_rows",
    "downstream_filter_group.included",
    "downstream_filter_group.excluded",
    "downstream_filter_group.group_count",
    "inclusion_fraction.candidate",
    "inclusion_fraction.source_reference",
    "inclusion_fraction.absolute_difference",
    "depth_spacing.candidate",
    "depth_spacing.source_reference",
    "depth_spacing.relative_difference",
    "swir_answer_rate.candidate",
    "swir_answer_rate.source_reference",
    "swir_answer_rate.absolute_difference",
    "tir_answer_rate.candidate",
    "tir_answer_rate.source_reference",
    "tir_answer_rate.absolute_difference",
    "downstream_structure_both_states_present.included_rows",
    "downstream_structure_both_states_present.excluded_rows",
)

#: Safe member file names produced by build_case_members.
CASE_MEMBER_NAMES: frozenset[str] = frozenset(
    {
        "case-binding.json",
        "case-contract.json",
        "case-reference.json",
        "case-utility.json",
        "case-evidence.json",
    }
)

#: Required semantic fields in case context binding.
ALLOWED_CONTEXT_BINDING_KEYS: frozenset[str] = frozenset(
    {
        "case_id",
        "case_version",
        "catalogue_sha256",
        "contract_sha256",
        "reference_sha256",
        "source_sha256",
        "thresholds_sha256",
        "checker_sha256",
        "evidence_sha256",
        "source_fit_sha256",
    }
)

#: Required semantic fields in case-binding.json.
ALLOWED_BINDING_KEYS: frozenset[str] = frozenset(
    (ALLOWED_CONTEXT_BINDING_KEYS - {"source_fit_sha256"})
    | {
        "candidate_sha256",
        "spec_digest",
    }
)

#: Unsupported claims for the research advisor.
UNSUPPORTED_DOMAINS: tuple[str, ...] = (
    "geological prediction",
    "mineral reserve",
    "mineral grade",
    "ore grade",
    "predict mineral",
    "predict ore",
    "predict grade",
    "reflectance curve",
    "core photograph",
    "core photo",
    "privacy guarantee",
    "guarantee anonymity",
    "anonymity",
    "anonymised",
    "anonymized",
    "universal licence",
    "universal license",
    "live web search",
    "new source admission",
)

#: Prohibited prose assertions across AI advisory roles.
PROHIBITED_PROSE_PATTERNS: tuple[str, ...] = (
    "privacy guaranteed",
    "guarantees privacy",
    "anonymised",
    "anonymized",
    "anonymity",
    "public domain",
    "safe to publish",
    "safe to republish",
    "cc-0",
    "cc0",
    "predicts ore",
    "predict ore",
    "predicts grade",
    "predict grade",
    "predict mineral",
    "predicts mineral",
    "ore bodies",
    "ore body",
    "mineral reserve",
    "scientifically validated",
    "scientific validation",
    "universal licence",
    "universal license",
)

HEX64_LOWERCASE_RE = re.compile(r"^[0-9a-f]{64}$")

#: A SHA-256 digest token inside lowercased prose (bounded by characters
#: that are not ASCII letters or digits). Digests are identifiers, never
#: claims, and an admitted passage digest contains a screened substring as
#: hex. A pattern occurrence is exempt only when its whole span lies inside
#: one such token; an occurrence that merely touches or straddles a digest
#: (for example ``cc-0`` split by the hyphen) is still rejected.
_PROSE_DIGEST_TOKEN_RE = re.compile(r"(?<![0-9a-z])[0-9a-f]{64}(?![0-9a-z])")


def prohibited_prose_contract(fields: Sequence[str]) -> str:
    """Prompt clause publishing the exact prose screen a role's answer must meet.

    The screen itself is unchanged and lexical: any listed term in a
    screened field rejects the whole answer, including inside a negated
    sentence or a disclaimer. Telling the role this up front is an output
    contract, not a relaxation; code still applies every pattern.
    """
    names = ", ".join(fields)
    terms = "; ".join(repr(t) for t in PROHIBITED_PROSE_PATTERNS)
    return (
        f"\nOutput prose contract: code screens {names} case-insensitively "
        "and rejects the entire answer if any of these terms appears anywhere, "
        "even in a negated sentence, a disclaimer or a quotation of the user "
        f"request: {terms}. Do not use those terms in those fields. "
        "The installed case's fixed limitations, which code records in the "
        "release, already state the privacy, licensing and scientific-validity "
        "non-claims, so do not restate them."
    )


@runtime_checkable
class StructuredModel(Protocol):
    """Protocol for injected model completion without concrete transports."""

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Any:
        """Produce structured response conforming to schema (dict or Completion dataclass)."""
        ...


def _canonical_bytes(obj: Any) -> bytes:
    """Serialize JSON object canonically without defining a sha256 helper."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _validate_case_context(
    context: Mapping[str, Any],
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    """Strictly validate and verify case context across all callers.

    Returns (case_def, binding, contract, reference, admitted_evidence).
    Fails closed on any alias, tampered digest, forged definition, or missing key.
    """
    if not isinstance(context, Mapping) or isinstance(context, (str, bytes)):
        raise ValueError(f"context must be a mapping, got {type(context).__name__}")

    for k in ("binding", "contract", "reference"):
        if (
            k not in context
            or not isinstance(context[k], Mapping)
            or isinstance(context[k], (str, bytes))
        ):
            raise ValueError(f"context missing or invalid required mapping key {k!r}")

    binding = dict(context["binding"])
    contract = dict(context["contract"])
    reference = dict(context["reference"])

    # 1. Binding schema and keys
    extra_binding = set(binding.keys()) - ALLOWED_CONTEXT_BINDING_KEYS
    if extra_binding:
        raise ValueError(
            f"context binding has unexpected keys: {sorted(extra_binding)}"
        )
    missing_binding = ALLOWED_CONTEXT_BINDING_KEYS - set(binding.keys())
    if missing_binding:
        raise ValueError(
            f"context binding missing required keys: {sorted(missing_binding)}"
        )

    case_id = binding["case_id"]
    if type(case_id) is not str:
        raise ValueError(
            f"binding case_id must be a string, got {type(case_id).__name__}"
        )

    load_cases_catalogue()
    try:
        case_def = get_case(case_id)
    except KeyError as err:
        raise ValueError(f"unknown case_id {case_id!r}: {err}") from err

    # Case ID must strictly match canonical case definition id (no alias allowed!)
    if case_id != case_def["id"]:
        raise ValueError(
            f"binding case_id {case_id!r} is not canonical; expected {case_def['id']!r}"
        )

    case_version = binding["case_version"]
    if type(case_version) is not int or isinstance(case_version, bool):
        raise ValueError(
            f"binding case_version must be integer, got {type(case_version).__name__}"
        )
    if case_version != case_def["version"]:
        raise ValueError(
            f"binding case_version {case_version} does not match installed case version {case_def['version']}"
        )

    # 2. Check catalogue pin
    if binding["catalogue_sha256"] != REVIEWED_CASES_CATALOGUE_SHA256:
        raise ValueError("binding catalogue_sha256 does not match installed pin")

    # 3. Check installed thresholds and checker files
    thresh_file = Path(__file__).resolve().parent.parent / "thresholds.json"
    if not thresh_file.is_file():
        raise FileNotFoundError(f"thresholds file not found: {thresh_file}")
    expected_thresh_hash = hashlib.sha256(thresh_file.read_bytes()).hexdigest()
    if binding["thresholds_sha256"] != expected_thresh_hash:
        raise ValueError(
            f"binding thresholds_sha256 does not match installed thresholds: {binding['thresholds_sha256']}"
        )

    checker_file = Path(__file__).resolve().parent / "cases.py"
    if not checker_file.is_file():
        raise FileNotFoundError(f"checker cases.py not found: {checker_file}")
    expected_checker_hash = hashlib.sha256(checker_file.read_bytes()).hexdigest()
    if binding["checker_sha256"] != expected_checker_hash:
        raise ValueError(
            f"binding checker_sha256 does not match installed checker: {binding['checker_sha256']}"
        )

    # 4. Check contract and reference canonical hashes
    c_canon = _canonical_bytes(contract)
    c_hash = hashlib.sha256(c_canon).hexdigest()
    if c_hash != binding["contract_sha256"]:
        raise ValueError(
            f"context contract does not match binding contract_sha256: computed {c_hash}, binding {binding['contract_sha256']}"
        )

    r_canon = _canonical_bytes(reference)
    r_hash = hashlib.sha256(r_canon).hexdigest()
    if r_hash != binding.get("reference_sha256"):
        raise ValueError(
            f"context reference does not match binding reference_sha256: computed {r_hash}, binding {binding.get('reference_sha256')}"
        )

    source_sha = binding["source_sha256"]
    if (
        contract.get("source_sha256") != source_sha
        or reference.get("source_sha256") != source_sha
    ):
        raise ValueError(
            "source_sha256 mismatch across binding, contract, and reference"
        )

    if contract.get("case_id") != case_id:
        raise ValueError(
            f"contract case_id {contract.get('case_id')!r} does not match binding {case_id!r}"
        )

    # 5. Strictly validate reference against canonical case rules
    validate_source_reference(reference, case_def)

    # 6. Strictly validate and recompile contract against canonical case rules
    validate_case_contract(contract, case_def, reference)
    target_rows = contract.get("target_rows")
    if type(target_rows) is not int or isinstance(target_rows, bool) or target_rows < 1:
        raise ValueError(
            f"contract target_rows must be positive integer, got {target_rows!r}"
        )

    recompiled_contract = compile_case_contract(
        case_def, target_rows, source_sha, reference
    )
    if contract != recompiled_contract:
        raise ValueError(
            "context contract does not match recompiled canonical contract from installed catalogue"
        )

    # 7. Admitted evidence must come strictly from installed pack
    evidence_pack = get_evidence_pack()
    primary_ids = case_def.get("researched_facts", {}).get("primary_evidence_ids", [])
    installed_admitted_evidence = {
        eid: evidence_pack[eid] for eid in primary_ids if eid in evidence_pack
    }
    e_canon = _canonical_bytes(installed_admitted_evidence)
    e_hash = hashlib.sha256(e_canon).hexdigest()

    evidence_sha = binding.get("evidence_sha256")
    if not isinstance(evidence_sha, str) or not HEX64_LOWERCASE_RE.fullmatch(
        evidence_sha
    ):
        raise ValueError(
            f"binding evidence_sha256 must be a 64-character lowercase hex string, got {evidence_sha!r}"
        )
    if evidence_sha != e_hash:
        raise ValueError(
            "binding evidence_sha256 does not match installed admitted evidence hash"
        )

    # 8. Check source suitability receipt: must be typed, verified suitable True
    if "source_fit_receipt" not in context:
        raise ValueError("context missing required mapping key 'source_fit_receipt'")
    fit_receipt = context["source_fit_receipt"]
    if not isinstance(fit_receipt, Mapping) or isinstance(fit_receipt, (str, bytes)):
        raise ValueError(
            f"context 'source_fit_receipt' must be a mapping, got {type(fit_receipt).__name__}"
        )

    # Validate context-only full receipt digest
    expected_receipt_hash = binding.get("source_fit_sha256")
    if not isinstance(expected_receipt_hash, str) or not HEX64_LOWERCASE_RE.fullmatch(
        expected_receipt_hash
    ):
        raise ValueError(
            f"binding source_fit_sha256 must be a 64-character lowercase hex string, got {expected_receipt_hash!r}"
        )
    actual_receipt_hash = hashlib.sha256(_canonical_bytes(fit_receipt)).hexdigest()
    if actual_receipt_hash != expected_receipt_hash:
        raise ValueError(
            f"context source_fit_receipt does not match binding source_fit_sha256: "
            f"computed {actual_receipt_hash}, binding {expected_receipt_hash}"
        )

    if fit_receipt.get("suitable") is not True:
        raise ValueError(
            "context source_fit_receipt marks source as not suitable or invalid"
        )
    if fit_receipt.get("source_sha256") != binding["source_sha256"]:
        raise ValueError(
            f"context source_fit_receipt source_sha256 ({fit_receipt.get('source_sha256')!r}) "
            f"does not match binding source_sha256 ({binding['source_sha256']!r})"
        )
    if fit_receipt.get("licence_status") != reference.get("licence_status"):
        raise ValueError(
            f"context source_fit_receipt licence_status ({fit_receipt.get('licence_status')!r}) "
            f"does not match context reference licence_status ({reference.get('licence_status')!r})"
        )

    # Validate sourcefit facts against reference facts
    for row_key in ("source_rows", "usable_rows", "rejected_rows"):
        r_val = fit_receipt.get(row_key)
        ref_val = reference.get(row_key)
        if type(r_val) is not int or isinstance(r_val, bool) or r_val != ref_val:
            raise ValueError(
                f"context source_fit_receipt {row_key} ({r_val!r}) does not match reference ({ref_val!r})"
            )

    rec_aggs = fit_receipt.get("aggregates")
    if not isinstance(rec_aggs, Mapping) or isinstance(rec_aggs, (str, bytes)):
        raise ValueError("context source_fit_receipt 'aggregates' must be a mapping")

    for num_key in (
        "valid_fraction",
        "median_forward_spacing",
        "swir_answer_rate",
        "tir_answer_rate",
    ):
        a_val = rec_aggs.get(num_key)
        ref_val = reference.get(num_key)
        if (
            not isinstance(a_val, (int, float))
            or isinstance(a_val, bool)
            or math.isnan(a_val)
            or math.isinf(a_val)
            or abs(float(a_val) - float(ref_val)) > 1e-9
        ):
            raise ValueError(
                f"context source_fit_receipt aggregate {num_key} ({a_val!r}) does not match reference ({ref_val!r})"
            )

    if rec_aggs.get("has_blanks") != reference.get("has_blanks"):
        raise ValueError(
            "context source_fit_receipt 'has_blanks' does not match reference"
        )
    for lab_key in ("disclosed_swir_labels", "disclosed_tir_labels"):
        if rec_aggs.get(lab_key) != reference.get(lab_key):
            raise ValueError(
                f"context source_fit_receipt {lab_key} does not match reference"
            )

    return case_def, binding, contract, reference, installed_admitted_evidence


def _validate_exact_types(payload: Any, schema: Mapping[str, Any]) -> None:
    """Supplement jsonschema with exact scalar and nested type enforcement."""
    if not isinstance(payload, Mapping):
        return
    properties = schema.get("properties", {})
    for k, v in payload.items():
        if k not in properties:
            continue
        prop_schema = properties[k]
        p_type = prop_schema.get("type")
        if p_type == "integer":
            if type(v) is not int or isinstance(v, bool):
                raise ValueError(
                    f"property {k!r} must be strictly integer, got {type(v).__name__}"
                )
        elif p_type == "number":
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or math.isnan(v)
                or math.isinf(v)
            ):
                raise ValueError(
                    f"property {k!r} must be strictly finite number, got {v!r}"
                )
        elif p_type == "string":
            if type(v) is not str:
                raise ValueError(
                    f"property {k!r} must be strictly string, got {type(v).__name__}"
                )
        elif p_type == "array":
            if not isinstance(v, list):
                raise ValueError(f"property {k!r} must be list, got {type(v).__name__}")
            items_schema = prop_schema.get("items", {})
            for idx, item in enumerate(v):
                if items_schema.get("type") == "string" and type(item) is not str:
                    raise ValueError(
                        f"property {k}[{idx}] must be strictly string, got {type(item).__name__}"
                    )
                elif items_schema.get("type") == "object":
                    if not isinstance(item, Mapping) or isinstance(item, (str, bytes)):
                        raise ValueError(
                            f"property {k}[{idx}] must be strictly object mapping, got {type(item).__name__}"
                        )
                    _validate_exact_types(item, items_schema)


def _invoke_structured_model(
    model: Any, prompt: str, schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Invoke structured model interface and return strictly validated payload dict.

    Duck-types models.base.Completion dataclass without importing models,
    extracting completion.data. The returned advisory remains JSON-native;
    raw Usage dataclasses are not included in the return dict to preserve
    compatibility with engine ctx._must_json.
    """
    if hasattr(model, "complete") and callable(model.complete):
        raw_resp = model.complete(prompt, schema)
    elif callable(model):
        raw_resp = model(prompt, schema)
    else:
        raise TypeError(
            f"model must provide a .complete(prompt, schema) method or be callable, "
            f"got {type(model).__name__}"
        )

    # Duck-typed handling of models.base.Completion dataclass
    if hasattr(raw_resp, "data") and isinstance(raw_resp.data, Mapping):
        payload = dict(raw_resp.data)
    elif isinstance(raw_resp, Mapping):
        payload = dict(raw_resp)
    else:
        raise TypeError(
            f"model response must be a Completion dataclass with .data or a Mapping, "
            f"got {type(raw_resp).__name__}"
        )

    # Validate using standard jsonschema
    try:
        jsonschema.validate(instance=payload, schema=dict(schema))
    except jsonschema.ValidationError as err:
        raise ValueError(
            f"model response schema validation failed: {err.message}"
        ) from err
    except jsonschema.SchemaError as err:
        raise ValueError(f"invalid schema: {err.message}") from err

    # Supplement jsonschema for strict integer types (no float or bool aliases)
    _validate_exact_types(payload, schema)
    return payload


def _is_affirmatively_requested(purpose: str, domain: str) -> bool:
    """Check if domain is affirmatively requested, avoiding false positives on negations."""
    p_lower = purpose.lower()
    if domain not in p_lower:
        return False

    idx = p_lower.find(domain)
    while idx != -1:
        before = p_lower[max(0, idx - 40) : idx]
        after = p_lower[idx + len(domain) : min(len(p_lower), idx + len(domain) + 40)]

        neg_before = any(
            marker in before
            for marker in (
                "no ",
                "not ",
                "without ",
                "excluding ",
                "does not ",
                "do not ",
                "neither ",
                "never ",
                "free of ",
                "none ",
            )
        )
        neg_after = any(
            marker in after
            for marker in (
                " not requested",
                " not required",
                " not needed",
                " is not",
                " are not",
                " excluded",
                " omitted",
            )
        )
        if not (neg_before or neg_after):
            return True
        idx = p_lower.find(domain, idx + len(domain))
    return False


def _check_prohibited_prose(text: str, context_label: str) -> None:
    """Screen text fields against prohibited prose patterns across AI advisory roles.

    Lexical and fail-closed: a pattern anywhere in the text rejects it,
    whether or not it is negated. The error names the field (with item
    index), the matched pattern and whether a negation marker preceded it,
    so a rejected answer is diagnosable without echoing the model's prose.
    """
    t_lower = text.lower()
    digest_spans = [m.span() for m in _PROSE_DIGEST_TOKEN_RE.finditer(t_lower)]
    for pattern in PROHIBITED_PROSE_PATTERNS:
        start = t_lower.find(pattern)
        while start != -1:
            end = start + len(pattern)
            if not any(lo <= start and end <= hi for lo, hi in digest_spans):
                break
            start = t_lower.find(pattern, start + 1)
        if start != -1:
            negated = not _is_affirmatively_requested(t_lower, pattern)
            raise ValueError(
                f"{context_label} contains unverified or prohibited claim: {pattern!r} "
                f"(rejected in any context; negation marker nearby: "
                f"{'yes' if negated else 'no'})"
            )


def _lookup_measurement_path(measurements: Mapping[str, Any], path: str) -> int | float:
    """Retrieve numeric scalar value from measurements for a dotted path."""
    parts = path.split(".")
    curr: Any = measurements
    for part in parts:
        if not isinstance(curr, Mapping) or part not in curr:
            raise ValueError(f"path {path!r} not found in measurements")
        curr = curr[part]
    if (
        not isinstance(curr, (int, float))
        or isinstance(curr, bool)
        or math.isnan(curr)
        or math.isinf(curr)
    ):
        raise ValueError(
            f"measurement path {path!r} resolved to non-finite or non-numeric value: {curr!r}"
        )
    return curr


def _validate_evidence_facts(
    facts: Sequence[Any], measurements: Mapping[str, Any]
) -> None:
    """Validate candidate review evidence facts against recomputed measurements.

    Requires a non-empty list of structured objects:
    {"measurement": <path>, "value": <exact scalar number>}.
    Rejects extra keys, unknown paths, duplicates, non-finite values, and bool aliases.
    Compares against recomputed measurement numbers with exact scalar types.
    """
    if not isinstance(facts, list):
        raise ValueError(f"evidence_facts must be a list, got {type(facts).__name__}")
    if len(facts) == 0:
        raise ValueError("evidence_facts cannot be empty")
    if len(facts) > 20:
        raise ValueError(f"evidence_facts exceeds maximum 20 items (got {len(facts)})")

    seen_paths: set[str] = set()
    for idx, fact in enumerate(facts):
        if not isinstance(fact, Mapping) or isinstance(fact, (str, bytes)):
            raise ValueError(
                f"evidence_facts[{idx}] must be a mapping of 'measurement' and 'value', got {type(fact).__name__}"
            )
        extra_keys = set(fact.keys()) - {"measurement", "value"}
        if extra_keys:
            raise ValueError(
                f"evidence_facts[{idx}] has unexpected extra keys: {sorted(extra_keys)}"
            )
        if "measurement" not in fact or "value" not in fact:
            raise ValueError(
                f"evidence_facts[{idx}] missing required 'measurement' or 'value' keys"
            )

        path = fact["measurement"]
        if not isinstance(path, str) or path not in EVIDENCE_FACT_MEASUREMENT_PATHS:
            raise ValueError(
                f"evidence_facts[{idx}] unknown or unsupported measurement path: {path!r}"
            )

        if path in seen_paths:
            raise ValueError(f"duplicate evidence_fact measurement path: {path!r}")
        seen_paths.add(path)

        f_val = fact["value"]
        if (
            isinstance(f_val, bool)
            or not isinstance(f_val, (int, float))
            or math.isnan(f_val)
            or math.isinf(f_val)
        ):
            raise ValueError(
                f"evidence_facts[{idx}] value for {path!r} must be a finite number, got {f_val!r}"
            )

        expected = _lookup_measurement_path(measurements, path)
        if type(expected) is int:
            if type(f_val) is not int or f_val != expected:
                raise ValueError(
                    f"evidence_facts[{idx}] measurement {path!r} value {f_val!r} does not match expected integer {expected!r}"
                )
        else:
            if (
                not isinstance(f_val, (int, float))
                or abs(float(f_val) - float(expected)) > 1e-9
            ):
                raise ValueError(
                    f"evidence_facts[{idx}] measurement {path!r} value {f_val!r} does not match expected float {expected!r}"
                )


def prepare_case_context(
    source_path: Path | str,
    spec: Mapping[str, Any],
    licence_meta: str | None = None,
    thresholds_path: Path | str | None = None,
) -> dict[str, Any]:
    """Prepare immutable, code-side context for a case-selected run.

    Verifies:
    - RULOOM_THRESHOLDS environment variable is absent (calibrated thresholds
      must not be overridden for selected cases);
    - installed cases catalogue matches pinned SHA-256;
    - case ID is strictly canonical and version matches installed catalogue;
    - spec contains no unknown keys;
    - source file exists and matches spec source pin under a double-read guard;
    - deterministic source suitability pre-flight;
    - bounded aggregate reference generation;
    - target rows feasibility and joint satisfiability;
    - canonical contract compilation;
    - hash bindings for thresholds, case checker, and evidence.

    Returns a JSON-native dictionary with binding, contract, reference,
    admitted evidence, and source-fit receipt.
    """
    # 1. Refuse environment override for calibrated thresholds (even if empty string)
    if "RULOOM_THRESHOLDS" in os.environ:
        raise RuntimeError(
            "RULOOM_THRESHOLDS environment override is forbidden; "
            "calibrated thresholds must remain unchanged for case runs."
        )

    # 2. Validate thresholds file bytes and hash
    installed_thresh_file = Path(__file__).resolve().parent.parent / "thresholds.json"
    if not installed_thresh_file.is_file():
        raise FileNotFoundError(f"thresholds file not found: {installed_thresh_file}")
    installed_thresh_bytes = installed_thresh_file.read_bytes()
    expected_thresholds_hash = hashlib.sha256(installed_thresh_bytes).hexdigest()

    if thresholds_path is not None:
        thresh_file = Path(thresholds_path)
        if not thresh_file.is_file():
            raise FileNotFoundError(f"thresholds file not found: {thresh_file}")
        if (
            hashlib.sha256(thresh_file.read_bytes()).hexdigest()
            != expected_thresholds_hash
        ):
            raise ValueError(
                "supplied thresholds_path differs from installed canonical thresholds"
            )

    thresholds_hash = expected_thresholds_hash

    # 3. Validate checker file bytes and hash
    checker_file = Path(__file__).resolve().parent / "cases.py"
    if not checker_file.is_file():
        raise FileNotFoundError(f"checker cases.py not found: {checker_file}")
    checker_bytes = checker_file.read_bytes()
    checker_hash = hashlib.sha256(checker_bytes).hexdigest()

    # 4. Validate spec shape and types with strict schema (no unknown keys)
    if not isinstance(spec, Mapping) or isinstance(spec, (str, bytes)):
        raise ValueError(f"spec must be a mapping, got {type(spec).__name__}")

    spec_allowed_keys = frozenset({"case", "target_rows", "source", "outputs"})
    extra_spec_keys = set(spec.keys()) - spec_allowed_keys
    if extra_spec_keys:
        raise ValueError(f"spec has unexpected unknown keys: {sorted(extra_spec_keys)}")

    case_spec = spec.get("case")
    if not isinstance(case_spec, Mapping) or isinstance(case_spec, (str, bytes)):
        raise ValueError("spec must contain a 'case' mapping with 'id' and 'version'")

    case_spec_allowed_keys = frozenset({"id", "version"})
    extra_case_spec_keys = set(case_spec.keys()) - case_spec_allowed_keys
    if extra_case_spec_keys:
        raise ValueError(
            f"spec['case'] has unexpected unknown keys: {sorted(extra_case_spec_keys)}"
        )

    case_id = case_spec.get("id")
    case_version = case_spec.get("version")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("spec['case']['id'] must be a non-empty string")
    if (
        type(case_version) is not int
        or isinstance(case_version, bool)
        or case_version < 1
    ):
        raise ValueError(
            "spec['case']['version'] must be a positive integer (no boolean/float alias)"
        )

    target_rows = spec.get("target_rows")
    if type(target_rows) is not int or isinstance(target_rows, bool) or target_rows < 1:
        raise ValueError(
            "spec['target_rows'] must be a positive integer (no boolean/float alias)"
        )

    source_spec = spec.get("source")
    if not isinstance(source_spec, Mapping) or isinstance(source_spec, (str, bytes)):
        raise ValueError("spec['source'] must be a mapping with 'sha256'")

    source_spec_allowed = frozenset({"sha256"})
    extra_source_keys = set(source_spec.keys()) - source_spec_allowed
    if extra_source_keys:
        raise ValueError(
            f"spec['source'] has unexpected unknown keys: {sorted(extra_source_keys)}"
        )

    expected_source_sha = source_spec.get("sha256")
    if not isinstance(expected_source_sha, str) or not HEX64_LOWERCASE_RE.fullmatch(
        expected_source_sha
    ):
        raise ValueError(
            "spec['source']['sha256'] must be a 64-character lowercase hex string"
        )

    outputs = spec.get("outputs")
    if not isinstance(outputs, list) or outputs != ["scalar"]:
        raise ValueError(f"spec['outputs'] must be ['scalar'], got {outputs!r}")

    # 5. Load and verify installed cases catalogue
    load_cases_catalogue()
    catalogue_hash = REVIEWED_CASES_CATALOGUE_SHA256
    try:
        case_def = get_case(case_id)
    except KeyError as err:
        raise ValueError(f"unknown case {case_id!r}: {err}") from err

    # Case ID must strictly match canonical ID (rejecting aliases and unstripped strings)
    if case_id != case_def["id"]:
        raise ValueError(
            f"spec['case']['id'] {case_id!r} is not canonical; expected {case_def['id']!r}"
        )

    if type(case_def.get("version")) is not int or case_def["version"] != case_version:
        raise ValueError(
            f"case version mismatch for {case_id!r}: catalogue has {case_def.get('version')}, "
            f"spec requested {case_version}"
        )

    # 6. Source existence & double-read guard
    source_p = Path(source_path)
    if not source_p.is_file():
        raise FileNotFoundError(f"source file not found: {source_p}")
    raw_source_bytes = source_p.read_bytes()
    measured_source_hash = hashlib.sha256(raw_source_bytes).hexdigest()
    if measured_source_hash != expected_source_sha:
        raise ValueError(
            f"source sha256 mismatch: spec expected {expected_source_sha}, "
            f"measured file {source_p} has {measured_source_hash}"
        )

    # 7. Deterministic suitability assessment
    suitability = assess_source_suitability(
        source_p,
        case_def,
        licence_meta=licence_meta,
        expected_source_sha256=expected_source_sha,
    )
    if suitability.get("suitable") is not True:
        reasons = suitability.get("reasons", [])
        raise ValueError(f"source is not suitable for case {case_id!r}: {reasons}")

    # 8. Build bounded source reference with double-read verification
    ref = build_source_reference(source_p, case_def=case_def, licence_meta=licence_meta)
    if ref.get("source_sha256") != measured_source_hash:
        raise ValueError(
            f"double-read hash mismatch: built reference source_sha256 {ref.get('source_sha256')} "
            f"does not match measured {measured_source_hash}"
        )
    validate_source_reference(ref, case_def)

    # 9. Target feasibility checks
    validate_plan_satisfiability(target_rows, case_def, ref)
    check_source_joint_satisfiability(target_rows, case_def, ref)

    # 10. Canonical contract compilation and validation
    contract = compile_case_contract(case_def, target_rows, measured_source_hash, ref)
    validate_case_contract(contract, case_def, ref)

    # 11. Compute canonical hashes
    contract_bytes = _canonical_bytes(contract)
    contract_hash = hashlib.sha256(contract_bytes).hexdigest()

    ref_bytes = _canonical_bytes(ref)
    ref_hash = hashlib.sha256(ref_bytes).hexdigest()

    # 12. Admitted evidence pack
    evidence_pack = get_evidence_pack()
    primary_ids = case_def.get("researched_facts", {}).get("primary_evidence_ids", [])
    validate_citations(primary_ids)
    admitted_evidence = {
        eid: evidence_pack[eid] for eid in primary_ids if eid in evidence_pack
    }
    evidence_bytes = _canonical_bytes(admitted_evidence)
    evidence_hash = hashlib.sha256(evidence_bytes).hexdigest()

    # 13. Canonical digest of deterministic source-fit receipt
    receipt_canon = _canonical_bytes(suitability)
    receipt_hash = hashlib.sha256(receipt_canon).hexdigest()

    # 14. Assemble immutable context
    binding = {
        "case_id": case_id,
        "case_version": case_version,
        "catalogue_sha256": catalogue_hash,
        "contract_sha256": contract_hash,
        "reference_sha256": ref_hash,
        "source_sha256": measured_source_hash,
        "thresholds_sha256": thresholds_hash,
        "checker_sha256": checker_hash,
        "evidence_sha256": evidence_hash,
        "source_fit_sha256": receipt_hash,
    }

    return {
        "binding": binding,
        "contract": copy.deepcopy(contract),
        "reference": copy.deepcopy(ref),
        "admitted_evidence": copy.deepcopy(admitted_evidence),
        "evidence": copy.deepcopy(admitted_evidence),
        "source_fit_receipt": copy.deepcopy(suitability),
        "source_fit": copy.deepcopy(suitability),
        "source_fit_receipt_sha256": receipt_hash,
        "case": copy.deepcopy(case_def),
        "disclosed_model_summary": build_disclosed_model_summary(ref),
    }


def evaluate_case_context(
    candidate_bytes: bytes | str | Path, context: Mapping[str, Any]
) -> dict[str, Any]:
    """Evaluate candidate CSV bytes against a strictly validated case context.

    Fails on missing context keys or inconsistent pins/digests.
    Returns the complete evaluate_case_utility result dictionary.
    """
    case_def, binding, contract, reference, _ = _validate_case_context(context)

    # Resolve candidate bytes
    if isinstance(candidate_bytes, str):
        c_payload = candidate_bytes.encode("utf-8")
    elif isinstance(candidate_bytes, Path):
        c_payload = candidate_bytes.read_bytes()
    elif isinstance(candidate_bytes, (bytes, bytearray)):
        c_payload = bytes(candidate_bytes)
    else:
        raise ValueError(
            f"candidate_bytes must be bytes, str or Path, got {type(candidate_bytes).__name__}"
        )

    return evaluate_case_utility(c_payload, reference, contract)


def _is_finite_num(v: Any) -> bool:
    """Return True if v is a finite int or float (rejecting booleans)."""
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and not math.isnan(v)
        and not math.isinf(v)
    )


def _is_non_neg_int(v: Any) -> bool:
    """Return True if v is a non-negative int (rejecting booleans)."""
    return type(v) is int and not isinstance(v, bool) and v >= 0


def _validate_measurement_shape(key: str, val: Any) -> str | None:
    """Validate that a measurement object matches the exact schema and types.

    Returns None if valid, or a descriptive reason string if invalid.
    Never raises an exception.
    """
    try:
        if key == "source_pin_match":
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return "source_pin_match must be a mapping"
            expected_keys = {"contract_pin", "reference_pin"}
            if set(val.keys()) != expected_keys:
                return f"source_pin_match keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            c_pin = val["contract_pin"]
            r_pin = val["reference_pin"]
            if not isinstance(c_pin, str) or not isinstance(r_pin, str):
                return "source_pin_match pins must be strings"
            if not c_pin or not r_pin:
                return "source_pin_match pins cannot be empty"
            return None

        if key == "output_columns":
            if not isinstance(val, list):
                return "output_columns must be a list"
            if len(val) == 0:
                return "output_columns list cannot be empty"
            if not all(isinstance(c, str) and len(c) > 0 for c in val):
                return "output_columns items must be non-empty strings"
            return None

        if key == "row_count":
            if not _is_non_neg_int(val):
                return f"row_count must be a non-negative integer, got {val!r}"
            return None

        if key == "well_formed_rows":
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return "well_formed_rows must be a mapping"
            expected_keys = {"parsed", "total", "malformed_detail"}
            if set(val.keys()) != expected_keys:
                return f"well_formed_rows keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            if not _is_non_neg_int(val["parsed"]):
                return "well_formed_rows 'parsed' must be a non-negative integer"
            if not _is_non_neg_int(val["total"]):
                return "well_formed_rows 'total' must be a non-negative integer"
            detail = val["malformed_detail"]
            if detail is not None and not isinstance(detail, str):
                return "well_formed_rows 'malformed_detail' must be a string or None"
            return None

        if key == "ordered_depth":
            if (type(val) is str and val == "nondecreasing required") or type(
                val
            ) is bool:
                return None
            return f"ordered_depth must be 'nondecreasing required' string or boolean, got {val!r}"

        if key == "depth_spacing":
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return "depth_spacing must be a mapping"
            expected_keys = {
                "source_reference",
                "candidate",
                "relative_difference",
                "limit",
                "definition",
            }
            if set(val.keys()) != expected_keys:
                return f"depth_spacing keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            for num_k in (
                "source_reference",
                "candidate",
                "relative_difference",
                "limit",
            ):
                if not _is_finite_num(val[num_k]):
                    return f"depth_spacing {num_k!r} must be a finite number, got {val[num_k]!r}"
            if (
                val["source_reference"] <= 0
                or val["candidate"] <= 0
                or val["limit"] <= 0
                or val["relative_difference"] < 0
            ):
                return "depth_spacing numbers out of valid ranges"
            if not isinstance(val["definition"], str) or not val["definition"]:
                return "depth_spacing 'definition' must be a non-empty string"
            return None

        if key == "inclusion_fraction":
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return "inclusion_fraction must be a mapping"
            expected_keys = {
                "source_reference",
                "candidate",
                "absolute_difference",
                "limit",
            }
            if set(val.keys()) != expected_keys:
                return f"inclusion_fraction keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            for num_k in expected_keys:
                if not _is_finite_num(val[num_k]) or not (0.0 <= val[num_k] <= 1.0):
                    return f"inclusion_fraction {num_k!r} must be a finite number in [0, 1], got {val[num_k]!r}"
            return None

        if key in ("swir_answer_rate", "tir_answer_rate"):
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return f"{key} must be a mapping"
            expected_keys = {
                "source_reference",
                "candidate",
                "absolute_difference",
                "limit",
                "denominator",
            }
            if set(val.keys()) != expected_keys:
                return f"{key} keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            if not _is_finite_num(val["source_reference"]) or not (
                0.0 <= val["source_reference"] <= 1.0
            ):
                return f"{key} 'source_reference' must be a finite number in [0, 1]"
            if not _is_finite_num(val["limit"]) or not (0.0 <= val["limit"] <= 1.0):
                return f"{key} 'limit' must be a finite number in [0, 1]"
            cand = val["candidate"]
            diff = val["absolute_difference"]
            if cand is not None and (
                not _is_finite_num(cand) or not (0.0 <= cand <= 1.0)
            ):
                return f"{key} 'candidate' must be a finite number in [0, 1] or None"
            if diff is not None and (
                not _is_finite_num(diff) or not (0.0 <= diff <= 1.0)
            ):
                return f"{key} 'absolute_difference' must be a finite number in [0, 1] or None"
            if not isinstance(val["denominator"], str) or not val["denominator"]:
                return f"{key} 'denominator' must be a non-empty string"
            return None

        if key in ("swir_supported_labels", "tir_supported_labels"):
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return f"{key} must be a mapping"
            expected_keys = {"unknown_labels", "population"}
            if set(val.keys()) != expected_keys:
                return f"{key} keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            if not isinstance(val["unknown_labels"], list) or not all(
                isinstance(lbl, str) for lbl in val["unknown_labels"]
            ):
                return f"{key} 'unknown_labels' must be a list of strings"
            if not isinstance(val["population"], str) or not val["population"]:
                return f"{key} 'population' must be a non-empty string"
            return None

        if key == "downstream_filter_group":
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return "downstream_filter_group must be a mapping"
            expected_keys = {"total_rows", "included", "excluded", "group_count"}
            if set(val.keys()) != expected_keys:
                return f"downstream_filter_group keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            for int_k in expected_keys:
                if not _is_non_neg_int(val[int_k]):
                    return f"downstream_filter_group {int_k!r} must be a non-negative integer, got {val[int_k]!r}"
            if val["included"] + val["excluded"] != val["total_rows"]:
                return f"downstream_filter_group accounting mismatch: included ({val['included']}) + excluded ({val['excluded']}) != total_rows ({val['total_rows']})"
            return None

        if key == "downstream_structure_both_states_present":
            if not isinstance(val, Mapping) or isinstance(val, (str, bytes)):
                return "downstream_structure_both_states_present must be a mapping"
            expected_keys = {
                "included_rows",
                "excluded_rows",
                "swir_blanks_in_valid",
                "tir_blanks_in_valid",
                "swir_answered_in_valid",
                "tir_answered_in_valid",
                "structure_detail",
            }
            if set(val.keys()) != expected_keys:
                return f"downstream_structure_both_states_present keys must be exactly {sorted(expected_keys)}, got {sorted(val.keys())}"
            for count_k in (
                "included_rows",
                "excluded_rows",
                "swir_blanks_in_valid",
                "tir_blanks_in_valid",
                "swir_answered_in_valid",
                "tir_answered_in_valid",
            ):
                if not _is_non_neg_int(val[count_k]):
                    return f"downstream_structure_both_states_present {count_k!r} must be a non-negative integer, got {val[count_k]!r}"
            s_detail = val["structure_detail"]
            if not isinstance(s_detail, list) or not all(
                isinstance(s, str) for s in s_detail
            ):
                return "downstream_structure_both_states_present 'structure_detail' must be a list of strings"
            return None

        return f"unknown measurement key: {key!r}"
    except Exception as err:
        return f"exception validating {key!r} shape: {err}"


def case_check_results(utility_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convert an already computed case utility report to engine CheckResult dicts.

    Emits ALL 13 namespaced case.* measurement IDs every time.
    Validates report top-level schema (passed boolean, 64-char lowercase hex
    candidate_sha256, non-empty limitations list, list of failure strings)
    and exact keysets and scalar types for all 13 measurement objects.
    Malformed report or measurement shapes fail closed with all 13 checks failing,
    never crashing with TypeError or KeyError.

    Note: This helper is a check result mapping adapter for an already computed
    evaluator report. It trusts the evaluator's semantic outcomes (recorded in
    failures); actual semantic and contract authority resides in evaluate_case_context
    and verify_case_candidate under the canonical contract.
    """
    if not isinstance(utility_report, Mapping) or isinstance(
        utility_report, (str, bytes)
    ):
        return [
            {
                "check_id": cid,
                "kind": "constraint",
                "status": "fail",
                "detail": "utility report is missing or invalid mapping",
                "measured": None,
                "threshold": {"source": "case_contract"},
                "threshold_source": "case_contract",
                "band_position": None,
            }
            for cid in CASE_CHECK_IDS
        ]

    rep_passed = utility_report.get("passed")
    failures_raw = utility_report.get("failures", [])

    is_inconsistent = False
    inconsistent_reason = ""

    # 1. Fail closed if passed is not strictly a boolean
    if type(rep_passed) is not bool:
        is_inconsistent = True
        inconsistent_reason = (
            f"utility report 'passed' must be boolean, got {type(rep_passed).__name__}"
        )

    # 2. Fail closed if candidate_sha256 is missing or invalid
    cand_sha = utility_report.get("candidate_sha256")
    if not is_inconsistent:
        if not isinstance(cand_sha, str) or not HEX64_LOWERCASE_RE.fullmatch(cand_sha):
            is_inconsistent = True
            inconsistent_reason = f"utility report 'candidate_sha256' must be 64-character lowercase hex string, got {cand_sha!r}"

    # 3. Fail closed if limitations is missing or empty
    limitations = utility_report.get("limitations")
    if not is_inconsistent:
        if (
            not isinstance(limitations, list)
            or len(limitations) == 0
            or not all(isinstance(x, str) and len(x.strip()) > 0 for x in limitations)
        ):
            is_inconsistent = True
            inconsistent_reason = "utility report 'limitations' must be a non-empty list of non-empty strings"

    # 4. Fail closed if report has unexpected top-level keys
    allowed_report_keys = frozenset(
        {
            "passed",
            "failures",
            "measurements",
            "candidate_sha256",
            "limitations",
            "detail",
        }
    )
    extra_report_keys = sorted(set(utility_report.keys()) - allowed_report_keys)
    if extra_report_keys and not is_inconsistent:
        is_inconsistent = True
        inconsistent_reason = (
            f"utility report has unexpected top-level keys: {extra_report_keys}"
        )

    # 5. Fail closed if failures is not strictly a list of strings (handling unhashables defensively)
    if not isinstance(failures_raw, list):
        is_inconsistent = True
        inconsistent_reason = f"utility report 'failures' must be a list, got {type(failures_raw).__name__}"
        failures_set: set[str] = set()
    else:
        for idx, item in enumerate(failures_raw):
            if type(item) is not str:
                is_inconsistent = True
                inconsistent_reason = f"utility report failures[{idx}] must be a string, got {type(item).__name__}"
                break
        try:
            failures_set = set(failures_raw) if not is_inconsistent else set()
        except TypeError as err:
            is_inconsistent = True
            inconsistent_reason = (
                f"utility report failures contains unhashable item: {err}"
            )
            failures_set = set()

    measurements = utility_report.get("measurements")
    if not isinstance(measurements, Mapping) or isinstance(measurements, (str, bytes)):
        is_inconsistent = True
        inconsistent_reason = "utility report 'measurements' must be a mapping"
        measurements = {}
    else:
        extra_meas = sorted(set(measurements.keys()) - set(CASE_MEASUREMENT_KEYS))
        if extra_meas and not is_inconsistent:
            is_inconsistent = True
            inconsistent_reason = (
                f"measurements has unexpected unknown keys: {extra_meas}"
            )

    known_check_failures = {
        f for f in failures_set if f in CASE_MEASUREMENT_KEYS or f in CASE_CHECK_IDS
    }
    global_failures = failures_set - known_check_failures

    # Inconsistent passed vs failures
    if not is_inconsistent:
        if rep_passed is True and len(failures_set) > 0:
            is_inconsistent = True
            inconsistent_reason = f"inconsistent report: passed is True but failures present: {sorted(failures_set)}"
        elif rep_passed is False and not known_check_failures and not global_failures:
            is_inconsistent = True
            inconsistent_reason = (
                "inconsistent report: passed is False but failures list is empty"
            )

    # Validate shape of each measurement present in measurements
    if not is_inconsistent:
        if not global_failures:
            missing_meas = [k for k in CASE_MEASUREMENT_KEYS if k not in measurements]
            if missing_meas:
                is_inconsistent = True
                inconsistent_reason = (
                    f"measurements missing required measurement keys: {missing_meas}"
                )

        if not is_inconsistent:
            for k, raw_v in measurements.items():
                shape_err = _validate_measurement_shape(k, raw_v)
                if shape_err is not None:
                    is_inconsistent = True
                    inconsistent_reason = f"malformed measurement {k!r}: {shape_err}"
                    break

    is_global_failure = bool(global_failures or is_inconsistent)

    results: list[dict[str, Any]] = []
    for k in CASE_MEASUREMENT_KEYS:
        cid = f"case.{k}"
        if is_global_failure:
            status = "fail"
            detail = (
                inconsistent_reason
                if is_inconsistent
                else f"overall evaluation failed (failures: {sorted(failures_set)})"
            )
            measured = copy.deepcopy(measurements.get(k))
        elif k not in measurements or measurements[k] is None:
            status = "fail"
            detail = f"measurement {k!r} missing from evaluator output (failures: {sorted(failures_set)})"
            measured = None
        else:
            raw_measured = measurements[k]
            measured = copy.deepcopy(raw_measured)

            check_failed = (k in failures_set) or (cid in failures_set)
            fail_reason = ""

            if not check_failed:
                if k == "source_pin_match":
                    if raw_measured.get("contract_pin") != raw_measured.get(
                        "reference_pin"
                    ):
                        check_failed = True
                        fail_reason = (
                            "source_pin_match contract_pin and reference_pin differ"
                        )
                elif k == "well_formed_rows":
                    if (
                        raw_measured.get("parsed") != raw_measured.get("total")
                        or raw_measured.get("malformed_detail") is not None
                    ):
                        check_failed = True
                        fail_reason = "well_formed_rows parsed != total or malformed"
                elif k in ("swir_supported_labels", "tir_supported_labels"):
                    if raw_measured.get("unknown_labels"):
                        check_failed = True
                        fail_reason = f"{k} has non-empty unknown_labels"
                elif k == "downstream_structure_both_states_present":
                    if raw_measured.get("structure_detail"):
                        check_failed = True
                        fail_reason = "downstream structure presence details non-empty"
                    elif (
                        raw_measured.get("included_rows", 0) <= 0
                        or raw_measured.get("excluded_rows", 0) <= 0
                    ):
                        check_failed = True
                        fail_reason = "downstream structure requires both included and excluded rows > 0"

            if check_failed:
                status = "fail"
                detail = fail_reason or (
                    json.dumps(measured, sort_keys=True)
                    if isinstance(measured, (dict, list))
                    else str(measured)
                )
            else:
                status = "pass"
                detail = (
                    json.dumps(measured, sort_keys=True)
                    if isinstance(measured, (dict, list))
                    else str(measured)
                )

        results.append(
            {
                "check_id": cid,
                "kind": "constraint",
                "status": status,
                "detail": detail,
                "measured": measured,
                "threshold": {"source": "case_contract"},
                "threshold_source": "case_contract",
                "band_position": None,
            }
        )

    return results


def build_case_members(
    candidate_bytes: bytes,
    context: Mapping[str, Any],
    utility_report: Mapping[str, Any],
    spec_digest: str,
) -> dict[str, bytes]:
    """Package case artifacts for frozen release.

    Produces flat member files:
    - case-binding.json
    - case-contract.json
    - case-reference.json
    - case-utility.json
    - case-evidence.json

    Strictly validates context, spec_digest format (64-char lowercase hex),
    and verifies the full utility report via verify_case_candidate.
    Only passed candidates can be packaged.
    """
    case_def, binding, contract, reference, admitted_evidence = _validate_case_context(
        context
    )

    if not isinstance(candidate_bytes, (bytes, bytearray)):
        raise TypeError(
            f"candidate_bytes must be bytes, got {type(candidate_bytes).__name__}"
        )
    candidate_bytes = bytes(candidate_bytes)
    cand_sha = hashlib.sha256(candidate_bytes).hexdigest()

    # spec_digest validation: strictly 64 lowercase hex chars
    if not isinstance(spec_digest, str) or not HEX64_LOWERCASE_RE.fullmatch(
        spec_digest
    ):
        raise ValueError(
            "spec_digest must be a 64-character lowercase hexadecimal SHA-256 string"
        )

    if not isinstance(utility_report, Mapping) or isinstance(
        utility_report, (str, bytes)
    ):
        raise TypeError(
            f"utility_report must be a mapping, got {type(utility_report).__name__}"
        )

    # Own-candidate report verification: must match candidate_sha256
    report_cand_sha = utility_report.get("candidate_sha256")
    if report_cand_sha != cand_sha:
        raise ValueError(
            f"utility report candidate sha256 {report_cand_sha!r} does not match "
            f"candidate bytes sha256 {cand_sha!r}; attaching another attempt's report is forbidden."
        )

    # Full report verification using verify_case_candidate
    cand_verify = verify_case_candidate(
        candidate_bytes, reference, contract, reported_utility=utility_report
    )
    if (
        not cand_verify.get("case_verified")
        or cand_verify.get("matches_reported") is not True
    ):
        raise ValueError(
            f"candidate utility report verification failed: {cand_verify.get('discrepancies')}"
        )

    if utility_report.get("passed") is not True:
        raise ValueError(
            "cannot package failing candidate report; candidate must pass case utility checks"
        )

    evidence_hash = hashlib.sha256(_canonical_bytes(admitted_evidence)).hexdigest()

    binding_member = {
        "case_id": str(binding["case_id"]),
        "case_version": int(binding["case_version"]),
        "spec_digest": str(spec_digest),
        "candidate_sha256": cand_sha,
        "catalogue_sha256": str(binding["catalogue_sha256"]),
        "contract_sha256": str(binding["contract_sha256"]),
        "reference_sha256": str(binding["reference_sha256"]),
        "source_sha256": str(binding["source_sha256"]),
        "thresholds_sha256": str(binding["thresholds_sha256"]),
        "checker_sha256": str(binding["checker_sha256"]),
        "evidence_sha256": evidence_hash,
    }

    members: dict[str, bytes] = {
        "case-binding.json": _canonical_bytes(binding_member),
        "case-contract.json": _canonical_bytes(contract),
        "case-reference.json": _canonical_bytes(reference),
        "case-utility.json": _canonical_bytes(utility_report),
        "case-evidence.json": _canonical_bytes(admitted_evidence),
    }

    for name in members:
        if name not in CASE_MEMBER_NAMES:
            raise ValueError(f"unsafe or undeclared case member name: {name!r}")
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"path traversal in member name: {name!r}")

    return members


def verify_case_members(
    members: Mapping[str, bytes],
    expected_case: Mapping[str, Any] | None = None,
    source_path: Path | str | None = None,
    expected_spec_digest: str | None = None,
) -> dict[str, Any]:
    """Independent consumer verification of case release members.

    Recomputes practical checks over data.csv using canonical installed contract
    and reference, verifies binding hashes against installed code/catalogue/thresholds,
    verifies case-evidence.json against installed evidence pack,
    and checks expected_case constraints (strictly {id, version}).
    Never executes or imports package-provided paths.
    """
    discrepancies: list[str] = []

    if not isinstance(members, Mapping) or isinstance(members, (str, bytes)):
        return {
            "verified": False,
            "discrepancies": ["members must be a mapping of filename to bytes"],
            "source_derivation": "unproven (no members)",
        }

    # Verify every member value is strictly bytes (avoiding TypeError on string data.csv)
    for k, v in members.items():
        if not isinstance(v, (bytes, bytearray)):
            discrepancies.append(
                f"member {k!r} payload must be bytes, got {type(v).__name__}"
            )

    # Exact flat required members set
    required_member_names = frozenset(
        {
            "case-binding.json",
            "case-contract.json",
            "case-reference.json",
            "case-utility.json",
            "case-evidence.json",
            "data.csv",
        }
    )

    missing_members = sorted(required_member_names - set(members.keys()))
    if missing_members:
        discrepancies.append(f"missing required release members: {missing_members}")

    extra_members = sorted(set(members.keys()) - required_member_names)
    if extra_members:
        discrepancies.append(
            f"unexpected extra or unsafe members in package: {extra_members}"
        )

    if discrepancies:
        return {
            "verified": False,
            "discrepancies": discrepancies,
            "source_derivation": "unproven (member structure invalid)",
        }

    # 1. Parse JSON members safely
    try:
        binding = json.loads(members["case-binding.json"].decode("utf-8"))
    except Exception as err:
        return {
            "verified": False,
            "discrepancies": [f"failed to decode case-binding.json: {err}"],
            "source_derivation": "unproven (invalid JSON)",
        }

    try:
        shipped_contract = json.loads(members["case-contract.json"].decode("utf-8"))
        shipped_reference = json.loads(members["case-reference.json"].decode("utf-8"))
        shipped_utility = json.loads(members["case-utility.json"].decode("utf-8"))
        shipped_evidence = json.loads(members["case-evidence.json"].decode("utf-8"))
    except Exception as err:
        return {
            "verified": False,
            "discrepancies": [f"failed to decode case JSON members: {err}"],
            "source_derivation": "unproven (invalid JSON)",
        }

    # 2. Validate binding schema
    if not isinstance(binding, Mapping) or isinstance(binding, (str, bytes)):
        discrepancies.append("case-binding.json must be a mapping")
        return {
            "verified": False,
            "discrepancies": discrepancies,
            "source_derivation": "unproven",
        }

    extra_binding_keys = sorted(set(binding.keys()) - ALLOWED_BINDING_KEYS)
    if extra_binding_keys:
        discrepancies.append(
            f"extra unknown keys in case-binding.json: {extra_binding_keys}"
        )

    missing_binding_keys = sorted(ALLOWED_BINDING_KEYS - set(binding.keys()))
    if missing_binding_keys:
        discrepancies.append(
            f"missing keys in case-binding.json: {missing_binding_keys}"
        )

    case_id = binding.get("case_id")
    case_version = binding.get("case_version")
    if type(case_version) is not int or isinstance(case_version, bool):
        discrepancies.append(
            f"case-binding.json case_version must be integer, got {type(case_version).__name__}"
        )
    if type(case_id) is not str:
        discrepancies.append(
            f"case-binding.json case_id must be string, got {type(case_id).__name__}"
        )

    # Validate spec_digest format in binding
    binding_sd = binding.get("spec_digest")
    if not isinstance(binding_sd, str) or not HEX64_LOWERCASE_RE.fullmatch(binding_sd):
        discrepancies.append(
            f"case-binding.json spec_digest must be 64-char lowercase hex string, got {binding_sd!r}"
        )

    if expected_spec_digest is not None:
        if not isinstance(
            expected_spec_digest, str
        ) or not HEX64_LOWERCASE_RE.fullmatch(expected_spec_digest):
            discrepancies.append(
                f"expected_spec_digest is invalid: {expected_spec_digest!r}"
            )
        elif binding_sd != expected_spec_digest:
            discrepancies.append(
                f"spec_digest mismatch: expected {expected_spec_digest}, got {binding_sd}"
            )

    # 3. Check expected_case strictly (exact {id, version}, no alias keys, no extra keys)
    if expected_case is not None:
        if not isinstance(expected_case, Mapping) or isinstance(
            expected_case, (str, bytes)
        ):
            discrepancies.append("expected_case must be a mapping")
        else:
            exp_keys = set(expected_case.keys())
            if exp_keys != {"id", "version"}:
                discrepancies.append(
                    f"expected_case must strictly contain only 'id' and 'version', got {sorted(exp_keys)}"
                )
            else:
                exp_id = expected_case["id"]
                exp_ver = expected_case["version"]
                if type(exp_id) is not str:
                    discrepancies.append(
                        f"expected_case 'id' must be string, got {type(exp_id).__name__}"
                    )
                elif exp_id != case_id:
                    discrepancies.append(
                        f"expected case ID {exp_id!r} does not match package case ID {case_id!r}"
                    )

                if type(exp_ver) is not int or isinstance(exp_ver, bool):
                    discrepancies.append(
                        f"expected_case 'version' must be integer, got {type(exp_ver).__name__}"
                    )
                elif exp_ver != case_version:
                    discrepancies.append(
                        f"expected case version {exp_ver!r} does not match package case version {case_version!r}"
                    )

    # 4. Check catalogue pin and load installed case definition
    load_cases_catalogue()
    if binding.get("catalogue_sha256") != REVIEWED_CASES_CATALOGUE_SHA256:
        discrepancies.append(
            f"binding catalogue hash {binding.get('catalogue_sha256')!r} does not match "
            f"installed catalogue pin {REVIEWED_CASES_CATALOGUE_SHA256!r}"
        )

    try:
        case_def = get_case(str(case_id)) if isinstance(case_id, str) else None
    except KeyError:
        discrepancies.append(f"case {case_id!r} is not installed in trusted catalogue")
        case_def = None

    if case_def is not None:
        if case_def["id"] != case_id:
            discrepancies.append(
                f"binding case_id {case_id!r} is not canonical; catalogue id is {case_def['id']!r}"
            )
        if case_def.get("version") != case_version:
            discrepancies.append(
                f"package case version {case_version} does not match installed case version {case_def.get('version')}"
            )

    # 5. Check installed thresholds and checker bytes
    thresh_file = Path(__file__).resolve().parent.parent / "thresholds.json"
    if thresh_file.is_file():
        expected_thresh_hash = hashlib.sha256(thresh_file.read_bytes()).hexdigest()
        if binding.get("thresholds_sha256") != expected_thresh_hash:
            discrepancies.append(
                f"binding thresholds hash {binding.get('thresholds_sha256')!r} differs from "
                f"installed thresholds hash {expected_thresh_hash!r}"
            )

    checker_file = Path(__file__).resolve().parent / "cases.py"
    if checker_file.is_file():
        expected_checker_hash = hashlib.sha256(checker_file.read_bytes()).hexdigest()
        if binding.get("checker_sha256") != expected_checker_hash:
            discrepancies.append(
                f"binding checker hash {binding.get('checker_sha256')!r} differs from "
                f"installed checker hash {expected_checker_hash!r}"
            )

    # 6. Verify shipped member hashes against binding
    c_canon = _canonical_bytes(shipped_contract)
    c_hash = hashlib.sha256(c_canon).hexdigest()
    if c_hash != binding.get("contract_sha256"):
        discrepancies.append(
            "shipped contract canonical hash does not match binding contract_sha256"
        )

    r_canon = _canonical_bytes(shipped_reference)
    r_hash = hashlib.sha256(r_canon).hexdigest()
    if r_hash != binding.get("reference_sha256"):
        discrepancies.append(
            "shipped reference canonical hash does not match binding reference_sha256"
        )

    e_canon = _canonical_bytes(shipped_evidence)
    e_hash = hashlib.sha256(e_canon).hexdigest()
    if e_hash != binding.get("evidence_sha256"):
        discrepancies.append(
            "shipped evidence canonical hash does not match binding evidence_sha256"
        )

    data_bytes = members["data.csv"]
    cand_hash = hashlib.sha256(data_bytes).hexdigest()
    if cand_hash != binding.get("candidate_sha256"):
        discrepancies.append(
            "shipped data.csv hash does not match binding candidate_sha256"
        )

    # Verify evidence content against installed pack
    if case_def is not None:
        evidence_pack = get_evidence_pack()
        primary_ids = case_def.get("researched_facts", {}).get(
            "primary_evidence_ids", []
        )
        expected_evidence = {
            eid: evidence_pack[eid] for eid in primary_ids if eid in evidence_pack
        }
        if shipped_evidence != expected_evidence:
            discrepancies.append(
                "shipped case-evidence.json content does not match installed canonical evidence pack"
            )

    # 7. Recompile contract from installed case and compare
    if case_def is not None:
        try:
            target_rows = shipped_contract.get("target_rows")
            source_sha = binding.get("source_sha256")
            recompiled_contract = compile_case_contract(
                case_def, target_rows, source_sha, shipped_reference
            )
            if recompiled_contract != shipped_contract:
                discrepancies.append(
                    "shipped contract does not match recompiled canonical contract from installed catalogue "
                    "(possible threshold, denominator, or flag tampering)"
                )
        except Exception as err:
            discrepancies.append(f"failed to recompile canonical contract: {err}")

    # 8. Recompute utility checks via verify_case_candidate
    cand_verify = None
    try:
        cand_verify = verify_case_candidate(
            data_bytes, shipped_reference, shipped_contract, shipped_utility
        )
        if not cand_verify.get("case_verified"):
            discrepancies.append(
                "candidate practical-use checks failed or tampered report detected"
            )
        if cand_verify.get("matches_reported") is not True:
            discrepancies.append(
                f"reported utility mismatch: {cand_verify.get('discrepancies', [])}"
            )
    except Exception as err:
        discrepancies.append(f"candidate verification failed with error: {err}")
        cand_verify = {"case_verified": False, "discrepancies": [str(err)]}

    # 9. Source derivation verification
    if source_path is not None:
        source_p = Path(source_path)
        if not source_p.is_file():
            discrepancies.append(f"supplied source file does not exist: {source_p}")
            source_derivation = "source_file_missing"
        elif case_def is None:
            source_derivation = "unproven (no case definition)"
        else:
            try:
                derived_ref = build_source_reference(source_p, case_def)
                # Compare structural and numerical source facts (licence status is separate assertion)
                keys_to_compare = [
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
                ]
                facts_match = all(
                    derived_ref.get(k) == shipped_reference.get(k)
                    for k in keys_to_compare
                )
                if not facts_match:
                    discrepancies.append(
                        "locally derived source reference differs from shipped reference in structural facts"
                    )
                    source_derivation = "failed_mismatch"
                else:
                    source_derivation = "proven (source facts verified; licence assertion unproven by bytes alone)"
            except Exception as err:
                discrepancies.append(f"failed to re-derive source reference: {err}")
                source_derivation = f"failed_error: {err}"
    else:
        source_derivation = "unproven (aggregate consistency only)"

    return {
        "verified": len(discrepancies) == 0,
        "case_id": case_id,
        "case_version": case_version,
        "discrepancies": discrepancies,
        "source_derivation": source_derivation,
        "utility_verification": cand_verify,
        "binding": binding,
    }


def repair_case_plan(
    plan: Mapping[str, Any],
    failed_ids: Sequence[str],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Causal, same-seed, same-target plan repair.

    Validates context, plan seed and target rows.
    Only one causal strategy adjustment is supported:
    fitted_markov_joint -> fitted_markov_balanced
    when 'case.inclusion_fraction' fails.
    The initial seed and exact row target remain immutable.
    Deep-copies output to prevent mutating input plan.
    No model calls.
    """
    case_def, binding, contract, reference, _ = _validate_case_context(context)

    if not isinstance(plan, Mapping) or isinstance(plan, (str, bytes)):
        raise TypeError(f"plan must be a mapping, got {type(plan).__name__}")

    # Validate seed strictly as integer
    seed = plan.get("seed")
    if type(seed) is not int or isinstance(seed, bool):
        raise ValueError(f"plan seed must be integer, got {type(seed).__name__}")

    # Validate target rows / n_rows
    if "target_rows" in plan and "n_rows" in plan:
        t_val = plan["target_rows"]
        n_val = plan["n_rows"]
        if (
            type(t_val) is not int
            or isinstance(t_val, bool)
            or t_val < 1
            or type(n_val) is not int
            or isinstance(n_val, bool)
            or n_val < 1
            or t_val != n_val
        ):
            raise ValueError(
                f"plan target_rows ({t_val!r}) and n_rows ({n_val!r}) conflict or are invalid"
            )
        target_rows = t_val
    elif "target_rows" in plan:
        t_val = plan["target_rows"]
        if type(t_val) is not int or isinstance(t_val, bool) or t_val < 1:
            raise ValueError(
                f"plan target_rows must be positive integer, got {t_val!r}"
            )
        target_rows = t_val
    elif "n_rows" in plan:
        n_val = plan["n_rows"]
        if type(n_val) is not int or isinstance(n_val, bool) or n_val < 1:
            raise ValueError(f"plan n_rows must be positive integer, got {n_val!r}")
        target_rows = n_val
    else:
        raise ValueError("plan must specify target_rows or n_rows")

    if target_rows != contract["target_rows"]:
        raise ValueError(
            f"plan target rows {target_rows} does not match contract target_rows {contract['target_rows']}"
        )

    # Validate failed_ids
    if not isinstance(failed_ids, (list, tuple)) or isinstance(
        failed_ids, (str, bytes)
    ):
        raise TypeError(
            f"failed_ids must be a sequence of strings, got {type(failed_ids).__name__}"
        )
    for item in failed_ids:
        if not isinstance(item, str):
            raise TypeError(
                f"failed_ids items must be strings, got {type(item).__name__}"
            )

    current_strategy = plan.get("strategy", "fitted_markov_joint")
    if current_strategy not in ("fitted_markov_joint", "fitted_markov_balanced"):
        raise ValueError(f"unsupported or unknown plan strategy: {current_strategy!r}")
    repaired = copy.deepcopy(dict(plan))

    # Causal strategy move specifically for case.inclusion_fraction
    if (
        "case.inclusion_fraction" in failed_ids
        and current_strategy == "fitted_markov_joint"
    ):
        repaired["strategy"] = "fitted_markov_balanced"
        repaired["seed"] = seed
        return repaired

    return repaired


# =====================================================================
# Bounded Runtime AI Roles (StructuredModel complete interface only)
# =====================================================================

RESEARCH_CASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "case_id": {"type": "string", "maxLength": 100},
        "case_version": {"type": "integer"},
        "cited_evidence_ids": {
            "type": "array",
            "items": {"type": "string", "maxLength": 100},
            "minItems": 1,
            "maxItems": 10,
        },
        "passage_citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string", "maxLength": 100},
                    "passage_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "relevance": {"type": "string", "maxLength": 500},
                },
                "required": ["evidence_id", "passage_sha256", "relevance"],
                "additionalProperties": False,
            },
            "maxItems": 10,
        },
        "supported_requirements": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 20,
        },
        "unsupported_requirements": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 20,
        },
        "research_summary": {"type": "string", "maxLength": 2000},
        "limitations": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 20,
        },
    },
    "required": [
        "case_id",
        "case_version",
        "cited_evidence_ids",
        "supported_requirements",
        "unsupported_requirements",
        "research_summary",
        "limitations",
    ],
    "additionalProperties": False,
}


def research_case(
    model: Any, purpose: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    """Execute bounded case research advisory role.

    Only admitted installed primary evidence IDs, exact case identity/version,
    and canonical passage references may be selected.
    A model cannot invent citations, set thresholds, replace requirements,
    or approve data. Unsupported requirements are explicitly reported.
    """
    case_def, binding, contract, reference, admitted_evidence = _validate_case_context(
        context
    )

    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("purpose must be a non-empty string")

    case_id = str(binding["case_id"])
    case_version = int(binding["case_version"])
    admitted_ids = list(
        case_def.get("researched_facts", {}).get("primary_evidence_ids", [])
    )

    prompt = (
        f"You are the case research advisor for Ruloom.\n"
        f"User purpose: {purpose}\n"
        f"Installed case: {case_id} (version {case_version})\n"
        f"Case title: {case_def.get('title')}\n"
        f"Case purpose: {case_def.get('purpose_description')}\n"
        f"Case output columns: {contract.get('output_columns')}\n"
        f"Admitted primary evidence IDs: {admitted_ids}\n"
        f"Admitted passages:\n"
    )
    for eid in admitted_ids:
        if eid in admitted_evidence:
            e = admitted_evidence[eid]
            prompt += (
                f"[{eid}]: {e.get('title')} ({e.get('url')}) sha256:{e.get('passage_sha256')}\n"
                f"  Passage: {e.get('verbatim_passage')}\n"
                f"  Supports: {e.get('supports')}\n"
            )

    prompt += (
        "\nSelect and explain relevant admitted evidence. "
        "Do NOT invent citations or references. Do NOT approve data or alter thresholds. "
        "Any requests for geological predictions, core photographs, reflectance curves, "
        "privacy guarantees, or live web search must be reported in unsupported_requirements. "
        "The admitted passages show that the teaching activity is real practice; they "
        "are not the data source. Depth spacing, inclusion flags, blank-label patterns "
        "and the label vocabulary are fitted by deterministic code from the pinned "
        "source and checked by the case contract, so a request for those features is "
        "within this case even though the passages contain no values. List in "
        "unsupported_requirements only requests outside the case contract and output "
        "columns above, such as new columns, identifiers, coordinates, images, spectra "
        "or curves, custom tolerances, or the domains named above."
        + prohibited_prose_contract(("research_summary", "limitations"))
    )

    resp = _invoke_structured_model(model, prompt, RESEARCH_CASE_SCHEMA)

    # Code-side strict validation of response
    if resp.get("case_id") != case_id:
        raise ValueError(
            f"model returned case_id {resp.get('case_id')!r}, expected {case_id!r}"
        )
    if resp.get("case_version") != case_version:
        raise ValueError(
            f"model returned case_version {resp.get('case_version')!r}, expected {case_version!r}"
        )

    cited = resp.get("cited_evidence_ids", [])
    if not cited:
        raise ValueError("cited_evidence_ids cannot be empty")
    if len(set(cited)) != len(cited):
        raise ValueError("duplicate cited evidence IDs")
    for cid in cited:
        if cid != cid.strip():
            raise ValueError(f"cited evidence ID {cid!r} has invalid whitespace")

    validate_citations(cited, allowed_ids=admitted_ids)

    # Validate passage citations if returned: must be admitted in case AND in cited_evidence_ids
    for p_cite in resp.get("passage_citations", []):
        p_eid = p_cite.get("evidence_id")
        p_sha = p_cite.get("passage_sha256")
        if p_eid not in admitted_ids:
            raise ValueError(
                f"model cited passage evidence ID {p_eid!r} not admitted by case {case_id!r}"
            )
        if p_eid not in cited:
            raise ValueError(
                f"passage citation {p_eid!r} was not declared in cited_evidence_ids"
            )
        expected_sha = admitted_evidence[p_eid].get("passage_sha256")
        if p_sha != expected_sha:
            raise ValueError(
                f"model returned tampered or invented passage hash {p_sha!r} for {p_eid!r}; "
                f"expected {expected_sha!r}"
            )

    # Validate supported_requirements: cannot claim unsupported domains affirmatively
    supported_reqs = resp.get("supported_requirements", [])
    for s_item in supported_reqs:
        s_lower = s_item.lower()
        for domain in UNSUPPORTED_DOMAINS:
            if domain in s_lower and _is_affirmatively_requested(s_item, domain):
                raise ValueError(
                    f"model claimed unsupported requirement as supported: {s_item!r}"
                )

    unsupported = list(resp.get("unsupported_requirements", []))
    for domain in UNSUPPORTED_DOMAINS:
        if _is_affirmatively_requested(purpose, domain):
            if not any(domain in item.lower() for item in unsupported):
                unsupported.append(
                    f"Domain requirement not supported by bounded case: {domain}"
                )

    # Reject fraudulent free-form claims in research_summary and limitations
    summary_text = str(resp.get("research_summary", ""))
    _check_prohibited_prose(summary_text, "research_summary")
    for index, lim in enumerate(resp.get("limitations", [])):
        _check_prohibited_prose(lim, f"limitations[{index}]")

    return {
        "advisory_kind": "case_research",
        "case_id": case_id,
        "case_version": case_version,
        "cited_evidence_ids": list(cited),
        "passage_citations": resp.get("passage_citations", []),
        "supported_requirements": supported_reqs,
        "unsupported_requirements": unsupported,
        "research_summary": summary_text,
        "limitations": resp.get("limitations", []),
        "authority_note": (
            "Research advisory only. The model has no authority to admit new sources, "
            "invent citations, set thresholds, replace requirements, or approve data. "
            "Code-side validation enforces exact case contract and admitted evidence."
        ),
    }


ASSESS_SOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "case_id": {"type": "string", "maxLength": 100},
        "source_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "cited_evidence_ids": {
            "type": "array",
            "items": {"type": "string", "maxLength": 100},
            "minItems": 1,
            "maxItems": 10,
        },
        "fit_assessment": {"type": "string", "maxLength": 1000},
        "source_limitations": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 20,
        },
        "teaching_suitability": {"type": "string", "maxLength": 500},
    },
    "required": [
        "case_id",
        "source_sha256",
        "cited_evidence_ids",
        "fit_assessment",
        "source_limitations",
        "teaching_suitability",
    ],
    "additionalProperties": False,
}


def assess_case_source(model: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    """Execute bounded source assessment advisory role.

    Always regenerates the disclosed aggregate summary from the validated reference,
    withholding literal label names, raw rows, and free-form licence text.
    Deterministic preparation owns suitability; model explains fit/limits.
    """
    case_def, binding, contract, reference, _ = _validate_case_context(context)

    case_id = str(binding["case_id"])
    source_sha = str(binding["source_sha256"])

    # Always freshly regenerate disclosed model summary from validated reference
    disclosed = build_disclosed_model_summary(reference)

    fit_receipt = context.get("source_fit_receipt") or context.get("source_fit", {})
    if fit_receipt.get("suitable") is False:
        raise ValueError("source is not suitable according to deterministic receipt")

    admitted_ids = list(
        case_def.get("researched_facts", {}).get("primary_evidence_ids", [])
    )

    # Format bounded prompt with numbers only (no raw rows, no literal label vocab)
    prompt = (
        f"You are the source assessment advisor for Ruloom.\n"
        f"Case: {case_id}\n"
        f"Source SHA-256: {source_sha}\n"
        f"Deterministic fit status: {fit_receipt.get('suitable')}\n"
        f"Source rows: {reference.get('source_rows')}, usable: {reference.get('usable_rows')}\n"
        f"Valid fraction: {disclosed.get('valid_fraction')}\n"
        f"Median spacing: {disclosed.get('median_forward_spacing')}\n"
        f"SWIR answer rate: {disclosed.get('swir_answer_rate')}\n"
        f"TIR answer rate: {disclosed.get('tir_answer_rate')}\n"
        f"Has blanks: {disclosed.get('has_blanks')}\n"
        f"Rights status: {disclosed.get('rights_status')}\n\n"
        f"Admitted evidence IDs: {admitted_ids}\n"
        "Explain source fit and limitations for the teaching lesson. "
        "You have no authority to approve sources or waive code checks."
        + prohibited_prose_contract(
            ("fit_assessment", "teaching_suitability", "source_limitations")
        )
    )

    resp = _invoke_structured_model(model, prompt, ASSESS_SOURCE_SCHEMA)

    if resp.get("case_id") != case_id:
        raise ValueError(
            f"model returned case_id {resp.get('case_id')!r}, expected {case_id!r}"
        )
    if resp.get("source_sha256") != source_sha:
        raise ValueError(
            f"model returned source_sha256 {resp.get('source_sha256')!r}, expected {source_sha!r}"
        )

    cited = resp.get("cited_evidence_ids", [])
    validate_citations(cited, allowed_ids=admitted_ids)

    # Check for fraudulent claims in fit_assessment, teaching_suitability, and limitations
    fit_text = str(resp.get("fit_assessment", ""))
    _check_prohibited_prose(fit_text, "fit_assessment")
    _check_prohibited_prose(
        str(resp.get("teaching_suitability", "")), "teaching_suitability"
    )
    for index, lim in enumerate(resp.get("source_limitations", [])):
        _check_prohibited_prose(lim, f"source_limitations[{index}]")

    return {
        "advisory_kind": "source_assessment",
        "case_id": case_id,
        "source_sha256": source_sha,
        "cited_evidence_ids": list(cited),
        "fit_assessment": fit_text,
        "source_limitations": resp.get("source_limitations", []),
        "teaching_suitability": str(resp.get("teaching_suitability", "")),
        "authority_note": (
            "Source assessment advisory only. Deterministic intake and suitability "
            "rules own source validation; the model has no authority to waive checks "
            "or approve source data."
        ),
    }


REVIEW_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "case_id": {"type": "string", "maxLength": 100},
        "candidate_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "code_verdict_acknowledged": {
            "type": "string",
            "enum": ["pass", "fail"],
        },
        "operator_recommendation": {
            "type": "string",
            "enum": [
                "recommend_proceed_to_human_gate",
                "recommend_reject",
                "recommend_investigate",
            ],
        },
        "evidence_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "measurement": {
                        "type": "string",
                        "enum": list(EVIDENCE_FACT_MEASUREMENT_PATHS),
                    },
                    "value": {
                        "type": "number",
                    },
                },
                "required": ["measurement", "value"],
                "additionalProperties": False,
            },
            "minItems": 1,
            "maxItems": 20,
        },
        "evaluator_opinion": {"type": "string", "maxLength": 1000},
        "strengths": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 20,
        },
        "concerns": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 20,
        },
    },
    "required": [
        "case_id",
        "candidate_sha256",
        "code_verdict_acknowledged",
        "operator_recommendation",
        "evidence_facts",
        "evaluator_opinion",
        "strengths",
        "concerns",
    ],
    "additionalProperties": False,
}


def review_case_candidate(
    model: Any,
    candidate_bytes: bytes,
    context: Mapping[str, Any],
    utility_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute bounded candidate reviewer advisory role.

    Operates only on measured reports, never raw candidate or source rows.
    Sanitizes candidate-controlled label strings to prevent prompt injection.
    Code recomputes checks and refuses contradictory claimed verdicts.
    A reviewer provides operator advisory recommendations, not acceptance.
    """
    case_def, binding, contract, reference, _ = _validate_case_context(context)

    if not isinstance(candidate_bytes, (bytes, bytearray)):
        raise TypeError(
            f"candidate_bytes must be bytes, got {type(candidate_bytes).__name__}"
        )
    candidate_bytes = bytes(candidate_bytes)
    cand_sha = hashlib.sha256(candidate_bytes).hexdigest()

    if not isinstance(utility_report, Mapping) or isinstance(
        utility_report, (str, bytes)
    ):
        raise TypeError(
            f"utility_report must be a mapping, got {type(utility_report).__name__}"
        )
    if utility_report.get("candidate_sha256") != cand_sha:
        raise ValueError("candidate bytes sha256 does not match utility report")

    case_id = str(binding["case_id"])

    # Recompute checks independently
    recomputed = evaluate_case_utility(candidate_bytes, reference, contract)
    expected_verdict = "pass" if recomputed.get("passed") else "fail"

    # Supplied utility_report must strictly match recomputed results
    cand_verify = verify_case_candidate(
        candidate_bytes, reference, contract, reported_utility=utility_report
    )
    if cand_verify.get("matches_reported") is not True:
        raise ValueError(
            f"supplied utility report contradicts independent recomputation: {cand_verify.get('discrepancies')}"
        )

    # Sanitize measurements for prompt: exclude candidate-controlled unknown label strings
    sanitized_measurements: dict[str, Any] = {}
    for k, v in recomputed.get("measurements", {}).items():
        if k in ("swir_supported_labels", "tir_supported_labels") and isinstance(
            v, Mapping
        ):
            unknown_list = v.get("unknown_labels", [])
            sanitized_measurements[k] = {
                "unknown_labels_count": len(unknown_list)
                if isinstance(unknown_list, (list, tuple, set))
                else 0
            }
        elif k == "output_columns":
            sanitized_measurements[k] = {
                "columns_match": v == contract.get("output_columns")
            }
        else:
            sanitized_measurements[k] = v

    available_numeric_measurements: dict[str, int | float] = {}
    for p in EVIDENCE_FACT_MEASUREMENT_PATHS:
        try:
            available_numeric_measurements[p] = _lookup_measurement_path(
                recomputed.get("measurements", {}), p
            )
        except Exception:
            pass

    prompt = (
        f"You are the candidate review advisor for Ruloom.\n"
        f"Case: {case_id}\n"
        f"Candidate SHA-256: {cand_sha}\n"
        f"Code check outcome: {expected_verdict}\n"
        f"Failures: {recomputed.get('failures', [])}\n"
        f"Measurements: {json.dumps(sanitized_measurements, indent=2)}\n"
        f"Limitations: {recomputed.get('limitations', [])}\n"
        f"Available verified numeric measurement paths:\n{json.dumps(available_numeric_measurements, indent=2)}\n\n"
        "Provide an advisory review for the human operator. "
        "Acknowledge the code check outcome honestly. "
        "evidence_facts must be a non-empty list of structured objects: "
        '{"measurement": "<path>", "value": <exact numeric value>} '
        "referencing only verified measurement paths from the map above. "
        "Evaluator opinion is unverified advisory text and carries no authority to accept data or alter checks."
        + prohibited_prose_contract(("evaluator_opinion", "strengths", "concerns"))
    )

    resp = _invoke_structured_model(model, prompt, REVIEW_CANDIDATE_SCHEMA)

    if resp.get("case_id") != case_id:
        raise ValueError(
            f"model returned case_id {resp.get('case_id')!r}, expected {case_id!r}"
        )
    if resp.get("candidate_sha256") != cand_sha:
        raise ValueError(
            f"model returned candidate_sha256 {resp.get('candidate_sha256')!r}, expected {cand_sha!r}"
        )

    # Refuse contradictory claims
    claimed_verdict = resp.get("code_verdict_acknowledged")
    if claimed_verdict != expected_verdict:
        raise ValueError(
            f"model claimed code verdict {claimed_verdict!r}, but code recomputation is {expected_verdict!r}; "
            "contradictory verdict claims are refused."
        )

    rec = resp.get("operator_recommendation")
    if expected_verdict == "fail" and rec == "recommend_proceed_to_human_gate":
        raise ValueError(
            "model recommended proceed to human gate on failing data; contradictory recommendation is refused."
        )

    # Validate evidence_facts against recomputed measurements and prohibited prose
    evidence_facts = resp.get("evidence_facts", [])
    _validate_evidence_facts(evidence_facts, recomputed.get("measurements", {}))

    opinion_text = str(resp.get("evaluator_opinion", ""))
    _check_prohibited_prose(opinion_text, "evaluator_opinion")
    for index, s in enumerate(resp.get("strengths", [])):
        _check_prohibited_prose(s, f"strengths[{index}]")
    for index, c in enumerate(resp.get("concerns", [])):
        _check_prohibited_prose(c, f"concerns[{index}]")

    return {
        "advisory_kind": "candidate_review",
        "case_id": case_id,
        "candidate_sha256": cand_sha,
        "code_verdict_acknowledged": claimed_verdict,
        "operator_recommendation": rec,
        "evidence_facts": evidence_facts,
        "evaluator_opinion": opinion_text,
        "strengths": resp.get("strengths", []),
        "concerns": resp.get("concerns", []),
        "authority_note": (
            "Operator advisory review only. A reviewer cannot accept data, change "
            "seed, modify checks, or bypass the human gate. The final gate decision "
            "remains exclusively with the human operator."
        ),
    }
