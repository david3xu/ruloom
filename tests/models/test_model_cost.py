"""Reported monetary cost is bounded provenance, never inferred billing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from models.base import ModelError, monetary_cost
from models.meta_cli import MuseCLIModel


@pytest.mark.parametrize(
    "amount", [10**400, -1, True, float("nan"), float("inf"), "0.1", None]
)
def test_malformed_cost_never_overflows_or_becomes_zero_charge(amount):
    cost = monetary_cost({"amount": amount, "currency": "USD"}, source="cli")
    assert cost["status"] == "unavailable"
    assert "amount" not in cost


@pytest.mark.parametrize(
    "currency", ["secret-canary", "usd", "US$", "USD" * 10000, None, {"canary": True}]
)
def test_cost_currency_is_a_bounded_code(currency):
    cost = monetary_cost({"amount": 0.1, "currency": currency}, source="cli")
    assert cost["status"] == "unavailable"
    assert "canary" not in json.dumps(cost)


def test_cost_source_is_adapter_owned():
    cost = monetary_cost(
        {"amount": 0, "currency": "USD", "source": {"secret-canary": "x" * 10000}},
        source="cli",
    )
    assert cost == {
        "status": "reported",
        "amount": 0.0,
        "currency": "USD",
        "source": "cli",
    }


@pytest.mark.parametrize("name", ["unknown", "", " "])
def test_vision_refuses_reported_unknown_model_identity(name):
    usage = {
        "payload_type": "run.usage",
        "payload": {
            "models": [
                {"model": name, "input_tokens": 1, "output_tokens": 1, "calls": 1}
            ]
        },
    }
    terminal = {
        "payload_type": "run.terminal.completed",
        "payload": {"terminal": "completed", "text": "{}"},
    }
    proc = SimpleNamespace(
        returncode=0, stdout=json.dumps(usage) + "\n" + json.dumps(terminal)
    )
    with pytest.raises(ModelError, match="missing or unknown"):
        MuseCLIModel()._parse_vision(proc, {"type": "object"})


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_image_schema_external_reference_refused_before_dispatch(keyword):
    from models.base import structured_image_schema

    with pytest.raises(ModelError, match="external reference"):
        structured_image_schema({keyword: "https://untrusted.invalid/schema.json"})
