"""Tabular generation tests: model planning against the plan schema."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from models.base import Completion, Usage
from profiles.tabular.analysis.statistics import TabularProfile
from profiles.tabular.generation import plan as plan_module

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


class FakeModel:
    """A StructuredModel double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0
        self.schemas: list[dict] = []

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert set(schema["required"]) == set(schema["properties"])
        assert set(schema["properties"]) <= {"seed", "n_rows", "strategy"}
        self.schemas.append(dict(schema))
        assert "orders-tabular" in prompt
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=5, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


def test_propose_returns_validated_plan() -> None:
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    completion = plan_module.propose(FakeModel([{"seed": 7, "n_rows": 20}]), profile)
    assert completion.data == {"seed": 7, "n_rows": 20}


def test_validate_plan_rejects_bad_rows() -> None:
    with pytest.raises(plan_module.PlanError):
        plan_module.validate_plan({"seed": 7, "n_rows": 0})


STRATEGY = "fitted_gaussian_disclosed"


def test_validate_plan_accepts_supported_strategy() -> None:
    plan = plan_module.validate_plan(
        {"seed": 7, "n_rows": 20, "strategy": STRATEGY},
        allowed_strategies=[STRATEGY],
    )
    assert plan == {"seed": 7, "n_rows": 20, "strategy": STRATEGY}


def test_validate_plan_rejects_unsupported_strategy() -> None:
    with pytest.raises(plan_module.PlanError):
        plan_module.validate_plan(
            {"seed": 7, "n_rows": 20, "strategy": "no-such-strategy"},
            allowed_strategies=[STRATEGY],
        )


def test_validate_plan_omitted_strategy_needs_no_injection() -> None:
    plan = plan_module.validate_plan(
        {"seed": 7, "n_rows": 20}, allowed_strategies=[STRATEGY]
    )
    assert plan == {"seed": 7, "n_rows": 20}


def test_propose_rejects_out_of_spec_strategy() -> None:
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    with pytest.raises(plan_module.PlanError):
        plan_module.propose(
            FakeModel([{"seed": 7, "n_rows": 20, "strategy": "nope"}]),
            profile,
            allowed_strategies=[STRATEGY],
        )


def test_build_prompt_names_supported_strategies() -> None:
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    header = plan_module.build_prompt(profile).split("Profile statistics")[0]
    assert '"strategy"' not in header
    prompted = plan_module.build_prompt(profile, [STRATEGY])
    assert STRATEGY in prompted
    assert profile.to_disclosed_json() in prompted


def test_provider_schema_is_strict_without_bound_strategies() -> None:
    """Unbound wire schema: required covers every declared property."""
    schema = plan_module.provider_schema(None)
    assert set(schema["required"]) == set(schema["properties"])
    assert set(schema["properties"]) == {"seed", "n_rows"}
    assert schema["additionalProperties"] is False


def test_provider_schema_is_strict_with_bound_strategies() -> None:
    """Bound wire schema: strategy required, so the provider accepts it.

    Regression test for the live 400 (``required`` omitted ``strategy``
    while ``properties`` declared it): the sent schema must require
    every property it declares.
    """
    schema = plan_module.provider_schema([STRATEGY])
    assert set(schema["required"]) == set(schema["properties"])
    assert set(schema["properties"]) == {"seed", "n_rows", "strategy"}
    assert schema["additionalProperties"] is False


def test_propose_sends_strict_schema_with_bound_strategies() -> None:
    """The captured sent schema requires every declared property."""
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    model = FakeModel([{"seed": 7, "n_rows": 20, "strategy": STRATEGY}])
    plan_module.propose(model, profile, allowed_strategies=[STRATEGY])
    (sent,) = model.schemas
    assert set(sent["required"]) == set(sent["properties"])
    assert set(sent["properties"]) == {"seed", "n_rows", "strategy"}


def test_build_prompt_requires_strategy_when_bound() -> None:
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    header = plan_module.build_prompt(profile, [STRATEGY]).split("Profile statistics")[
        0
    ]
    assert "exactly three keys" in header
    assert STRATEGY in header


def test_repair_prompt_carries_only_supported_options() -> None:
    profile = TabularProfile.from_rows(COLUMNS, ROWS, "orders-tabular", "abc123")
    failed = [{"name": "mean_value", "status": "fail", "detail": "drift"}]
    options = {"strategies": [STRATEGY], "max_rows": 200000}
    prompt = plan_module.build_repair_prompt(
        profile, failed, {"seed": 1, "n_rows": 20}, options
    )
    assert "mean_value" in prompt
    assert STRATEGY in prompt
    assert "200000" in prompt
    assert profile.to_disclosed_json() in prompt
    header = prompt.split("Profile statistics")[0]
    assert "10.0" not in header
