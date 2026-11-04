"""Bounded capability context for request interpretation.

Request preparation tells the interpreting model what the selected
profile can actually do on this route, built only from validated
manifest declarations: the profile-owned ``generation.request_context``
brief (when present), the implemented strategy names, the checks that
apply to a scalar run (required versus optional), and the release
members a scalar run declares. Checks gated on a run condition that
this route never establishes are omitted from the menu and only
counted, so the model is never told that an unrequested modality is
available. No source bytes, rows or statistics are read here: the
context is a pure function of the manifest, and its canonical digest
(computed by the caller with the shared hashing helper) is saved with
the draft so the interpretation can be audited later.

The engine never interprets the brief's wording. Domain meaning lives
in each profile's ``profile.json``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.manifest import ENGINE_REPORT_MEMBER

#: Version of the capability-context record layout.
CONTEXT_VERSION = 1

#: Output modalities this request route can draft. Anything else is
#: refused by the response schema, so the context never advertises it.
ROUTE_OUTPUTS = ("scalar",)

#: Upper bound on the serialised context (characters). A manifest whose
#: declarations exceed it is refused before any model contact rather
#: than silently truncated.
MAX_CONTEXT_CHARS = 24000

#: Fixed statements about what the context is and is not. They describe
#: engine behaviour (proposal versus measurement, pass/fail/skip
#: semantics) and hold for every profile.
STATUS = (
    "This context describes a proposed plan only. Generation, checks and "
    "reports have not run; measured results exist only after generation "
    "and validation, and any required check can fail."
)

CHECK_SEMANTICS = (
    "Required checks must each record a pass for the run to become "
    "eligible for human acceptance; a failed or skipped required check "
    "blocks it. Optional checks are recorded and shown but never gate "
    "acceptance; an optional check may be skipped (for example when its "
    "band is uninformative) and a skip is never a pass. Thresholds and "
    "tolerances are fixed by the profile and cannot be set per request."
)

ROUTE_NOTE = (
    "This request route drafts scalar output only. Checks that apply only "
    "under a run condition this route never establishes are omitted here "
    "and will not run on the drafted plan."
)

NO_BRIEF_NOTE = (
    "This profile declares no capability brief. Only the strategy names, "
    "checks and release members below are known; do not assume what a strategy "
    "preserves, and list any requirement that depends on such an "
    "assumption as unsupported."
)


def _release_members(release: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Unconditional declared release members, with their origin."""
    members = release.get("members", []) if isinstance(release, Mapping) else []
    out: list[dict[str, Any]] = []
    for entry in members if isinstance(members, list) else []:
        if not isinstance(entry, Mapping) or entry.get("when") is not None:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        record: dict[str, Any] = {
            "name": name,
            "required": bool(entry.get("required", True)),
        }
        if name == ENGINE_REPORT_MEMBER and entry.get("source") == "engine-report":
            record["origin"] = (
                "rendered by the engine after the run from stored records: "
                "lists every recorded check with required/optional status "
                "and pass/fail/skip outcome"
            )
        out.append(record)
    return out


def _route_checks(
    checks: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Split declared checks into required, optional and an omitted count.

    Conditional checks are counted but neither named nor described, so a
    scalar draft never advertises the modality their condition implies.
    """
    required: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    omitted = 0
    for entry in checks:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("applicable_when") is not None:
            omitted += 1
            continue
        threshold = entry.get("threshold")
        record = {
            "id": entry.get("id"),
            "kind": entry.get("kind"),
            "threshold_source": (
                threshold.get("source") if isinstance(threshold, Mapping) else None
            ),
        }
        if entry.get("required", True) is False:
            optional.append(record)
        else:
            required.append(record)
    return required, optional, omitted


def build_capability_context(manifest: Any) -> dict[str, Any]:
    """The bounded, manifest-derived capability context for one profile.

    Raises ``ValueError`` when the profile declares no output this route
    can draft or when the serialised context exceeds
    :data:`MAX_CONTEXT_CHARS`; both happen before any model contact.
    """
    generation = getattr(manifest, "generation", None)
    if not isinstance(generation, Mapping):
        generation = {}
    declared_outputs = generation.get("outputs", ["scalar"])
    outputs = [name for name in ROUTE_OUTPUTS if name in declared_outputs]
    if not outputs:
        raise ValueError(
            f"profile {manifest.id!r} declares no output this request route drafts"
        )
    strategies = list(generation.get("strategies", []) or [])
    required, optional, omitted = _route_checks(list(manifest.checks))
    brief = generation.get("request_context")
    context: dict[str, Any] = {
        "context_version": CONTEXT_VERSION,
        "profile": manifest.id,
        "manifest_digest": manifest.digest,
        "status": STATUS,
        "route": {"outputs": outputs, "note": ROUTE_NOTE},
        "strategies": strategies,
        "default_strategy": generation.get("default_strategy"),
        "max_rows": manifest.limits.get("max_rows"),
        "profile_brief": dict(brief) if isinstance(brief, Mapping) else None,
        "checks": {
            "semantics": CHECK_SEMANTICS,
            "required": required,
            "optional": optional,
            "omitted_conditional_count": omitted,
        },
        "release_members": _release_members(getattr(manifest, "release", {}) or {}),
    }
    if context["profile_brief"] is None:
        context["no_brief_note"] = NO_BRIEF_NOTE
    size = len(render_capability_context(context))
    if size > MAX_CONTEXT_CHARS:
        raise ValueError(
            f"capability context for profile {manifest.id!r} is {size} "
            f"characters (limit {MAX_CONTEXT_CHARS}): refusing before model contact"
        )
    return context


def render_capability_context(context: Mapping[str, Any]) -> str:
    """Deterministic JSON text of the context, as placed in the prompt."""
    return json.dumps(context, sort_keys=True, ensure_ascii=False)
