"""ModelError reason codes and whitelisted failure diagnostics (offline)."""

from __future__ import annotations

from typing import Any

import pytest

from models.base import (
    MODEL_ERROR_REASON_CODES,
    ModelError,
    safe_failure_diagnostics,
)

CANARY = "CANARY-SECRET-91b2"


def test_plain_model_error_is_backward_compatible() -> None:
    err = ModelError("fixed message")
    assert str(err) == "fixed message"
    assert err.reason_code is None
    assert err.diagnostics == {}


def test_unknown_reason_code_is_dropped() -> None:
    assert ModelError("x", reason_code=CANARY).reason_code is None
    assert ModelError("x", reason_code="refusal").reason_code == "refusal"
    assert "refusal" in MODEL_ERROR_REASON_CODES


def test_unknown_keys_and_text_values_are_dropped() -> None:
    diag = safe_failure_diagnostics(
        {
            "content": CANARY,
            "reasoning": CANARY,
            "body": CANARY,
            "finish_reason": CANARY,
            "observed_model": CANARY + " with spaces",
            "schema_keyword": CANARY,
            "schema_path": CANARY + "\n<html>",
        }
    )
    assert diag == {
        "finish_reason": "unrecognized",
        "observed_model": "unrecognized",
        "schema_keyword": "other",
    }


@pytest.mark.parametrize(
    "value",
    [True, -1, 1.5, "3", None],
)
def test_counters_require_non_negative_ints(value: Any) -> None:
    diag = safe_failure_diagnostics(
        {
            "content_chars": value,
            "choice_count": value,
            "http_status": value,
            "reported_usage": {"prompt_tokens": value},
        }
    )
    assert diag == {}


def test_field_names_bounded_and_sorted() -> None:
    names = [f"field_{i}" for i in range(20)] + [CANARY]
    diag = safe_failure_diagnostics({"unsupported_message_fields": names})
    assert len(diag["unsupported_message_fields"]) == 8
    assert CANARY not in str(diag)
    assert diag["unsupported_message_fields"][0] == "[unrecognized]"


def test_non_mapping_input_yields_empty() -> None:
    assert safe_failure_diagnostics(CANARY) == {}
    assert safe_failure_diagnostics(None) == {}
