"""A model proposes the generation plan; seeded code does everything else.

The plan is a strict JSON object -- an integer ``seed`` and an integer
``n_rows >= 1`` -- and the only source-derived content the model ever sees
is :meth:`Profile.to_json` statistics. The returned plan is validated
against :data:`PLAN_SCHEMA`; anything else raises :class:`PlanError`.
"""

from __future__ import annotations

from typing import Any, Mapping

import jsonschema

from models.base import Completion, ModelError, StructuredModel

from ..analysis.statistics import Profile


class PlanError(Exception):
    """The proposed plan does not conform to :data:`PLAN_SCHEMA`."""


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seed": {"type": "integer"},
        "n_rows": {"type": "integer", "minimum": 1},
    },
    "required": ["seed", "n_rows"],
    "additionalProperties": False,
}


def validate_plan(data: Any) -> dict[str, Any]:
    """Validate a plan dict against :data:`PLAN_SCHEMA`.

    Returns a plain ``dict`` copy of the conforming plan. Raises
    :class:`PlanError` when the value is not a conforming plan.
    """
    if not isinstance(data, Mapping):
        raise PlanError(f"plan must be an object, got {type(data).__name__}")
    plan = dict(data)
    try:
        jsonschema.validate(plan, PLAN_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise PlanError(f"plan does not conform to PLAN_SCHEMA: {exc.message}") from exc
    for key in ("seed", "n_rows"):
        value = plan[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise PlanError(f"plan {key!r} must be an integer, got {value!r}")
    return plan


def build_prompt(profile: Profile) -> str:
    """Build the planning prompt for a profile.

    The only source-derived content in the prompt is the profile's
    statistics serialisation; no raw source rows are ever included.
    """
    return (
        "Propose a data-generation plan for this HyLogger hole.\n"
        "Reply with a JSON object with exactly two keys:\n"
        '- "seed": any integer (the deterministic seed for the generator).\n'
        '- "n_rows": a positive integer count of rows to generate. '
        "Set it to the profile statistics' n_samples so the synthetic "
        "hole matches the source hole's row count.\n"
        "Profile statistics (the only source-derived content):\n"
        + profile.to_json()
    )


def propose(model: StructuredModel, profile: Profile) -> Completion:
    """Ask ``model`` for a plan and validate it against :data:`PLAN_SCHEMA`.

    Returns the :class:`Completion` whose ``data`` is the validated plan.
    Raises :class:`PlanError` when the answer is not a conforming plan and
    :class:`ModelError` when the model call itself fails.
    """
    if not isinstance(profile, Profile):
        raise TypeError(f"profile must be a Profile, got {type(profile).__name__}")
    completion = model.complete(build_prompt(profile), PLAN_SCHEMA)
    plan = validate_plan(completion.data)
    return Completion(data=plan, usage=completion.usage, meta=completion.meta)
