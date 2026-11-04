"""Advisory vision critique over SYNTHETIC images only.

A vision model may look at a rendered synthetic PNG and return a
structured advisory note. The note is logged through the engine's
advisory channel and can never change a pass/fail verdict (the engine
excludes advisories from verdict computation; the ``image_critique``
capability declares ``advisory: true`` and never calls
``record_checks``).

Boundaries, enforced by construction:

* the image path handed to the model is always the synthetic artifact
  written by seeded code — raw source imagery is never attached;
* the prompt carries a whitelisted numeric statistics summary only
  (dimensions, histograms, channel means, texture, strip means) — no
  source name, source hash, or free text;
* the model client is duck-typed (it only needs a
  ``complete_with_image`` method), so this module imports no model code
  and the profile/model layer boundary stays intact;
* the answer must satisfy :data:`CRITIC_SCHEMA` exactly and is
  re-validated locally (even for injected clients): the model must echo
  the exact artifact SHA-256 and the exact normalized deterministic
  check facts, it cannot edit them, and it may cite only supplied check
  IDs — nothing is coerced, defaulted, or silently filtered;
* every outcome (success, unavailable, failure) is captured as a
  version-1 receipt bound to the artifact digest and the deterministic
  facts, sealed by ``receipt_sha256``; failure receipts never echo a
  provider exception body.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class CriticError(Exception):
    """The vision critique could not run or returned an unusable answer.

    ``dispatched`` records whether the model call was made before the
    failure; ``provenance`` carries the completion's reported identity
    when a (rejected) answer was received. Messages never contain
    model-supplied or provider-supplied text.
    """

    def __init__(
        self,
        message: str,
        *,
        dispatched: bool = False,
        provenance: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.dispatched = dispatched
        self.provenance = dict(provenance) if provenance is not None else None


#: Receipt contract version.
RECEIPT_VERSION = 1

#: Receipt statuses: a validated opinion, or an explicit non-opinion.
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "advisory-failed"
RECEIPT_STATUSES = (STATUS_OK, STATUS_UNAVAILABLE, STATUS_FAILED)

#: How the model identity in a receipt is evidenced.
EVIDENCE_PROVIDER = "provider_reported"
EVIDENCE_INJECTED = "injected"
EVIDENCE_UNVERIFIED = "unverified"
EVIDENCE_NOT_DISPATCHED = "not_dispatched"
EVIDENCE_KINDS = (
    EVIDENCE_PROVIDER,
    EVIDENCE_INJECTED,
    EVIDENCE_UNVERIFIED,
    EVIDENCE_NOT_DISPATCHED,
)

#: Genuine adapter identities admitted as ``provider_reported``.
_PROVIDER_ADAPTERS = {"meta": "meta_cli", "azure": "azure_foundry"}

#: Deterministic check statuses produced by the image checks.
CHECK_STATUSES = ("pass", "fail", "skip")

#: The four structured observation categories.
OBSERVATION_KEYS = (
    "broken_bands",
    "abrupt_colour_changes",
    "cropping_or_clipping",
    "rendering_defects",
)

MAX_NOTE_CHARS = 2000
MAX_OBSERVATION_CHARS = 1000
MAX_CITED_CHECKS = 20
_MAX_IDENTITY_CHARS = 120
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_USAGE_INT_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "reasoning_tokens",
    "calls",
)
_RECEIPT_KEYS = frozenset(
    {
        "version",
        "status",
        "artifact_sha256",
        "deterministic_checks",
        "model",
        "provider",
        "adapter",
        "evidence_kind",
        "usage",
        "cost",
        "observations",
        "note",
        "cited_check_ids",
        "disclaimer",
        "receipt_sha256",
    }
)

#: Explicit teaching-material scope limit: no geological or certification claim.
TEACHING_REVIEW_DISCLAIMER = (
    "Advisory teaching-material review of a synthetic rendering only; it cannot "
    "override deterministic checks or human review. Does not certify geological "
    "veracity, mineral identity, grade, privacy, licensing, or real core "
    "photography. The source image is not attached, so loss of original framing "
    "cannot be assessed."
)

#: Markers around the machine-readable echo block inside the prompt.
ECHO_BEGIN = "BEGIN_ECHO_JSON"
ECHO_END = "END_ECHO_JSON"

_FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "check_id": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": list(CHECK_STATUSES)},
        "detail": {"type": "string"},
    },
    "required": ["check_id", "status", "detail"],
    "additionalProperties": False,
}


def _observation_schema(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_OBSERVATION_CHARS,
        "description": description,
    }


#: Strict schema for the vision-critic answer: every field required.
CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "description": "Exact echo of the supplied synthetic artifact SHA-256.",
        },
        "deterministic_checks": {
            "type": "array",
            "items": _FACT_SCHEMA,
            "description": "Exact, unedited echo of the supplied deterministic check facts.",
        },
        "note": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_NOTE_CHARS,
            "description": "Concise advisory summary for a teaching-material reviewer.",
        },
        "broken_bands": _observation_schema(
            "Visible strip banding continuity or unnatural breaks."
        ),
        "abrupt_colour_changes": _observation_schema(
            "Visible abrupt colour changes; intentional band transitions may be normal."
        ),
        "cropping_or_clipping": _observation_schema(
            "Visible framing, cropping, or clipping defects within the synthetic image."
        ),
        "rendering_defects": _observation_schema(
            "Visible procedural rendering defects, noise, or artefacts."
        ),
        "cited_check_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": MAX_CITED_CHECKS,
            "uniqueItems": True,
            "description": "Deterministic check IDs (from the supplied facts only) cited in the review.",
        },
    },
    "required": [
        "artifact_sha256",
        "deterministic_checks",
        "note",
        *OBSERVATION_KEYS,
        "cited_check_ids",
    ],
    "additionalProperties": False,
}

_RESPONSE_KEYS = frozenset(CRITIC_SCHEMA["required"])


def _is_str(value: Any) -> bool:
    return type(value) is str


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _bounded_text(value: Any, limit: int) -> bool:
    return _is_str(value) and bool(value.strip()) and len(value) <= limit


def _is_hex_digest(value: Any) -> bool:
    return _is_str(value) and _SHA256_RE.fullmatch(value) is not None


def normalize_check_facts(results: Any) -> list[dict[str, str]]:
    """Normalize deterministic image check results to exact fact records.

    Accepts the ``results`` list of ``CheckReport.to_dict()`` (``name``,
    ``status``, ``detail``; ``band_position`` is ignored) or already
    normalized records (``check_id``, ``status``, ``detail``). Types are
    checked strictly; duplicates, unknown statuses, and records naming
    both or neither identifier are rejected. Raises :class:`CriticError`.
    """
    if not isinstance(results, (list, tuple)) or not results:
        raise CriticError("deterministic check facts must be a non-empty list")
    facts: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, Mapping):
            raise CriticError("deterministic check fact is not a mapping")
        has_name, has_id = "name" in item, "check_id" in item
        if has_name == has_id:
            raise CriticError(
                "deterministic check fact must name exactly one of 'name' or 'check_id'"
            )
        check_id = item["name"] if has_name else item["check_id"]
        status = item.get("status")
        detail = item.get("detail")
        if not _is_str(check_id) or not check_id.strip():
            raise CriticError("deterministic check id must be a non-empty string")
        if not _is_str(status) or status not in CHECK_STATUSES:
            raise CriticError("deterministic check status is not pass, fail or skip")
        if not _is_str(detail):
            raise CriticError("deterministic check detail must be a string")
        if check_id in seen:
            raise CriticError("deterministic check facts contain a duplicate id")
        seen.add(check_id)
        facts.append({"check_id": check_id, "status": status, "detail": detail})
    return facts


def _numeric_channels(value: Any) -> dict[str, float | int] | None:
    if not isinstance(value, Mapping):
        return None
    out = {ch: value[ch] for ch in ("r", "g", "b") if _is_number(value.get(ch))}
    return out or None


def sanitize_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only whitelisted numeric statistics for the model prompt.

    Retained: ``width``/``height``, ``histograms`` (r/g/b integer
    counts), ``means``/``texture_h``/``texture_v`` (r/g/b numbers), and
    ``strips`` entries limited to numeric ``index``, ``row_lo``,
    ``row_hi``, ``mean_r``, ``mean_g``, ``mean_b``. Everything else —
    source name, source hash, any text — is dropped recursively.
    """
    if not isinstance(summary, Mapping):
        raise CriticError("statistics summary must be a mapping")
    clean: dict[str, Any] = {}
    for key in ("width", "height"):
        if _is_count(summary.get(key)):
            clean[key] = summary[key]
    histograms = summary.get("histograms")
    if isinstance(histograms, Mapping):
        kept = {
            ch: list(histograms[ch])
            for ch in ("r", "g", "b")
            if isinstance(histograms.get(ch), (list, tuple))
            and all(_is_count(v) for v in histograms[ch])
        }
        if kept:
            clean["histograms"] = kept
    for key in ("means", "texture_h", "texture_v"):
        channels = _numeric_channels(summary.get(key))
        if channels is not None:
            clean[key] = channels
    strips = summary.get("strips")
    if isinstance(strips, (list, tuple)):
        kept_strips = []
        for strip in strips:
            if not isinstance(strip, Mapping):
                continue
            entry = {
                field: strip[field]
                for field in ("index", "row_lo", "row_hi", "mean_r", "mean_g", "mean_b")
                if _is_number(strip.get(field))
            }
            if entry:
                kept_strips.append(entry)
        if kept_strips:
            clean["strips"] = kept_strips
    return clean


def build_critic_prompt(
    summary: Mapping[str, Any],
    artifact_sha256: str,
    deterministic_checks: Sequence[Mapping[str, Any]],
) -> str:
    """Build the critique prompt from a sanitized numeric summary only."""
    if not _is_hex_digest(artifact_sha256):
        raise CriticError("artifact SHA-256 must be 64 lowercase hex characters")
    facts = normalize_check_facts(list(deterministic_checks))
    echo = json.dumps(
        {"artifact_sha256": artifact_sha256, "deterministic_checks": facts},
        sort_keys=True,
    )
    lines = [
        "You are an advisory critic for a SYNTHETIC banded core-strip image used in "
        "TEACHING materials.",
        "The attached image was rendered by seeded deterministic code fitted to "
        "numeric statistics; it is not a photograph and not measured data.",
        "Your review is advisory: it cannot override the deterministic checks or the "
        "human reviewer, and it cannot change any pass/fail verdict.",
        "Do not make any claim about mineral identity, geology, grade, privacy, or "
        "licensing certification, and do not claim the image is an original photo.",
        "",
        "Describe visible defects on four criteria, so a human reviewing the teaching "
        "material knows where to look before using it:",
        "1. broken_bands: banding continuity or unnatural breaks in a strip;",
        "2. abrupt_colour_changes: abrupt colour shifts; intentional band transitions "
        "between strips may be normal and are not defects by themselves;",
        "3. cropping_or_clipping: framing, cropping, or clipping visible within the "
        "synthetic image; the source image is not attached, so you cannot assess "
        "whether original framing was lost;",
        "4. rendering_defects: procedural noise, artefacts, or other rendering defects.",
        "Write a non-empty statement for every criterion (say 'none visible' when "
        "nothing is seen).",
        "",
        "Reply with one JSON object matching the schema. Copy 'artifact_sha256' and "
        "'deterministic_checks' EXACTLY, unedited, from the echo block below; you "
        "may not alter them. 'cited_check_ids' may only contain check_id values "
        "from that block (unique, at most "
        f"{MAX_CITED_CHECKS}). 'note' is a concise summary of at most "
        f"{MAX_NOTE_CHARS} characters; each criterion at most "
        f"{MAX_OBSERVATION_CHARS} characters.",
        "",
        ECHO_BEGIN,
        echo,
        ECHO_END,
        "",
        "Fitted numeric statistics summary (the only source-derived content):",
        json.dumps(sanitize_summary(summary), indent=2, sort_keys=True),
    ]
    return "\n".join(lines)


def validate_critic_response(
    data: Any,
    artifact_sha256: str,
    deterministic_checks: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """Strictly validate one critic answer against :data:`CRITIC_SCHEMA`.

    Enforced locally, regardless of client: exact key set, exact echo of
    the artifact SHA-256 and the normalized facts, bounded non-empty
    strings (no coercion), and unique citations drawn from the supplied
    facts. Raises :class:`CriticError` (message free of model text).
    """
    if not isinstance(data, Mapping):
        raise CriticError("critic answer is not a JSON object")
    keys = set(data.keys())
    if keys != _RESPONSE_KEYS:
        missing = sorted(_RESPONSE_KEYS - keys)
        extra = len(keys - _RESPONSE_KEYS)
        raise CriticError(
            f"critic answer key set mismatch (missing={missing}, unexpected={extra})"
        )
    if not _is_hex_digest(data["artifact_sha256"]):
        raise CriticError("critic answer artifact_sha256 is not 64 lowercase hex")
    if data["artifact_sha256"] != artifact_sha256:
        raise CriticError("critic answer artifact_sha256 does not match the artifact")
    echoed = data["deterministic_checks"]
    if not isinstance(echoed, list) or len(echoed) != len(deterministic_checks):
        raise CriticError("critic answer deterministic_checks is not the exact echo")
    for got, want in zip(echoed, deterministic_checks):
        if (
            not isinstance(got, Mapping)
            or set(got.keys()) != {"check_id", "status", "detail"}
            or not all(_is_str(got[k]) for k in ("check_id", "status", "detail"))
            or dict(got) != dict(want)
        ):
            raise CriticError(
                "critic answer deterministic_checks is not the exact echo"
            )
    if not _bounded_text(data["note"], MAX_NOTE_CHARS):
        raise CriticError(
            f"critic answer note must be a non-empty string of at most {MAX_NOTE_CHARS} chars"
        )
    for key in OBSERVATION_KEYS:
        if not _bounded_text(data[key], MAX_OBSERVATION_CHARS):
            raise CriticError(
                f"critic answer {key} must be a non-empty string of at most "
                f"{MAX_OBSERVATION_CHARS} chars"
            )
    citations = data["cited_check_ids"]
    known = {fact["check_id"] for fact in deterministic_checks}
    if not isinstance(citations, list) or len(citations) > MAX_CITED_CHECKS:
        raise CriticError(
            f"critic answer cited_check_ids must be a list of at most {MAX_CITED_CHECKS}"
        )
    if not all(_is_str(cid) for cid in citations):
        raise CriticError("critic answer cited_check_ids must contain strings only")
    if len(set(citations)) != len(citations):
        raise CriticError("critic answer cited_check_ids contains duplicates")
    if any(cid not in known for cid in citations):
        raise CriticError(
            "critic answer cites a check id absent from the supplied facts"
        )
    return {
        "note": data["note"],
        "observations": {key: data[key] for key in OBSERVATION_KEYS},
        "cited_check_ids": list(citations),
    }


def _identity(value: Any) -> str | None:
    if _is_str(value) and value.strip() and len(value) <= _MAX_IDENTITY_CHARS:
        return value
    return None


def _usage_record(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None

    def read(field: str) -> Any:
        if isinstance(usage, Mapping):
            return usage.get(field)
        return getattr(usage, field, None)

    model = _identity(read("model"))
    if model is None:
        return None
    record: dict[str, Any] = {"model": model}
    for field in _USAGE_INT_FIELDS:
        value = read(field)
        if not _is_count(value):
            return None
        record[field] = value
    return record


def _cost_record(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        status = raw.get("status")
        if (
            status == "reported"
            and set(raw.keys()) == {"status", "amount", "currency", "source"}
            and _is_number(raw.get("amount"))
            and raw["amount"] >= 0
            and _is_str(raw.get("currency"))
            and len(raw["currency"]) == 3
            and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for ch in raw["currency"])
            and _identity(raw.get("source")) is not None
        ):
            return {
                "status": "reported",
                "amount": raw["amount"],
                "currency": raw["currency"],
                "source": raw["source"],
            }
        if (
            status == "unavailable"
            and set(raw.keys()) == {"status", "reason"}
            and _bounded_text(raw.get("reason"), 500)
        ):
            return {"status": "unavailable", "reason": raw["reason"]}
    return {"status": "unavailable", "reason": "cost metadata missing or malformed"}


def completion_provenance(completion: Any) -> dict[str, Any]:
    """Read model identity, usage, and cost from a completion's metadata.

    ``provider_reported`` is granted only to genuine adapter identities
    (``meta``/``meta_cli`` or ``azure``/``azure_foundry``) that say so;
    ``injected`` only to explicit fixtures (``fixture``/``injected``).
    Anything else is ``unverified``. Missing fields stay ``None``; usage
    is never fabricated.
    """
    meta = getattr(completion, "meta", None)
    meta = meta if isinstance(meta, Mapping) else {}
    provider = _identity(meta.get("provider"))
    adapter = _identity(meta.get("adapter"))
    claimed = meta.get("evidence_kind")
    if (
        claimed == EVIDENCE_PROVIDER
        and provider in _PROVIDER_ADAPTERS
        and _PROVIDER_ADAPTERS[provider] == adapter
    ):
        kind = EVIDENCE_PROVIDER
    elif (
        claimed == EVIDENCE_INJECTED and provider == "fixture" and adapter == "injected"
    ):
        kind = EVIDENCE_INJECTED
    else:
        kind = EVIDENCE_UNVERIFIED
    usage = _usage_record(getattr(completion, "usage", None))
    model = _identity(meta.get("model"))
    if model is None and usage is not None and usage["model"] != "unknown":
        model = usage["model"]
    return {
        "model": model,
        "provider": provider,
        "adapter": adapter,
        "evidence_kind": kind,
        "usage": usage,
        "cost": _cost_record(meta.get("cost")),
    }


def _seal(body: Mapping[str, Any]) -> dict[str, Any]:
    receipt = dict(body)
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return receipt


def validate_receipt(receipt: Any) -> dict[str, Any]:
    """Validate one version-1 vision-advisory receipt; return a copy.

    Raises :class:`CriticError` on any deviation, including a
    ``receipt_sha256`` that does not seal the other fields.
    """
    if not isinstance(receipt, Mapping) or set(receipt.keys()) != _RECEIPT_KEYS:
        raise CriticError("receipt key set mismatch")
    r = dict(receipt)
    if type(r["version"]) is not int or r["version"] != RECEIPT_VERSION:
        raise CriticError("receipt version must be 1")
    if r["status"] not in RECEIPT_STATUSES:
        raise CriticError("receipt status unknown")
    if not _is_hex_digest(r["artifact_sha256"]):
        raise CriticError("receipt artifact_sha256 malformed")
    facts = normalize_check_facts(r["deterministic_checks"])
    if facts != r["deterministic_checks"]:
        raise CriticError("receipt deterministic_checks are not normalized facts")
    for key in ("model", "provider", "adapter"):
        if r[key] is not None and _identity(r[key]) is None:
            raise CriticError(f"receipt {key} malformed")
    if r["evidence_kind"] not in EVIDENCE_KINDS:
        raise CriticError("receipt evidence_kind unknown")
    if r["usage"] is not None and _usage_record(r["usage"]) != r["usage"]:
        raise CriticError("receipt usage malformed")
    if _cost_record(r["cost"]) != r["cost"]:
        raise CriticError("receipt cost malformed")
    if not _bounded_text(r["note"], MAX_NOTE_CHARS):
        raise CriticError("receipt note malformed")
    if r["disclaimer"] != TEACHING_REVIEW_DISCLAIMER:
        raise CriticError("receipt disclaimer mismatch")
    known = {fact["check_id"] for fact in facts}
    citations = r["cited_check_ids"]
    observations = r["observations"]
    if r["status"] == STATUS_OK:
        if r["evidence_kind"] == EVIDENCE_NOT_DISPATCHED:
            raise CriticError("ok receipt cannot be not_dispatched")
        if not isinstance(observations, Mapping) or set(observations) != set(
            OBSERVATION_KEYS
        ):
            raise CriticError("ok receipt observations must name the four categories")
        if not all(
            _bounded_text(observations[k], MAX_OBSERVATION_CHARS)
            for k in OBSERVATION_KEYS
        ):
            raise CriticError("ok receipt observation malformed")
        if (
            not isinstance(citations, list)
            or len(citations) > MAX_CITED_CHECKS
            or not all(_is_str(c) and c in known for c in citations)
            or len(set(citations)) != len(citations)
        ):
            raise CriticError("ok receipt cited_check_ids malformed")
        if any(
            r[k] is None or r[k] == "unknown" for k in ("model", "provider", "adapter")
        ):
            raise CriticError(
                "ok receipt requires observed model and provider metadata"
            )
        if (
            r["usage"] is None
            or r["usage"]["calls"] < 1
            or r["usage"]["model"] != r["model"]
        ):
            raise CriticError("ok receipt requires valid reported usage")
    else:
        if observations != {} or citations != []:
            raise CriticError("failure receipt carries no observations or citations")
        if r["usage"] is None:
            if (
                any(r[k] is not None for k in ("model", "provider", "adapter"))
                or r["cost"]["status"] != "unavailable"
            ):
                raise CriticError(
                    "unreported failure usage and identity must remain unknown"
                )
        elif (
            r["status"] != STATUS_FAILED
            or r["evidence_kind"] == EVIDENCE_NOT_DISPATCHED
            or r["usage"]["model"] != r["model"]
            or r["usage"]["calls"] < 1
        ):
            raise CriticError("invalid reported usage on rejected advisory")
    expected = _seal({k: v for k, v in r.items() if k != "receipt_sha256"})
    if expected["receipt_sha256"] != r["receipt_sha256"]:
        raise CriticError("receipt_sha256 does not seal the receipt")
    return r


def build_ok_receipt(
    artifact_sha256: str,
    facts: Sequence[Mapping[str, str]],
    provenance: Mapping[str, Any],
    opinion: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal a successful receipt from validated facts, provenance, opinion."""
    receipt = _seal(
        {
            "version": RECEIPT_VERSION,
            "status": STATUS_OK,
            "artifact_sha256": artifact_sha256,
            "deterministic_checks": [dict(f) for f in facts],
            "model": provenance["model"],
            "provider": provenance["provider"],
            "adapter": provenance["adapter"],
            "evidence_kind": provenance["evidence_kind"],
            "usage": provenance["usage"],
            "cost": dict(provenance["cost"]),
            "observations": dict(opinion["observations"]),
            "note": opinion["note"],
            "cited_check_ids": list(opinion["cited_check_ids"]),
            "disclaimer": TEACHING_REVIEW_DISCLAIMER,
        }
    )
    return validate_receipt(receipt)


def build_failure_receipt(
    artifact_sha256: str,
    deterministic_checks: Any,
    *,
    status: str,
    evidence_kind: str,
    note: str,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Seal an ``unavailable`` / ``advisory-failed`` receipt.

    Usage is ``None`` and cost is unavailable; model/provider/adapter
    are taken from ``provenance`` only when a (rejected) answer actually
    reported them, else ``None`` — nothing is fabricated.
    """
    if status not in (STATUS_UNAVAILABLE, STATUS_FAILED):
        raise CriticError(
            "failure receipt status must be unavailable or advisory-failed"
        )
    if evidence_kind not in EVIDENCE_KINDS:
        raise CriticError("failure receipt evidence_kind unknown")
    prov = provenance or {}
    reported_usage = prov.get("usage")
    receipt = _seal(
        {
            "version": RECEIPT_VERSION,
            "status": status,
            "artifact_sha256": artifact_sha256,
            "deterministic_checks": normalize_check_facts(deterministic_checks),
            "model": prov.get("model"),
            "provider": prov.get("provider"),
            "adapter": prov.get("adapter"),
            "evidence_kind": evidence_kind,
            "usage": reported_usage,
            "cost": _cost_record(prov.get("cost")),
            "observations": {},
            "note": note[:MAX_NOTE_CHARS],
            "cited_check_ids": [],
            "disclaimer": TEACHING_REVIEW_DISCLAIMER,
        }
    )
    return validate_receipt(receipt)


def failure_receipt_for(
    artifact_sha256: str,
    deterministic_checks: Any,
    exc: BaseException,
    *,
    dispatched: bool,
) -> dict[str, Any]:
    """Classify one critique failure into a sealed generic receipt.

    Only the exception class name is recorded, never its message, so
    provider exception bodies cannot leak into packaged evidence.
    Policy refusals and pre-dispatch failures are ``not_dispatched``;
    budget exhaustion is refused before the call by the engine.
    """
    dispatched = getattr(exc, "dispatched", dispatched)
    name = type(exc).__name__
    provenance: Mapping[str, Any] | None = None
    if isinstance(exc, CriticError):
        dispatched = exc.dispatched
        provenance = exc.provenance
    if name == "ModelPolicyError":
        status, kind = STATUS_UNAVAILABLE, EVIDENCE_NOT_DISPATCHED
        note = f"Vision advisory unavailable: model policy refused the critic call ({name})."
    elif name == "BudgetExceeded":
        status, kind = STATUS_FAILED, EVIDENCE_NOT_DISPATCHED
        note = f"Vision advisory failed: model token budget exhausted before the critic call ({name})."
    elif not dispatched:
        status, kind = STATUS_UNAVAILABLE, EVIDENCE_NOT_DISPATCHED
        note = f"Vision advisory unavailable: critic could not be dispatched ({name})."
    elif isinstance(exc, CriticError) and provenance is not None:
        status, kind = (
            STATUS_FAILED,
            provenance.get("evidence_kind", EVIDENCE_UNVERIFIED),
        )
        note = (
            "Vision advisory failed: critic answer rejected by local strict "
            f"validation ({name}: {exc})."
        )
    else:
        status, kind = STATUS_FAILED, EVIDENCE_UNVERIFIED
        provenance = None
        note = f"Vision advisory failed: critic call raised {name}."
    return build_failure_receipt(
        artifact_sha256,
        deterministic_checks,
        status=status,
        evidence_kind=kind,
        note=note,
        provenance=provenance,
    )


def critique_synthetic(
    model_client: Any,
    synthetic_path: str | Path,
    summary: Mapping[str, Any],
    artifact_sha256: str | None = None,
    deterministic_checks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask the vision role for an advisory receipt on a synthetic PNG.

    ``model_client`` is duck-typed: it must offer
    ``complete_with_image(prompt, schema, image_path)``. The image
    SHA-256 is computed from the file before the call; a supplied
    ``artifact_sha256`` that differs is refused. The answer is validated
    strictly (:func:`validate_critic_response`) and sealed into a
    version-1 ``ok`` receipt.

    Raises :class:`CriticError` for local refusals and rejected answers.
    Exceptions raised by the client call itself (including the engine's
    budget refusal) propagate unchanged so the engine keeps their class.
    """
    path = Path(synthetic_path)
    if not path.is_file():
        raise CriticError("synthetic image not found")
    actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if artifact_sha256 is not None and artifact_sha256 != actual_sha:
        raise CriticError("supplied artifact SHA-256 does not match the image bytes")
    if deterministic_checks is None:
        raise CriticError("deterministic check facts are required")
    facts = normalize_check_facts(list(deterministic_checks))
    method = getattr(model_client, "complete_with_image", None)
    if not callable(method):
        raise CriticError("vision client has no complete_with_image method")
    prompt = build_critic_prompt(summary, actual_sha, facts)
    completion = method(prompt, CRITIC_SCHEMA, path)
    provenance = completion_provenance(completion)
    try:
        opinion = validate_critic_response(
            getattr(completion, "data", None), actual_sha, facts
        )
    except CriticError as exc:
        raise CriticError(str(exc), dispatched=True, provenance=provenance) from None
    try:
        return build_ok_receipt(actual_sha, facts, provenance, opinion)
    except CriticError as exc:
        raise CriticError(str(exc), dispatched=True, provenance=provenance) from None
