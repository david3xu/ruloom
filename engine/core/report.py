"""User-facing evidence summary rendered from stored records only.

The report is a view, not new evidence: every number it shows is read
from a record the run already persisted (the run record, one attempt
receipt, the frozen candidate manifest, the human decision, the
release rights block), and every section names the record its numbers
were read from. It recomputes no quality measure and duplicates no
check: presenting the same measurement twice would look like extra
value where there is none.

The renderer takes a single ``evidence`` mapping assembled by the
engine from those stored records; it never opens files and never
calls a check. ``manifest_checks`` carries the profile's check
declarations so each result is shown with its required/optional
status.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .verdict import applicable_required_ids


def _short(value: Any, width: int = 120) -> str:
    """One-line rendering of a stored value for the report."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split()).replace("|", "/")
    if len(text) > width:
        return text[: width - 1] + "…"
    return text


def render_evidence_report(evidence: Mapping[str, Any]) -> str:
    """Render the evidence summary markdown for one run.

    ``evidence`` keys: ``run_id``, ``profile_id``,
    ``profile_version``, ``spec``, ``spec_digest``, ``source``,
    ``attempt_no`` (or None), ``plan``, ``check_results`` (stored
    result dicts), ``manifest_checks`` (declarations), ``conditions``
    (applicability facts), ``decision`` (or None), ``frozen_digest``
    (or None), ``frozen_members`` (list), ``rights``, ``refs``
    (label-to-record-path citations), and ``images_present``.
    """
    get = evidence.get
    lines = [
        "# Evidence report",
        "",
        f"Run: {get('run_id')}",
        f"Profile: {get('profile_id')} v{get('profile_version')}",
    ]
    spec = get("spec") or {}
    lines += [
        f"Spec: {spec.get('spec_id')} v{spec.get('spec_version')} "
        f"(digest {get('spec_digest')})",
    ]
    refs = get("refs") or {}
    if isinstance(refs, Mapping) and refs.get("run"):
        lines.append(f"Run record: {refs.get('run')}")
    lines.append("")

    lines += ["## Requested outputs and strategy", ""]
    lines.append(f"Outputs: {_short(spec.get('outputs'))}")
    lines.append(f"Strategy: {_short(spec.get('strategy'))}")
    lines.append(f"Row bound: {_short((spec.get('limits') or {}).get('max_rows'))}")
    lines.append(
        f"Attempt bound: {_short((spec.get('limits') or {}).get('max_attempts'))}"
    )
    if isinstance(refs, Mapping) and refs.get("spec"):
        lines.append(f"Spec record: {refs.get('spec')}")
    lines.append("")

    lines += ["## Source", ""]
    source = get("source") or {}
    lines.append(f"Source name: {source.get('source_name')}")
    lines.append(f"Source sha256: {source.get('source_sha256')}")
    lines.append(f"Source hash verified: {source.get('hash_verified')}")
    if source.get("manifest_name"):
        lines.append(f"Registry record: {source.get('manifest_name')}")
    if source.get("licence") or source.get("source_attribution"):
        lines.append(f"Licence: {source.get('licence')}")
        lines.append(f"Attribution: {source.get('attribution')}")
    if isinstance(refs, Mapping) and refs.get("source"):
        lines.append(f"Source record: {refs.get('source')}")
    lines.append("")

    lines += ["## Plan", ""]
    plan = get("plan") or {}
    attempt_no = get("attempt_no")
    lines.append(
        f"Selected attempt: {attempt_no if attempt_no is not None else 'none'}"
    )
    lines.append(f"Seed: {_short(plan.get('seed'))}")
    lines.append(f"Rows: {_short(plan.get('n_rows'))}")
    lines.append(f"Strategy: {_short(plan.get('strategy', spec.get('strategy')))}")
    if isinstance(refs, Mapping) and refs.get("attempt"):
        lines.append(f"Attempt record: {refs.get('attempt')}")
    lines.append("")

    lines += ["## Checks", ""]
    manifest_checks = get("manifest_checks") or []
    conditions = get("conditions") or {}
    if isinstance(conditions, Mapping):
        conditions = dict(conditions)
    else:
        conditions = {}
    try:
        required = set(
            applicable_required_ids(
                list(manifest_checks) if isinstance(manifest_checks, list) else [],
                conditions=conditions,
            )
        )
    except Exception:  # noqa: BLE001 -- declarations drive display only
        required = set()
    declared = {
        entry.get("id"): entry
        for entry in (manifest_checks if isinstance(manifest_checks, list) else [])
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }
    results = get("check_results") or []
    if not isinstance(results, list) or not results:
        lines.append("No check results recorded.")
    else:
        lines.append("| check | required | status | measured | threshold |")
        lines.append("|---|---|---|---|---|")
        for result in results:
            if not isinstance(result, Mapping):
                continue
            check_id = str(result.get("check_id"))
            entry = declared.get(check_id, {})
            if check_id in required:
                req = "yes"
            elif entry.get("required") is True:
                condition = entry.get("applicable_when")
                req = (
                    f"no ({condition} unmet)"
                    if condition
                    else "no (declaration)"
                )
            else:
                req = "no"
            lines.append(
                f"| {check_id} | {req} | {result.get('status')} "
                f"| {_short(result.get('measured'))} "
                f"| {_short(result.get('threshold'))} |"
            )
    if isinstance(refs, Mapping) and refs.get("checks"):
        lines.append(f"Check records: {refs.get('checks')}")
    lines.append("")

    lines += ["## Decision and frozen evidence", ""]
    decision = get("decision")
    if isinstance(decision, Mapping):
        lines.append(f"Decision: {decision.get('verdict')}")
        lines.append(f"Decided by: {decision.get('decided_by')}")
        lines.append(f"Note: {_short(decision.get('note'))}")
    else:
        lines.append("Decision: none (the run never reached an accepted gate)")
    frozen_digest = get("frozen_digest")
    if frozen_digest:
        lines.append(f"Frozen evidence digest: {frozen_digest}")
        members = get("frozen_members") or []
        if isinstance(members, list) and members:
            lines.append("Frozen members:")
            for entry in members:
                if not isinstance(entry, Mapping):
                    continue
                lines.append(
                    f"- {entry.get('path')} "
                    f"(sha256 {entry.get('sha256')}, "
                    f"attempt {entry.get('attempt')})"
                )
    else:
        lines.append("Frozen evidence digest: none (no frozen snapshot)")
    for label in ("frozen", "decision"):
        if isinstance(refs, Mapping) and refs.get(label):
            lines.append(f"{label.capitalize()} record: {refs.get(label)}")
    lines.append("")

    lines += ["## Rights", ""]
    rights = get("rights") or {}
    lines.append(f"Source sha256: {rights.get('source_sha256')}")
    lines.append(f"Source hash verified: {rights.get('source_hash_verified')}")
    lines.append(f"Output terms: {rights.get('output_terms')}")
    if rights.get("output_terms_grant") is not None:
        lines.append(
            f"Grant evidence: {rights.get('output_terms_grant_field')} = "
            f"{_short(rights.get('output_terms_grant'))}"
        )
    if isinstance(refs, Mapping) and refs.get("release"):
        lines.append(f"Release record: {refs.get('release')}")
    lines.append("")

    lines += ["## Limitations", ""]
    lines.append(
        "- Model prompts on this run carried disclosed statistics only; "
        "values shared by fewer than two source rows are withheld from "
        "the model view and never become data values."
    )
    if get("images_present"):
        lines.append(
            "- Imagery on this run is schematic-only: synthetic renders "
            "are seeded code fitted to code-computed statistics, and the "
            "image checks judge strip order plus scene geometry only. No "
            "claim about registered core photography is made."
        )
    lines.append(
        "- Checks marked not-required above are shown for context and "
        "never gate acceptance."
    )
    lines.append(
        "- This report renders stored records only; it recomputes no "
        "quality measure. Every number above names the record it was "
        "read from."
    )
    lines.append("")
    return "\n".join(lines)
