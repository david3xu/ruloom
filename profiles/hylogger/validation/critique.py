"""Advisory text critique of a run's validation evidence.

A local text model reads the run's validation report plus the profile
statistics summary -- never raw source rows, never images -- and returns
a structured advisory note. The note travels the engine's advisory
channel and can never change a pass/fail verdict: the ``local_critique``
capability declares ``advisory: true`` and never calls
``record_checks``.

Boundaries, enforced by construction:

* the prompt carries the serialized validation report and the profile
  statistics JSON only -- no raw rows, no pixels, no depths;
* the model client is duck-typed (it only needs a ``complete(prompt,
  schema)`` method), so this module imports no model code and the
  profile/model layer boundary stays intact;
* transport failures from the client propagate unchanged, so a caller
  that requires the critique (``--local-critic``) fails loudly instead
  of silently skipping it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class CriticError(Exception):
    """The text critique call returned no usable advisory note."""


#: Schema for the structured text-critique answer.
CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "critique": {
            "type": "string",
            "description": "Advisory observations on the validation evidence.",
        },
        "concerns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Follow-up concerns, if any (may be empty).",
        },
    },
    "required": ["critique", "concerns"],
    "additionalProperties": False,
}


def build_critique_prompt(report: Mapping[str, Any], profile_json: str) -> str:
    """Build the critique prompt from validation evidence only.

    ``report`` is the serialized validation report
    (:meth:`CheckReport.to_dict`); ``profile_json`` is the disclosed
    profile statistics JSON (:meth:`Profile.to_disclosed_json`). Neither
    carries raw rows or rare literals.
    """
    return (
        "You are an advisory critic for a synthetic-data validation run.\n"
        "Your note is advisory only and cannot change any pass/fail "
        "verdict.\n"
        "Read the validation report and the fitted source statistics "
        "summary below, then comment briefly on whether the report reads "
        "as a trustworthy validation of the synthetic artifact: do the "
        "checks cover the important failure modes, is anything skipped "
        "that should not be, and what should a human reviewer double-check "
        "before accepting? Reply with a JSON object with exactly two keys: "
        '"critique", a short paragraph, and "concerns", a list of short '
        "strings (possibly empty).\n"
        "Validation report (the only run-derived content besides the "
        "statistics summary):\n"
        + json.dumps(dict(report), indent=2, sort_keys=True)
        + "\nFitted source statistics summary (aggregates only, no raw rows):\n"
        + profile_json
    )


def critique_report(
    model_client: Any,
    report: Mapping[str, Any],
    profile_json: str,
) -> dict[str, Any]:
    """Ask the local-critic role for an advisory note on run evidence.

    ``model_client`` is duck-typed: it must offer ``complete(prompt,
    schema)`` (see :class:`models.base.StructuredModel`). Returns the
    note mapping ``{"critique": ..., "concerns": [...], "model": ...}``.
    Client transport errors propagate unchanged so a required critique
    fails loudly. Raises :class:`CriticError` when the answer carries no
    usable note.
    """
    prompt = build_critique_prompt(report, profile_json)
    completion = model_client.complete(prompt, CRITIQUE_SCHEMA)
    data = getattr(completion, "data", None)
    if not isinstance(data, Mapping):
        raise CriticError("text critique returned a non-mapping answer")
    critique = data.get("critique")
    concerns = data.get("concerns")
    if not isinstance(critique, str) or not critique.strip():
        raise CriticError("text critique returned no usable critique")
    if not isinstance(concerns, list) or not all(
        isinstance(item, str) for item in concerns
    ):
        raise CriticError("text critique returned no usable concerns list")
    meta = getattr(completion, "meta", {})
    return {
        "critique": critique.strip(),
        "concerns": [item for item in concerns],
        "model": meta.get("model") if isinstance(meta, Mapping) else None,
    }
