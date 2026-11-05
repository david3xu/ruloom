"""Shared model types: usage, completions, and the structured-call contract.

A :class:`StructuredModel` takes a prompt plus a JSON Schema and returns a
:class:`Completion` whose ``data`` already conforms to that schema. Anything
that stops that contract -- a refusal, a transport failure, or a malformed
answer -- surfaces as :class:`ModelError`.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import jsonschema

#: Closed vocabulary of failure reasons an adapter may attach to a
#: :class:`ModelError`. A code names which local check refused the call;
#: it never carries provider text. Unknown codes are dropped to ``None``.
MODEL_ERROR_REASON_CODES: frozenset[str] = frozenset(
    {
        "http_error",
        "network_error",
        "response_too_large",
        "invalid_response_json",
        "response_not_object",
        "missing_model_identity",
        "unknown_model_identity",
        "model_identity_mismatch",
        "choice_count",
        "choice_not_object",
        "finish_reason_not_stop",
        "message_not_object",
        "unsupported_message_fields",
        "refusal",
        "function_call",
        "tool_calls",
        "content_not_string",
        "content_not_json",
        "content_not_object",
        "schema_nonconformance",
        "schema_unresolvable",
        "usage_missing",
        "usage_invalid",
    }
)

#: Finish reasons recorded verbatim; any other provider value is recorded
#: as ``"unrecognized"`` so free text never reaches the run log.
KNOWN_FINISH_REASONS: frozenset[str] = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "function_call"}
)

#: JSON Schema keywords recorded verbatim for a local validation failure.
KNOWN_SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "required",
        "additionalProperties",
        "properties",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "items",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minProperties",
        "maxProperties",
        "propertyNames",
        "contains",
        "anyOf",
        "allOf",
        "oneOf",
        "not",
        "format",
        "dependentRequired",
    }
)

_SAFE_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_SAFE_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SAFE_SCHEMA_PATH_RE = re.compile(r"^[A-Za-z0-9_\[\]/.-]{1,200}$")
_USAGE_COUNTER_KEYS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)
_MAX_REPORTED_FIELDS = 8


def _non_negative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def safe_failure_diagnostics(raw: Any) -> dict[str, Any]:
    """Reduce adapter failure facts to a whitelisted, provider-text-free mapping.

    Only these keys survive, each with a type and charset check:
    ``finish_reason`` (closed vocabulary, else ``"unrecognized"``),
    ``observed_model`` (identifier charset, else ``"unrecognized"``),
    ``model_matches_request`` (bool), ``unsupported_message_fields``
    (identifier names, at most eight), ``schema_path`` (already
    redacted by the adapter), ``schema_keyword`` (closed vocabulary, else
    ``"other"``), ``content_chars`` / ``choice_count`` / ``http_status``
    (non-negative integers) and ``reported_usage`` (non-negative integer
    token counters as the provider reported them, not validated against
    each other). Anything else, including message content, reasoning text
    and response bodies, is dropped.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    finish = raw.get("finish_reason")
    if isinstance(finish, str):
        out["finish_reason"] = (
            finish if finish in KNOWN_FINISH_REASONS else "unrecognized"
        )
    observed = raw.get("observed_model")
    if isinstance(observed, str):
        stripped = observed.strip()
        out["observed_model"] = (
            stripped if _SAFE_MODEL_ID_RE.fullmatch(stripped) else "unrecognized"
        )
    matches = raw.get("model_matches_request")
    if type(matches) is bool:
        out["model_matches_request"] = matches
    fields = raw.get("unsupported_message_fields")
    if isinstance(fields, (list, tuple, set, frozenset)):
        names = sorted(
            {
                name
                if isinstance(name, str) and _SAFE_FIELD_NAME_RE.fullmatch(name)
                # Provider-chosen key names outside the identifier charset
                # are collapsed rather than echoed.
                else "[unrecognized]"
                for name in fields
            }
        )
        out["unsupported_message_fields"] = names[:_MAX_REPORTED_FIELDS]
    path = raw.get("schema_path")
    if isinstance(path, str) and _SAFE_SCHEMA_PATH_RE.fullmatch(path):
        out["schema_path"] = path
    keyword = raw.get("schema_keyword")
    if isinstance(keyword, str):
        out["schema_keyword"] = keyword if keyword in KNOWN_SCHEMA_KEYWORDS else "other"
    for key in ("content_chars", "choice_count"):
        if _non_negative_int(raw.get(key)):
            out[key] = raw[key]
    status = raw.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        out["http_status"] = status
    usage = raw.get("reported_usage")
    if isinstance(usage, Mapping):
        counters = {
            key: usage[key]
            for key in _USAGE_COUNTER_KEYS
            if _non_negative_int(usage.get(key))
        }
        if counters:
            out["reported_usage"] = counters
    return out


class ModelError(Exception):
    """A model call failed: refusal, nonzero exit, or malformed answer.

    Adapters may attach ``reason_code`` (one of
    :data:`MODEL_ERROR_REASON_CODES`) and ``diagnostics`` (reduced through
    :func:`safe_failure_diagnostics`). Both are safe to log: neither can
    carry provider message content, reasoning text or credentials. The
    exception message stays a fixed adapter-authored string.
    """

    def __init__(
        self,
        *args: Any,
        reason_code: str | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        self.reason_code: str | None = (
            reason_code if reason_code in MODEL_ERROR_REASON_CODES else None
        )
        self.diagnostics: dict[str, Any] = safe_failure_diagnostics(diagnostics)


def structured_image_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a JSON schema without allowing external reference retrieval."""
    if not isinstance(schema, Mapping):
        raise ModelError("image output schema must be an object")
    result = dict(schema)
    try:
        json.dumps(result, allow_nan=False)
        pending: list[Any] = [result]
        while pending:
            item = pending.pop()
            if isinstance(item, Mapping):
                for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                    ref = item.get(keyword)
                    if ref is not None and (
                        not isinstance(ref, str) or not ref.startswith("#")
                    ):
                        raise ValueError("external reference")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        validator = jsonschema.validators.validator_for(result)
        validator.check_schema(result)
    except (ValueError, TypeError, RecursionError, jsonschema.SchemaError):
        raise ModelError("invalid vision output schema or external reference") from None
    return result


def image_wire_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Project the image schema to the provider dialect; validate the full original locally.

    Meta rejects ``uniqueItems`` during structured-output admission. Uniqueness
    remains enforced by local validation. Visit schema nodes only, preserving
    data property names and annotation values.
    """
    result = copy.deepcopy(dict(schema))
    pending = [result]
    maps = (
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
    )
    singles = (
        "items",
        "additionalProperties",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
    )
    lists = ("anyOf", "allOf", "oneOf", "prefixItems")
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        node.pop("uniqueItems", None)
        for key in maps:
            children = node.get(key)
            if isinstance(children, dict):
                pending.extend(children.values())
        for key in singles:
            child = node.get(key)
            if isinstance(child, dict):
                pending.append(child)
            elif key == "items" and isinstance(child, list):
                pending.extend(child)
        for key in lists:
            children = node.get(key)
            if isinstance(children, list):
                pending.extend(children)
    return result


#: Observed model identity when the transport supplies no usable name.
#: Adapters record this in ``Usage.model`` and ``Completion.meta["model"]``
#: when the wire reply names no model; the requested identity is never
#: substituted there -- it lives in ``Completion.meta["requested_model"]``
#: and the invocation record instead.
UNKNOWN_MODEL = "unknown"


def monetary_cost(value: Any, *, source: str) -> dict[str, Any]:
    """Normalize a reported charge; never infer currency, price, or zero cost.

    The adapter supplies the provenance label. Provider-controlled source
    strings are not copied into evidence.
    """
    unavailable = {
        "status": "unavailable",
        "reason": "transport reports no valid monetary cost",
    }
    if not isinstance(value, Mapping):
        return unavailable
    amount = value.get("amount", value.get("value"))
    currency = value.get("currency")
    if type(amount) not in (int, float) or not isinstance(currency, str):
        return unavailable
    if len(currency) != 3 or any(
        ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for ch in currency
    ):
        return unavailable
    try:
        parsed = float(amount)
    except (OverflowError, ValueError):
        return unavailable
    if not math.isfinite(parsed) or parsed < 0:
        return unavailable
    return {
        "status": "reported",
        "amount": parsed,
        "currency": currency,
        "source": source,
    }


#: Input modalities recognized across supported models.
SUPPORTED_INPUT_MODALITIES = frozenset({"text", "image"})

#: Admitted image media types across supported models.
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset({"image/png"})


@dataclass(frozen=True)
class ModelCapabilities:
    """Declared capabilities and boundaries of a model adapter.

    Defaults are strictly text-only; vision must be declared explicitly
    with positive byte and pixel bounds, never inferred from a model name.
    """

    input_modalities: tuple[str, ...] = ("text",)
    structured_output: bool = True
    image_media_types: tuple[str, ...] = ()
    max_image_bytes: int = 0
    max_image_pixels: int = 0

    def __post_init__(self) -> None:
        if type(self.structured_output) is not bool:
            raise ValueError(
                f"structured_output must be an exact bool, got {type(self.structured_output).__name__}"
            )

        # Modalities validation
        if isinstance(self.input_modalities, list):
            object.__setattr__(self, "input_modalities", tuple(self.input_modalities))
        if not isinstance(self.input_modalities, tuple) or not self.input_modalities:
            raise ValueError("input_modalities must be a non-empty tuple of strings")
        for m in self.input_modalities:
            if not isinstance(m, str) or m not in SUPPORTED_INPUT_MODALITIES:
                raise ValueError(
                    f"unsupported modality {m!r}; supported modalities: {sorted(SUPPORTED_INPUT_MODALITIES)}"
                )

        # Image media types validation
        if isinstance(self.image_media_types, list):
            object.__setattr__(self, "image_media_types", tuple(self.image_media_types))
        if not isinstance(self.image_media_types, tuple):
            raise ValueError("image_media_types must be a tuple of strings")
        for t in self.image_media_types:
            if not isinstance(t, str) or t not in SUPPORTED_IMAGE_MEDIA_TYPES:
                raise ValueError(
                    f"unsupported image media type {t!r}; supported types: {sorted(SUPPORTED_IMAGE_MEDIA_TYPES)}"
                )

        # Integer bounds validation
        if (
            type(self.max_image_bytes) is not int
            or isinstance(self.max_image_bytes, bool)
            or self.max_image_bytes < 0
        ):
            raise ValueError(
                f"max_image_bytes must be a non-negative integer, got {self.max_image_bytes!r}"
            )
        if (
            type(self.max_image_pixels) is not int
            or isinstance(self.max_image_pixels, bool)
            or self.max_image_pixels < 0
        ):
            raise ValueError(
                f"max_image_pixels must be a non-negative integer, got {self.max_image_pixels!r}"
            )

        # Modal consistency
        if "image" in self.input_modalities:
            if not self.structured_output:
                raise ValueError("image modality requires structured_output True")
            if not self.image_media_types:
                raise ValueError(
                    "image modality requires at least one admitted image_media_type"
                )
            if self.max_image_bytes <= 0:
                raise ValueError("image modality requires positive max_image_bytes")
            if self.max_image_pixels <= 0:
                raise ValueError("image modality requires positive max_image_pixels")
        else:
            if self.image_media_types:
                raise ValueError(
                    "image_media_types must be empty when 'image' modality is absent"
                )
            if self.max_image_bytes != 0:
                raise ValueError(
                    "max_image_bytes must be 0 when 'image' modality is absent"
                )
            if self.max_image_pixels != 0:
                raise ValueError(
                    "max_image_pixels must be 0 when 'image' modality is absent"
                )

    @property
    def supports_image(self) -> bool:
        """True when this adapter declares explicit bounded image input."""
        return (
            self.structured_output is True
            and "image" in self.input_modalities
            and bool(self.image_media_types)
            and self.max_image_bytes > 0
            and self.max_image_pixels > 0
        )


@dataclass(frozen=True)
class Usage:
    """Token usage aggregated across the per-model entries of a run.

    ``model`` is the *observed* identity the transport reported for the
    call (``UNKNOWN_MODEL`` when it reported none, a ``multiple:...``
    aggregate when it reported several). It never falls back to the
    requested name; the full per-model breakdown rides in
    ``Completion.meta["models"]`` and the requested name in
    ``Completion.meta["requested_model"]``.
    """

    model: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls: int = 0


@dataclass(frozen=True)
class Completion:
    """A structured answer plus its usage and provenance."""

    data: dict[str, Any]
    usage: Usage
    meta: dict[str, Any] = field(default_factory=dict)


class StructuredModel(Protocol):
    """A model that answers with JSON conforming to a given schema."""

    capabilities: ModelCapabilities

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        """Return the model's structured answer for ``prompt``.

        Raises :class:`ModelError` when the model refuses, the transport
        fails, or the answer does not conform to ``schema``.
        """
        ...


class VisionStructuredModel(Protocol):
    """A structured model that also accepts an image alongside the prompt."""

    capabilities: ModelCapabilities

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        """Return the model's structured answer for ``prompt``."""
        ...

    def complete_with_image(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        image_path: str | Path,
    ) -> Completion:
        """Return the model's structured answer for ``prompt`` and ``image_path``."""
        ...
