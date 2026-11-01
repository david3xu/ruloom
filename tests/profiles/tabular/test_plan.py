"""Tabular generation tests: model planning against the plan schema."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

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

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert dict(schema) == plan_module.PLAN_SCHEMA
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
