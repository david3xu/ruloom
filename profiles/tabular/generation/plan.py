"""A model proposes the generation plan; seeded code does everything else.

The plan is a strict JSON object -- an integer ``seed``, an integer
``n_rows >= 1``, and an optional ``strategy`` naming the generation
strategy -- and the only source-derived content the model ever sees
is the disclosed statistics view
(:meth:`TabularProfile.to_disclosed_json`), which withholds rare text
literals. The returned plan is validated against :data:`PLAN_SCHEMA`;
anything else raises :class:`PlanError`. A plan naming a strategy
outside the spec-supported set is refused before any generation runs.
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
        "strategy": {"type": "string", "minLength": 1},
    },
    "required": ["seed", "n_rows"],
    "additionalProperties": False,
}


def provider_schema(allowed_strategies: Any = None) -> dict[str, Any]:
    """The strict output schema actually sent to the model adapter.

    The provider rejects non-strict schemas with a 400 (``required``
    must list every key in ``properties``), while :data:`PLAN_SCHEMA`
    keeps ``strategy`` optional so a plan omitting it still validates
    and runs under the run spec's own strategy. The wire schema sent
    here therefore always requires every property it declares: with
    bound strategies it carries ``seed``/``n_rows``/``strategy`` (all
    required, so the planner must name one of the supported
    strategies); without bound strategies it carries ``seed``/``n_rows``
    only (both required). Validation stays on :data:`PLAN_SCHEMA`, so
    both wire shapes validate.
    """
    if allowed_strategies:
        return {
            "type": "object",
            "properties": {
                "seed": {"type": "integer"},
                "n_rows": {"type": "integer", "minimum": 1},
                "strategy": {"type": "string", "minLength": 1},
            },
            "required": ["seed", "n_rows", "strategy"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "seed": {"type": "integer"},
            "n_rows": {"type": "integer", "minimum": 1},
        },
        "required": ["seed", "n_rows"],
        "additionalProperties": False,
    }


def validate_plan(
    data: Any,
    max_rows: int | None = None,
    allowed_strategies: Any = None,
) -> dict[str, Any]:
    """Validate a plan dict against :data:`PLAN_SCHEMA`.

    Returns a plain ``dict`` copy of the conforming plan. Raises
    :class:`PlanError` when the value is not a conforming plan. When
    the profile declares a row maximum (the manifest ``max_rows``
    limit), ``max_rows`` carries it and a larger ``n_rows`` is refused
    here, before any generation or check runs. When
    ``allowed_strategies`` is given, a plan naming a strategy outside
    that set is refused here too: the planner may only propose values
    inside the spec-supported bounded schema. A plan omitting
    ``strategy`` runs under the run spec's own strategy (no injection).
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
    strategy = plan.get("strategy")
    if strategy is not None:
        if not isinstance(strategy, str) or not strategy:
            raise PlanError(
                f"plan 'strategy' must be a non-empty string, got {strategy!r}"
            )
        if allowed_strategies is not None and strategy not in list(
            allowed_strategies
        ):
            raise PlanError(
                f"plan strategy {strategy!r} is not one of the supported "
                f"strategies {list(allowed_strategies)!r}: "
                "refused before generation"
            )
    if max_rows is not None and plan["n_rows"] > max_rows:
        raise PlanError(
            f"plan n_rows {plan['n_rows']} exceeds maximum {max_rows}: "
            "refused before generation"
        )
    return plan


def build_prompt(
    profile: TabularProfile, allowed_strategies: Any = None
) -> str:
    """Build the planning prompt for a profile.

    The only source-derived content in the prompt is the profile's
    disclosed statistics serialisation (rare text literals withheld); no
    raw source rows are ever included. When ``allowed_strategies`` is
    given, the prompt requires the ``strategy`` key (one of the
    supported strategies, matching the strict wire schema); otherwise
    the prompt asks for the seed/row-count pair only.
    """
    prompt = (
        "Propose a data-generation plan for this tabular dataset.\n"
        "Reply with a JSON object with exactly two keys:\n"
        '- "seed": any integer (the deterministic seed for the generator).\n'
        '- "n_rows": a positive integer count of rows to generate. '
        "Set it to the profile statistics' n_rows so the synthetic "
        "table matches the source table's row count.\n"
    )
    if allowed_strategies:
        prompt = (
            "Propose a data-generation plan for this tabular dataset.\n"
            "Reply with a JSON object with exactly three keys:\n"
            '- "seed": any integer (the deterministic seed for the generator).\n'
            '- "n_rows": a positive integer count of rows to generate. '
            "Set it to the profile statistics' n_rows so the synthetic "
            "table matches the source table's row count.\n"
            f'"strategy": must be one of {list(allowed_strategies)!r} '
            "naming the generation strategy. No other keys are accepted.\n"
        )
    return (
        prompt
        + "Profile statistics (the only source-derived content):\n"
        + profile.to_disclosed_json()
    )


def propose(
    model: Any,
    profile: TabularProfile,
    max_rows: int | None = None,
    allowed_strategies: Any = None,
) -> Any:
    """Ask ``model`` for a plan and validate it against :data:`PLAN_SCHEMA`.

    Returns the completion whose ``data`` is the validated plan. Raises
    :class:`PlanError` when the answer is not a conforming plan and
    :class:`ValueError` when the profile is not a tabular profile.
    ``max_rows`` carries the profile-declared row maximum when one is
    declared, and ``allowed_strategies`` carries the spec-supported
    strategy set the proposal must stay inside. The schema sent to the
    model is the strict :func:`provider_schema` variant (required
    covers every declared property, as the provider demands);
    validation stays on :data:`PLAN_SCHEMA`.
    """
    if not isinstance(profile, TabularProfile):
        raise PlanError(
            f"profile must be a TabularProfile, got {type(profile).__name__}"
        )
    completion = model.complete(
        build_prompt(profile, allowed_strategies),
        provider_schema(allowed_strategies),
    )
    completion_data = validate_plan(
        completion.data,
        max_rows=max_rows,
        allowed_strategies=allowed_strategies,
    )
    return completion.__class__(
        data=completion_data, usage=completion.usage, meta=completion.meta
    )


def build_repair_prompt(
    profile: TabularProfile,
    failed: Any,
    current_plan: Mapping[str, Any],
    supported_options: Any = None,
) -> str:
    """Build the repair prompt for failed checks.

    Only failed-check JSON, the current plan, the supported corrective
    options, and the disclosed profile statistics are included; no raw
    source rows are ever embedded. The revised plan must stay inside
    the supported options: the prompt carries the allowed strategies
    and bounds, never free-text reinvention.
    """
    import json as _json

    failed_payload = [
        {"name": item["name"], "status": item["status"], "detail": item["detail"]}
        for item in failed
    ]
    prompt = (
        "The previous generation plan failed checks. Propose a revised plan.\n"
        "Reply with a JSON object with exactly two keys:\n"
        '- "seed": any integer (the deterministic seed for the generator).\n'
        '- "n_rows": a positive integer count of rows to generate.\n'
    )
    if supported_options is not None and supported_options.get("strategies"):
        prompt = (
            "The previous generation plan failed checks. Propose a revised plan.\n"
            "Reply with a JSON object with exactly three keys:\n"
            '- "seed": any integer (the deterministic seed for the generator).\n'
            '- "n_rows": a positive integer count of rows to generate.\n'
            '"strategy": must be one of '
            f"{list(supported_options['strategies'])!r} naming the "
            "generation strategy. No other keys are accepted.\n"
        )
    prompt += (
        "Failed checks (JSON):\n"
        + _json.dumps(failed_payload, sort_keys=True, indent=2)
        + "\nCurrent plan (JSON):\n"
        + _json.dumps(dict(current_plan), sort_keys=True)
    )
    if supported_options is not None:
        prompt += (
            "\nSupported corrective options (choose only within these):\n"
            + _json.dumps(dict(supported_options), sort_keys=True, indent=2)
        )
    return (
        prompt
        + "\nProfile statistics (the only source-derived content):\n"
        + profile.to_disclosed_json()
    )
