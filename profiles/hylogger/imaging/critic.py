"""Advisory vision critique over SYNTHETIC images only.

A vision model may look at a rendered synthetic PNG and return a
structured advisory note. The note is logged through the engine's
advisory channel and can never change a pass/fail verdict (the engine
excludes advisories from verdict computation; the ``image_critique``
capability declares ``advisory: true`` and never calls
``record_checks``).

Boundaries, enforced by construction:

* the image path handed to the model is always the synthetic artifact
  written by seeded code — raw source pixels are never attached;
* the prompt carries statistics summaries only, never raw rows or raw
  pixels;
* the model client is duck-typed (it only needs a
  ``complete_with_image`` method), so this module imports no model code
  and the profile/model layer boundary stays intact.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


class CriticError(Exception):
    """The vision critique call failed or returned no usable note."""


#: Schema for the structured vision-critic answer: a free-text note.
CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "note": {
            "type": "string",
            "description": "Advisory observations on the synthetic image.",
        },
    },
    "required": ["note"],
    "additionalProperties": False,
}


def build_critic_prompt(summary: Mapping[str, Any]) -> str:
    """Build the critique prompt from a statistics summary only."""
    import json

    return (
        "You are an advisory critic for synthetic geological core imagery.\n"
        "The attached image is SYNTHETIC: it was rendered by seeded "
        "deterministic code fitted to statistics of a real source image, "
        "not measured data. Your note is advisory only and cannot change "
        "any pass/fail verdict.\n"
        "Comment briefly on the synthetic image's plausibility as a "
        "banded core-strip rendering (strip banding, colour continuity, "
        "visible artefacts). Reply with a JSON object with exactly one "
        'key: "note", a short paragraph.\n'
        "Fitted statistics summary (the only source-derived content):\n"
        + json.dumps(dict(summary), indent=2, sort_keys=True)
    )


def critique_synthetic(
    model_client: Any,
    synthetic_path: str | Path,
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Ask the vision role for an advisory note on a synthetic PNG.

    ``model_client`` is duck-typed: it must offer
    ``complete_with_image(prompt, schema, image_path)`` (see
    :class:`models.meta_cli.MuseCLIModel`). Returns the note mapping
    ``{"note": ..., "model": ...}``. Raises :class:`CriticError` when
    the call fails or the answer carries no usable note.
    """
    path = Path(synthetic_path)
    if not path.is_file():
        raise CriticError(f"synthetic image not found: {path}")
    prompt = build_critic_prompt(summary)
    try:
        completion = model_client.complete_with_image(prompt, CRITIC_SCHEMA, path)
    except AttributeError as exc:
        raise CriticError("vision client has no complete_with_image method") from exc
    except Exception as exc:  # noqa: BLE001 -- wrapped, then recorded
        raise CriticError(
            f"vision critique failed: {type(exc).__name__}: {exc}"
        ) from exc
    data = getattr(completion, "data", None)
    if not isinstance(data, Mapping) or not str(data.get("note", "")).strip():
        raise CriticError("vision critique returned no usable note")
    meta = getattr(completion, "meta", {})
    return {
        "note": str(data["note"]).strip(),
        "model": meta.get("model") if isinstance(meta, Mapping) else None,
    }
