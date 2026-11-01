"""A model proposes the generation plan; seeded code does everything else.

The plan is a strict JSON object -- an integer ``seed`` and an integer
``n_rows >= 1`` -- and the only source-derived content the model ever sees
is :meth:`TabularProfile.to_json` statistics. The returned plan is validated
against :data:`PLAN_SCHEMA`; anything else raises :class:`PlanError`.
"""

from __future__ import annotations

from typing import Any, Mapping

import jsonschema

from ..analysis.statistics import TabularProfile


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


def build_prompt(profile: TabularProfile) -> str:
    """Build the planning prompt for a profile.

    The only source-derived content in the prompt is the profile's
    statistics serialisation; no raw source rows are ever included.
    """
    return (
        "Propose a data-generation plan for this tabular dataset.\n"
        "Reply with a JSON object with exactly two keys:\n"
        '- "seed": any integer (the deterministic seed for the generator).\n'
        '- "n_rows": a positive integer count of rows to generate. '
        "Set it to the profile statistics' n_rows so the synthetic "
        "table matches the source table's row count.\n"
        "Profile statistics (the only source-derived content):\n"
        + profile.to_json()
    )


def propose(model: Any, profile: TabularProfile) -> Any:
    """Ask ``model`` for a plan and validate it against :data:`PLAN_SCHEMA`.

    Returns the completion whose ``data`` is the validated plan. Raises
    :class:`PlanError` when the answer is not a conforming plan and
    :class:`ValueError` when the profile is not a tabular profile.
    """
    if not isinstance(profile, TabularProfile):
        raise PlanError(
            f"profile must be a TabularProfile, got {type(profile).__name__}"
        )
    completion = model.complete(build_prompt(profile), PLAN_SCHEMA)
    completion_data = validate_plan(completion.data)
    return completion.__class__(
        data=completion_data, usage=completion.usage, meta=completion.meta
    )


def build_repair_prompt(
    profile: TabularProfile,
    failed: Any,
    current_plan: Mapping[str, Any],
) -> str:
    """Build the repair prompt for failed checks.

    Only failed-check JSON, the current plan, and profile statistics are
    included; no raw source rows are ever embedded.
    """
    import json as _json

    failed_payload = [
        {"name": item["name"], "status": item["status"], "detail": item["detail"]}
        for item in failed
    ]
    return (
        "The previous generation plan failed checks. Propose a revised plan.\n"
        "Reply with a JSON object with exactly two keys:\n"
        '- "seed": any integer (the deterministic seed for the generator).\n'
        '- "n_rows": a positive integer count of rows to generate.\n'
        "Failed checks (JSON):\n"
        + _json.dumps(failed_payload, sort_keys=True, indent=2)
        + "\nCurrent plan (JSON):\n"
        + _json.dumps(dict(current_plan), sort_keys=True)
        + "\nProfile statistics (the only source-derived content):\n"
        + profile.to_json()
    )
