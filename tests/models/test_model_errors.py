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


class _SneakyStr(str):
    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash("refusal")


def test_str_subclass_reason_code_cannot_pass() -> None:
    assert ModelError("x", reason_code=_SneakyStr(CANARY)).reason_code is None


def test_unhashable_reason_code_does_not_raise() -> None:
    assert ModelError("x", reason_code=["refusal"]).reason_code is None  # type: ignore[arg-type]


def test_canonical_member_is_stored() -> None:
    code = "".join(["ref", "usal"])
    err = ModelError("x", reason_code=code)
    assert err.reason_code in MODEL_ERROR_REASON_CODES
    assert type(err.reason_code) is str


class _TwoFacedDict(dict):
    def get(self, key: Any, default: Any = None) -> Any:
        return 5 if key == "content_chars" else super().get(key, default)


def test_mapping_get_and_getitem_disagreement_cannot_leak() -> None:
    diag = safe_failure_diagnostics(_TwoFacedDict(content_chars=CANARY))
    assert diag == {"content_chars": 5}
    assert CANARY not in str(diag)


def test_str_subclass_values_are_not_echoed() -> None:
    diag = safe_failure_diagnostics(
        {
            "observed_model": _SneakyStr("ok-model"),
            "finish_reason": _SneakyStr("stop"),
            "unsupported_message_fields": [_SneakyStr("annotations")],
        }
    )
    assert diag == {"unsupported_message_fields": ["[unrecognized]"]}


def test_raising_mapping_yields_empty() -> None:
    class Boom(dict):
        def get(self, key: Any, default: Any = None) -> Any:
            raise RuntimeError(CANARY)

    assert safe_failure_diagnostics(Boom()) == {}
