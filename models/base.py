"""Shared model types: usage, completions, and the structured-call contract.

A :class:`StructuredModel` takes a prompt plus a JSON Schema and returns a
:class:`Completion` whose ``data`` already conforms to that schema. Anything
that stops that contract -- a refusal, a transport failure, or a malformed
answer -- surfaces as :class:`ModelError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelError(Exception):
    """A model call failed: refusal, nonzero exit, or malformed answer."""


#: Observed model identity when the transport supplies no usable name.
#: Adapters record this in ``Usage.model`` and ``Completion.meta["model"]``
#: when the wire reply names no model; the requested identity is never
#: substituted there -- it lives in ``Completion.meta["requested_model"]``
#: and the invocation record instead.
UNKNOWN_MODEL = "unknown"


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

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        """Return the model's structured answer for ``prompt``.

        Raises :class:`ModelError` when the model refuses, the transport
        fails, or the answer does not conform to ``schema``.
        """
        ...
