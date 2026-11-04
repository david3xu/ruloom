"""Tests for Azure AI Foundry transport (Meta Muse Spark 1.3).

All calls here are offline fixtures with mock HTTP and token provider.
Zero live models, billing or network calls.
Uses invented identifiers only (no real Azure subscription UUIDs or accounts).
"""

from __future__ import annotations

import http.client
import io
import json
import subprocess
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from models.azure_foundry import (
    AzureFoundryModel,
    _validate_bearer_token,
    acquire_entra_token,
    validate_azure_base_url,
    validate_subscription_id,
)
from models.base import ModelError
from models.selection import model_family, validate_selection

VALID_SUB = "11111111-2222-3333-4444-555555555555"
VALID_URL = "https://audit-fixture.openai.azure.com/openai/v1/"
TEST_SCHEMA = {
    "type": "object",
    "properties": {"seed": {"type": "integer"}, "n_rows": {"type": "integer"}},
    "required": ["seed", "n_rows"],
    "additionalProperties": False,
}


class MockHTTPResponse:
    def __init__(
        self,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._io = io.BytesIO(body)
        self.headers = headers or {"x-request-id": "req-12345"}

    def read(self, amt: int = -1) -> bytes:
        return self._io.read(amt)

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _make_azure_response(
    *,
    model: str = "muse-spark-1.3",
    finish_reason: str = "stop",
    content: str = '{"seed": 42, "n_rows": 100}',
    prompt_tokens: int | Any = 227,
    completion_tokens: int | Any = 1054,
    cached_tokens: int | Any = 0,
    reasoning_tokens: int | Any = 977,
    total_tokens: int | Any | None = 1281,
    tool_calls: list[Any] | None = None,
    function_call: dict[str, Any] | None = None,
    refusal: str | None = None,
    extra_choices: list[Any] | None = None,
    extra_msg_fields: dict[str, Any] | None = None,
    rid: Any = "chatcmpl-test-receipt-uuid",
    cost: dict[str, Any] | None = None,
) -> bytes:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    if function_call is not None:
        msg["function_call"] = function_call
    if refusal is not None:
        msg["refusal"] = refusal
    if extra_msg_fields:
        msg.update(extra_msg_fields)
    # Encrypted reasoning content blob to test that it is safely dropped
    msg["reasoning_content"] = "encrypted-opaque-reasoning-blob"

    choice: dict[str, Any] = {
        "index": 0,
        "finish_reason": finish_reason,
        "message": msg,
    }
    choices = [choice]
    if extra_choices:
        choices.extend(extra_choices)

    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cached_tokens},
        "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens

    payload = {
        "id": rid,
        "object": "chat.completion",
        "created": 1730744988,
        "model": model,
        "choices": choices,
        "usage": usage,
    }
    if cost is not None:
        payload["cost"] = cost
    return json.dumps(payload).encode("utf-8")


# --- URL Validation ---


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://audit-fixture.openai.azure.com/openai/v1/",
            "https://audit-fixture.openai.azure.com/openai/v1/",
        ),
        (
            "https://audit-account.openai.azure.com/openai/v1",
            "https://audit-account.openai.azure.com/openai/v1/",
        ),
        (
            "  https://my-resource-01.openai.azure.com/openai/v1/  ",
            "https://my-resource-01.openai.azure.com/openai/v1/",
        ),
    ],
)
def test_validate_azure_base_url_valid(url: str, expected: str) -> None:
    assert validate_azure_base_url(url) == expected


@pytest.mark.parametrize(
    "url,reason",
    [
        ("", "empty string"),
        ("http://account.openai.azure.com/openai/v1/", "non-HTTPS"),
        ("https://account.openai.azure.com:8443/openai/v1/", "custom port"),
        ("https://account.openai.azure.com:443/openai/v1/", "explicit port"),
        ("https://account.openai.azure.com/openai/v1/?api-version=1", "query params"),
        ("https://account.openai.azure.com/openai/v1/#frag", "fragment"),
        ("https://user:pass@account.openai.azure.com/openai/v1/", "credentials"),
        ("https://evil.com/openai/v1/", "arbitrary host"),
        ("https://account.services.ai/models", "unverified services.ai route"),
        ("https://.openai.azure.com/openai/v1/", "empty account label"),
        ("https://-invalid.openai.azure.com/openai/v1/", "invalid account label"),
        ("https://account.openai.azure.com/v1/", "wrong path"),
        ("https://account.openai.azure.com/", "root path"),
    ],
)
def test_validate_azure_base_url_invalid(url: str, reason: str) -> None:
    with pytest.raises(ValueError):
        validate_azure_base_url(url)


# --- Subscription Validation ---


def test_validate_subscription_id() -> None:
    assert (
        validate_subscription_id("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
        == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    with pytest.raises(ValueError, match="canonical UUID"):
        validate_subscription_id("not-a-uuid")
    with pytest.raises(ValueError, match="canonical UUID"):
        validate_subscription_id("")
    with pytest.raises(ValueError, match="canonical UUID"):
        validate_subscription_id("aaaaaaaabbbbccccddddeeeeeeeeeeee")


# --- Timeout Validation ---


@pytest.mark.parametrize(
    "bad_timeout",
    [float("nan"), float("inf"), -1.0, 0.0, True, 301.0, "10"],
)
def test_azure_foundry_timeout_bounds_rejected(bad_timeout: Any) -> None:
    with pytest.raises(ValueError, match="timeout must be a finite positive number"):
        AzureFoundryModel(VALID_URL, VALID_SUB, timeout=bad_timeout)
    with pytest.raises(ValueError, match="timeout must be a finite positive number"):
        acquire_entra_token(VALID_SUB, timeout=bad_timeout)


# --- Entra Token Acquisition ---


def test_acquire_entra_token_success() -> None:
    fake_proc = subprocess.CompletedProcess(
        args=["az"],
        returncode=0,
        stdout=json.dumps({"accessToken": "secret-entra-token-abc"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        tok = acquire_entra_token(VALID_SUB)
        assert tok == "secret-entra-token-abc"
        assert mock_run.call_count == 1
        argv = mock_run.call_args[0][0]
        assert "--subscription" in argv
        assert VALID_SUB in argv
        assert "--resource" in argv
        assert "https://cognitiveservices.azure.com" in argv


def test_acquire_entra_token_null_or_invalid_shape() -> None:
    for bad_stdout in ["null", "[]", '"string"', "123"]:
        fake_proc = subprocess.CompletedProcess(
            args=["az"],
            returncode=0,
            stdout=bad_stdout,
            stderr="",
        )
        with patch("subprocess.run", return_value=fake_proc):
            with pytest.raises(ModelError, match="non-object JSON token response"):
                acquire_entra_token(VALID_SUB)


def test_acquire_entra_token_az_missing() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ModelError, match="Azure CLI .* not found"):
            acquire_entra_token(VALID_SUB)


def test_acquire_entra_token_timeout() -> None:
    with patch(
        "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="az", timeout=15)
    ):
        with pytest.raises(ModelError, match="Azure CLI token acquisition timed out"):
            acquire_entra_token(VALID_SUB)


def test_acquire_entra_token_nonzero_exit() -> None:
    fake_proc = subprocess.CompletedProcess(
        args=["az"],
        returncode=1,
        stdout="",
        stderr="ERROR: AADSTS50076: Due to a configuration change...",
    )
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(ModelError) as exc_info:
            acquire_entra_token(VALID_SUB)
        # Stderr must NOT be echoed in ModelError
        assert "AADSTS50076" not in str(exc_info.value)
        assert "exit code 1" in str(exc_info.value)


def test_acquire_entra_token_missing_token_field() -> None:
    fake_proc = subprocess.CompletedProcess(
        args=["az"],
        returncode=0,
        stdout=json.dumps({"tokenType": "Bearer"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(ModelError, match="missing accessToken"):
            acquire_entra_token(VALID_SUB)


# --- Model Selection and Construction ---


def test_azure_foundry_selection_validation() -> None:
    assert model_family("azure_foundry", "muse-spark-1.3") == "meta"
    with pytest.raises(ValueError, match="unknown azure_foundry model"):
        model_family("azure_foundry", "muse-glimmer-30B")

    validate_selection("azure_foundry", "muse-spark-1.3", "meta", "xhigh")
    validate_selection("azure_foundry", "muse-spark-1.3", "meta", "minimal")

    with pytest.raises(ValueError, match="rejects reasoning_effort 'max'"):
        validate_selection("azure_foundry", "muse-spark-1.3", "meta", "max")

    with pytest.raises(ValueError, match="belongs to 'meta', not 'claude'"):
        validate_selection("azure_foundry", "muse-spark-1.3", "claude", "xhigh")


def test_azure_foundry_init_checks() -> None:
    with pytest.raises(ValueError, match="unsupported azure_foundry model"):
        AzureFoundryModel(
            base_url=VALID_URL, subscription_id=VALID_SUB, model="other-model"
        )

    with pytest.raises(ValueError, match="rejects reasoning_effort 'max'"):
        AzureFoundryModel(base_url=VALID_URL, subscription_id=VALID_SUB, effort="max")

    with pytest.raises(ValueError, match="unsupported azure_foundry reasoning effort"):
        AzureFoundryModel(
            base_url=VALID_URL, subscription_id=VALID_SUB, effort="unknown"
        )

    with pytest.raises(ValueError, match="max_completion_tokens"):
        AzureFoundryModel(
            base_url=VALID_URL, subscription_id=VALID_SUB, max_completion_tokens=0
        )

    with pytest.raises(ValueError, match="max_completion_tokens"):
        AzureFoundryModel(
            base_url=VALID_URL, subscription_id=VALID_SUB, max_completion_tokens=20000
        )


# --- Complete Contract and Execution ---


def test_complete_success_contract() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        effort="xhigh",
        token_provider=lambda sub: "mock-entra-token",
    )

    resp_body = _make_azure_response(
        model="muse-spark-1.3",
        content='{"seed": 42, "n_rows": 100}',
        prompt_tokens=227,
        completion_tokens=1054,
        cached_tokens=50,
        reasoning_tokens=977,
        total_tokens=1281,
    )
    captured_reqs: list[urllib.request.Request] = []

    def fake_open(
        req: urllib.request.Request, *args: Any, **kwargs: Any
    ) -> MockHTTPResponse:
        captured_reqs.append(req)
        return MockHTTPResponse(resp_body)

    with patch.object(urllib.request.OpenerDirector, "open", side_effect=fake_open):
        result = model.complete("Generate practice table", TEST_SCHEMA)

    assert result.data == {"seed": 42, "n_rows": 100}
    assert result.usage.model == "muse-spark-1.3"
    assert result.usage.input_tokens == 227
    assert result.usage.output_tokens == 1054
    assert result.usage.cached_tokens == 50
    assert result.usage.reasoning_tokens == 977
    assert result.usage.calls == 1

    assert result.meta["requested_model"] == "muse-spark-1.3"
    assert result.meta["deployment"] == "muse-spark-1.3"
    assert result.meta["model"] == "muse-spark-1.3"
    assert result.meta["model_family"] == "meta"
    assert result.meta["provider"] == "azure"
    assert result.meta["endpoint_host"] == "audit-fixture.openai.azure.com"
    assert result.meta["subscription_id"] == VALID_SUB
    assert result.meta["reasoning_effort"] == "xhigh"
    assert result.meta["request_id"] == "chatcmpl-test-receipt-uuid"
    assert "reasoning_content" not in result.meta

    # Check payload sent
    assert len(captured_reqs) == 1
    sent_req = captured_reqs[0]
    assert sent_req.full_url == f"{VALID_URL}chat/completions"
    assert sent_req.headers["Authorization"] == "Bearer mock-entra-token"
    assert sent_req.headers["Content-type"] == "application/json"

    sent_body = json.loads(sent_req.data.decode("utf-8"))
    assert sent_body["model"] == "muse-spark-1.3"
    assert sent_body["reasoning_effort"] == "xhigh"
    assert sent_body["max_completion_tokens"] == 4096
    assert sent_body["response_format"]["type"] == "json_schema"
    assert sent_body["response_format"]["json_schema"]["strict"] is True
    assert sent_body["response_format"]["json_schema"]["schema"] == TEST_SCHEMA


def test_complete_prevalidates_schema_before_token() -> None:
    token_called = False

    def bad_token(sub: str) -> str:
        nonlocal token_called
        token_called = True
        return "token"

    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=bad_token,
    )

    with pytest.raises(ModelError, match="schema must be a Mapping"):
        model.complete("prompt", "not-a-mapping")  # type: ignore

    with pytest.raises(ModelError, match="invalid JSON schema"):
        model.complete("prompt", {"type": 12345})  # invalid type spec

    assert not token_called, "Schema must be pre-validated before token acquisition"


# --- Provider Canary Redaction Tests (Item 1 & Root Probe) ---


CANARY = "DO_NOT_LOG_PROVIDER_SECRET_937"


@pytest.mark.parametrize(
    "probe_name,changes",
    [
        ("refusal", {"refusal": CANARY}),
        (
            "schema_output",
            {"content": json.dumps({"seed": CANARY, "n_rows": 100})},
        ),
        (
            "extra_key",
            {"content": json.dumps({"seed": 42, "n_rows": 100, CANARY: 1})},
        ),
        ("invalid_usage", {"prompt_tokens": CANARY}),
        (
            "bad_cached",
            {"prompt_tokens": 10, "cached_tokens": CANARY, "completion_tokens": 10},
        ),
        (
            "bad_reasoning",
            {"prompt_tokens": 10, "completion_tokens": 10, "reasoning_tokens": CANARY},
        ),
        (
            "bad_total",
            {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "reasoning_tokens": 0,
                "total_tokens": CANARY,
            },
        ),
        ("observed_model", {"model": CANARY}),
        ("finish_reason", {"finish_reason": CANARY}),
        ("content_not_json", {"content": CANARY}),
        ("content_json_array", {"content": json.dumps([CANARY])}),
    ],
)
def test_provider_canary_not_leaked_in_model_error(
    probe_name: str, changes: dict[str, Any]
) -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(**changes)

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError) as exc_info:
            model.complete("invented probe only", TEST_SCHEMA)

        assert CANARY not in str(exc_info.value), (
            f"Canary leaked in ModelError for {probe_name}: {exc_info.value}"
        )
        tb = "".join(
            traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
        )
        assert CANARY not in tb, (
            f"Canary leaked in full traceback for {probe_name}: {tb}"
        )


def test_httperror_request_id_canary_not_leaked() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    err = urllib.error.HTTPError(
        url=f"{VALID_URL}chat/completions",
        code=500,
        msg="Internal Error",
        hdrs=MagicMock(
            get=lambda k, default=None: CANARY if k == "x-request-id" else default
        ),
        fp=io.BytesIO(b'{"error": "secret"}'),
    )
    with patch.object(urllib.request.OpenerDirector, "open", side_effect=err):
        with pytest.raises(ModelError) as exc_info:
            model.complete("prompt", TEST_SCHEMA)
        assert CANARY not in str(exc_info.value)
        tb = "".join(
            traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
        )
        assert CANARY not in tb


# --- No-Tool & Legacy Function Call Refusal (Item 2) ---


def test_complete_refuses_legacy_function_call() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(
        function_call={"name": "unsafe", "arguments": "{}"}
    )

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="unexpected function call"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_refuses_tool_calls() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(
        tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "browse"}}
        ]
    )

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="unexpected tool calls"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_refuses_model_refusal() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(refusal="I cannot fulfill this request")

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="returned model refusal"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_refuses_observed_model_mismatch() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(model="different-backend-model")

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="model mismatch"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_refuses_unknown_or_missing_observed_model() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(model="unknown")

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="reported unknown model"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_refuses_finish_reason_not_stop() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(finish_reason="length")

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="finish_reason is not 'stop'"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_refuses_schema_violation_in_content() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(content='{"seed": 42, "n_rows": "one hundred"}')

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(
            ModelError, match="does not conform to requested schema at n_rows"
        ):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_open_key_schema_sanitizes_provider_keys() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    # additionalProperties schema: provider canary as key name
    open_schema = {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }
    resp_add = _make_azure_response(content=json.dumps({CANARY: "not_an_integer"}))
    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_add)
    ):
        with pytest.raises(
            ModelError, match=r"does not conform to requested schema at \[key\]"
        ) as exc_info:
            model.complete("prompt", open_schema)
        assert CANARY not in str(exc_info.value)
        tb = "".join(
            traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
        )
        assert CANARY not in tb

    # patternProperties schema: provider canary in key name
    pattern_schema = {
        "type": "object",
        "patternProperties": {r"^item_": {"type": "integer"}},
        "additionalProperties": False,
    }
    resp_pat = _make_azure_response(
        content=json.dumps({f"item_{CANARY}": "not_an_integer"})
    )
    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_pat)
    ):
        with pytest.raises(
            ModelError, match=r"does not conform to requested schema at \[key\]"
        ) as exc_info:
            model.complete("prompt", pattern_schema)
        assert CANARY not in str(exc_info.value)
        tb = "".join(
            traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
        )
        assert CANARY not in tb

    # Positive control: valid data conforms to open schema
    resp_valid = _make_azure_response(content='{"dynamic_count": 42}')
    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_valid)
    ):
        c = model.complete("prompt", open_schema)
        assert c.data == {"dynamic_count": 42}


def test_complete_refuses_invalid_json_content() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(content="not json at all")

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="not valid JSON"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_refuses_json_array_when_object_expected() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(content='[{"seed": 42, "n_rows": 100}]')

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match="structured data must be a JSON object"):
            model.complete("prompt", TEST_SCHEMA)


@pytest.mark.parametrize(
    "usage_kwargs,err_match",
    [
        ({"prompt_tokens": -1}, "prompt_tokens must be non-negative integer"),
        (
            {"completion_tokens": -5},
            "completion_tokens must be non-negative integer",
        ),
        (
            {"cached_tokens": 500, "prompt_tokens": 100},
            "cached_tokens must be non-negative integer <= prompt_tokens",
        ),
        (
            {"reasoning_tokens": 500, "completion_tokens": 100},
            "reasoning_tokens must be non-negative integer <= completion_tokens",
        ),
        (
            {
                "total_tokens": 9999,
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "reasoning_tokens": 5,
            },
            "total_tokens does not match",
        ),
    ],
)
def test_complete_refuses_invalid_usage(
    usage_kwargs: dict[str, Any], err_match: str
) -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(**usage_kwargs)

    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(resp_body)
    ):
        with pytest.raises(ModelError, match=err_match):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_handles_http_error_sanitized() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )

    err = urllib.error.HTTPError(
        url=f"{VALID_URL}chat/completions",
        code=400,
        msg="Bad Request",
        hdrs=MagicMock(
            get=lambda k, default=None: (
                "az-req-id-777" if k == "x-request-id" else default
            )
        ),
        fp=io.BytesIO(
            b'{"error":{"message":"Internal Azure details with confidential subscription 1111..."}}'
        ),
    )

    with patch.object(urllib.request.OpenerDirector, "open", side_effect=err):
        with pytest.raises(ModelError) as exc_info:
            model.complete("prompt", TEST_SCHEMA)

        msg = str(exc_info.value)
        assert "HTTP 400" in msg
        assert "confidential" not in msg, (
            "Raw response body must not be leaked in ModelError"
        )


def test_complete_refuses_oversized_payload() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )

    huge_response = b"a" * (10 * 1024 * 1024 + 10)
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(huge_response),
    ):
        with pytest.raises(ModelError, match="exceeded maximum allowed size"):
            model.complete("prompt", TEST_SCHEMA)


# --- Bearer Token Charset & Injection Protection (B2) ---


@pytest.mark.parametrize(
    "bad_tok",
    [
        "abc\ndef",
        "abc\rdef",
        "abc def",
        "",
        123,
        None,
        "token@host",
        "tok#en",
        "abc\n",
        "SECRETVAL\n",
        "\nabc",
        "token\r\n",
    ],
)
def test_bearer_token_validation_rejects_unsafe(bad_tok: Any) -> None:
    with pytest.raises(ModelError, match="invalid bearer token character set"):
        _validate_bearer_token(bad_tok)


def test_complete_injected_newline_token_does_not_leak_secret() -> None:
    secret = "SUPER_SECRET_TOKEN_TAIL_99"
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: f"abc\n{secret}",
    )
    with pytest.raises(ModelError) as exc_info:
        model.complete("prompt", TEST_SCHEMA)
    assert secret not in str(exc_info.value)
    tb = "".join(traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb))
    assert secret not in tb


def test_complete_injected_trailing_newline_token_refused_cleanly() -> None:
    secret = "SECRETVAL_TRAILING_LF_99"
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: f"{secret}\n",
    )
    with pytest.raises(
        ModelError, match="invalid bearer token character set"
    ) as exc_info:
        model.complete("prompt", TEST_SCHEMA)
    assert secret not in str(exc_info.value)
    tb = "".join(traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb))
    assert secret not in tb


def test_complete_valid_bearer_token_positive_control() -> None:
    valid_token = "valid.entra.token-123_456~789+/="
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: valid_token,
    )
    resp_body = _make_azure_response()
    captured_reqs: list[urllib.request.Request] = []

    def fake_open(
        req: urllib.request.Request, *args: Any, **kwargs: Any
    ) -> MockHTTPResponse:
        captured_reqs.append(req)
        return MockHTTPResponse(resp_body)

    with patch.object(urllib.request.OpenerDirector, "open", side_effect=fake_open):
        result = model.complete("prompt", TEST_SCHEMA)
    assert result.data == {"seed": 42, "n_rows": 100}
    assert captured_reqs[0].headers["Authorization"] == f"Bearer {valid_token}"


# --- Network Read Failure Protection (B3) ---


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: ConnectionResetError(f"reset {CANARY}"),
        lambda: http.client.IncompleteRead(f"incomplete {CANARY}".encode()),
        lambda: OSError(f"network os error {CANARY}"),
        lambda: http.client.BadStatusLine(f"500 {CANARY}"),
        lambda: ValueError(f"raw header value {CANARY}"),
    ],
)
def test_network_read_failures_mapped_to_model_error(exc_factory: Any) -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    with patch.object(urllib.request.OpenerDirector, "open", side_effect=exc_factory()):
        with pytest.raises(
            ModelError, match="Azure Foundry network connection error"
        ) as exc_info:
            model.complete("prompt", TEST_SCHEMA)
        assert CANARY not in str(exc_info.value)
        tb = "".join(
            traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
        )
        assert CANARY not in tb


# --- Unsupported Message Fields & Allowed Response Modes (B4) ---


def test_complete_refuses_unsupported_message_fields() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(extra_msg_fields={"audio": {"data": "x"}})
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_body),
    ):
        with pytest.raises(ModelError, match="unsupported fields"):
            model.complete("prompt", TEST_SCHEMA)

    resp_body2 = _make_azure_response(extra_msg_fields={"annotations": [{"x": 1}]})
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_body2),
    ):
        with pytest.raises(ModelError, match="unsupported fields"):
            model.complete("prompt", TEST_SCHEMA)


def test_complete_accepts_empty_tool_calls_list() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    resp_body = _make_azure_response(tool_calls=[])
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_body),
    ):
        c = model.complete("prompt", TEST_SCHEMA)
        assert c.data == {"seed": 42, "n_rows": 100}


# --- Strict JSON Parsing: Non-Finite & Duplicate Keys (B9) ---


def test_complete_strict_json_parsing_rejects_nan_and_duplicates() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    # NaN literal
    resp_nan = _make_azure_response(content='{"seed": 42, "n_rows": NaN}')
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_nan),
    ):
        with pytest.raises(ModelError, match="not valid JSON"):
            model.complete("prompt", TEST_SCHEMA)

    # Duplicate keys in object
    resp_dup = _make_azure_response(content='{"seed": 42, "n_rows": 100, "seed": 99}')
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_dup),
    ):
        with pytest.raises(ModelError, match="not valid JSON"):
            model.complete("prompt", TEST_SCHEMA)


# --- Request ID Bounding & Sanitization (B7) ---


def test_complete_bounds_and_sanitizes_request_id() -> None:
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )
    # 5M chars id
    resp_big = _make_azure_response(rid="X" * 5_000_000)
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_big),
    ):
        c = model.complete("prompt", TEST_SCHEMA)
        assert c.meta["request_id"] == "unavailable"

    # dict id
    resp_dict_id = _make_azure_response(rid={"a": "secret"})
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_dict_id),
    ):
        c = model.complete("prompt", TEST_SCHEMA)
        assert c.meta["request_id"] == "unavailable"

    # normal id
    resp_normal = _make_azure_response(rid="chatcmpl-valid.123_456:789")
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_normal),
    ):
        c = model.complete("prompt", TEST_SCHEMA)
        assert c.meta["request_id"] == "chatcmpl-valid.123_456:789"

    # trailing newline in id (R3: fullmatch)
    resp_trailing_lf = _make_azure_response(rid="req-77\n")
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_trailing_lf),
    ):
        c = model.complete("prompt", TEST_SCHEMA)
        assert c.meta["request_id"] == "unavailable"


# --- Az Subscription Echo & Exception Handling (B11) ---


def test_acquire_entra_token_subscription_check() -> None:
    # matching subscription
    proc_ok = subprocess.CompletedProcess(
        args=["az"],
        returncode=0,
        stdout=json.dumps({"accessToken": "valid.token", "subscription": VALID_SUB}),
        stderr="",
    )
    with patch("subprocess.run", return_value=proc_ok):
        tok = acquire_entra_token(VALID_SUB)
        assert tok == "valid.token"

    # mismatched subscription
    proc_bad = subprocess.CompletedProcess(
        args=["az"],
        returncode=0,
        stdout=json.dumps(
            {
                "accessToken": "valid.token",
                "subscription": "99999999-9999-9999-9999-999999999999",
            }
        ),
        stderr="",
    )
    with patch("subprocess.run", return_value=proc_bad):
        with pytest.raises(ModelError, match="subscription does not match"):
            acquire_entra_token(VALID_SUB)

    # absent subscription is accepted
    proc_no_sub = subprocess.CompletedProcess(
        args=["az"],
        returncode=0,
        stdout=json.dumps({"accessToken": "valid.token"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=proc_no_sub):
        tok = acquire_entra_token(VALID_SUB)
        assert tok == "valid.token"

    # PermissionError
    with patch("subprocess.run", side_effect=PermissionError("denied")):
        with pytest.raises(ModelError, match="permission denied"):
            acquire_entra_token(VALID_SUB)

    # OSError
    with patch("subprocess.run", side_effect=OSError("os err")):
        with pytest.raises(ModelError, match="failed to execute"):
            acquire_entra_token(VALID_SUB)


# --- Text-Only Contract & Vision Protocol (B10) ---


def test_azure_foundry_spark_complete_with_image_refuses_before_auth() -> None:
    token_called = False

    def dummy_token_provider(sub: str) -> str:
        nonlocal token_called
        token_called = True
        return "tok"

    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-spark-1.3",
        token_provider=dummy_token_provider,
    )
    with pytest.raises(ModelError, match="does not support image modality"):
        model.complete_with_image("prompt", TEST_SCHEMA, "/any/path.png")

    assert not token_called


# --- Prepare & Plan Schemas Validated Offline (B11) ---


def test_complete_validates_actual_prepare_and_plan_schemas_offline() -> None:
    import profiles as pp
    from engine.cli.request import request_schema
    from engine.core.manifest import load_manifest
    from profiles.tabular.generation.plan import PLAN_SCHEMA

    manifest = load_manifest(pp.manifest_path("tabular"))
    prep_schema = request_schema(manifest)
    plan_schema = PLAN_SCHEMA

    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        token_provider=lambda s: "tok",
    )

    # Test prepare schema
    prep_content = json.dumps(
        {
            "summary": "draft plan",
            "unsupported": [],
            "n_rows": 50,
            "seed": 42,
            "strategy": "fitted_gaussian_disclosed",
            "outputs": ["scalar"],
        }
    )
    resp_prep = _make_azure_response(content=prep_content)
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_prep),
    ):
        c_prep = model.complete("draft prompt", prep_schema)
        assert c_prep.data["n_rows"] == 50

    # Test plan schema
    plan_content = json.dumps(
        {
            "seed": 42,
            "n_rows": 50,
            "strategy": "fitted_gaussian_disclosed",
        }
    )
    resp_plan = _make_azure_response(content=plan_content)
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp_plan),
    ):
        c_plan = model.complete("plan prompt", plan_schema)
        assert c_plan.data["seed"] == 42


# --- Azure Vision (complete_with_image) Tests ---


def _make_test_png(width: int = 2, height: int = 2) -> bytes:
    import struct
    import zlib

    magic = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I4s", 13, b"IHDR") + ihdr_data + struct.pack(">I", ihdr_crc)
    raw = b"".join(b"\x00" + (b"\xff\x00\x00\xff" * width) for _ in range(height))
    compressed = zlib.compress(raw)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = (
        struct.pack(">I4s", len(compressed), b"IDAT")
        + compressed
        + struct.pack(">I", idat_crc)
    )
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", iend_crc)
    return magic + ihdr + idat + iend


def test_azure_foundry_glimmer_complete_with_image_payload_and_media_metadata(
    tmp_path: Path,
) -> None:
    img_path = tmp_path / "valid.png"
    img_bytes = _make_test_png(width=2, height=2)
    img_path.write_bytes(img_bytes)

    captured_req: list[urllib.request.Request] = []

    def mock_open(
        self: Any, req: urllib.request.Request, **kwargs: Any
    ) -> MockHTTPResponse:
        captured_req.append(req)
        return MockHTTPResponse(
            _make_azure_response(
                model="muse-glimmer-30b",
                content='{"seed": 42, "n_rows": 100}',
                prompt_tokens=300,
                completion_tokens=500,
                reasoning_tokens=400,
                total_tokens=800,
            )
        )

    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-glimmer-30b",
        token_provider=lambda s: "mock-bearer-token",
    )

    with patch.object(urllib.request.OpenerDirector, "open", mock_open):
        c = model.complete_with_image("critique prompt", TEST_SCHEMA, img_path)

    assert len(captured_req) == 1
    req = captured_req[0]
    assert req.get_header("Authorization") == "Bearer mock-bearer-token"
    assert req.get_header("Content-type") == "application/json"

    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "muse-glimmer-30b"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    parts = body["messages"][0]["content"]
    assert len(parts) == 2
    assert parts[0] == {"type": "text", "text": "critique prompt"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    from base64 import b64decode

    assert b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]) == img_bytes

    # Verify structured answer
    assert c.data == {"seed": 42, "n_rows": 100}

    # Verify usage without double counting reasoning tokens
    assert c.usage.input_tokens == 300
    assert c.usage.output_tokens == 500
    assert c.usage.reasoning_tokens == 400
    assert c.usage.model == "muse-glimmer-30b"

    # Verify media metadata has digest, dimensions, size, but NO base64 pixels
    media = c.meta["media"]
    assert media["media_type"] == "image/png"
    assert media["width"] == 2
    assert media["height"] == 2
    assert media["size_bytes"] == len(img_bytes)
    assert media["digest"].startswith("sha256:")
    assert "data:image" not in str(c.meta)
    assert "base64" not in str(c.meta)

    # Verify cost is explicitly unavailable with reason
    assert c.meta["cost"] == {
        "status": "unavailable",
        "reason": "transport reports no valid monetary cost",
    }

    # Verify declared protocol format distinguished from live validation
    assert c.meta["protocol"] == {
        "modality": "text+image_url",
        "format": "openai_v1_chat_completions",
    }


def test_azure_foundry_complete_with_image_model_mismatch_refused(
    tmp_path: Path,
) -> None:
    img_path = tmp_path / "img.png"
    img_path.write_bytes(_make_test_png(2, 2))

    resp = _make_azure_response(
        model="wrong-returned-model",
        content='{"seed": 42, "n_rows": 100}',
    )
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-glimmer-30b",
        token_provider=lambda s: "tok",
    )
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp),
    ):
        with pytest.raises(ModelError, match="Azure Foundry model mismatch"):
            model.complete_with_image("prompt", TEST_SCHEMA, img_path)


def test_azure_foundry_complete_with_image_tool_calls_refused(
    tmp_path: Path,
) -> None:
    img_path = tmp_path / "img.png"
    img_path.write_bytes(_make_test_png(2, 2))

    resp = _make_azure_response(
        model="muse-glimmer-30b",
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "foo"}}],
    )
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-glimmer-30b",
        token_provider=lambda s: "tok",
    )
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp),
    ):
        with pytest.raises(ModelError, match="unexpected tool calls"):
            model.complete_with_image("prompt", TEST_SCHEMA, img_path)


def test_azure_foundry_complete_with_image_corrupt_png_refused_before_network(
    tmp_path: Path,
) -> None:
    img_path = tmp_path / "corrupt.png"
    img_path.write_bytes(b"NOT_A_PNG_DATA" * 4)

    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-glimmer-30b",
        token_provider=lambda s: "tok",
    )
    with patch.object(urllib.request.OpenerDirector, "open") as mock_open:
        with pytest.raises(ModelError, match="invalid PNG magic header"):
            model.complete_with_image("prompt", TEST_SCHEMA, img_path)
        assert mock_open.call_count == 0


def test_azure_foundry_complete_with_image_network_timeout_redaction(
    tmp_path: Path,
) -> None:
    img_path = tmp_path / "valid.png"
    img_path.write_bytes(_make_test_png(2, 2))

    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-glimmer-30b",
        token_provider=lambda s: "tok",
    )
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        side_effect=TimeoutError("request timed out"),
    ):
        with pytest.raises(ModelError, match="Azure Foundry network connection error"):
            model.complete_with_image("prompt", TEST_SCHEMA, img_path)


def test_azure_foundry_complete_with_image_reported_cost(
    tmp_path: Path,
) -> None:
    img_path = tmp_path / "valid.png"
    img_path.write_bytes(_make_test_png(2, 2))

    resp = _make_azure_response(
        model="muse-glimmer-30b",
        content='{"seed": 42, "n_rows": 100}',
        cost={"amount": 0.0042, "currency": "USD", "source": "azure"},
    )
    model = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-glimmer-30b",
        token_provider=lambda s: "tok",
    )
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(resp),
    ):
        c = model.complete_with_image("prompt", TEST_SCHEMA, img_path)
    assert c.meta["cost"] == {
        "status": "reported",
        "amount": 0.0042,
        "currency": "USD",
        "source": "azure",
    }


MANAGED_URL = "https://audit-fixture.services.ai.azure.com/openai/v1/"
MANAGED_MODEL = "meta-models--muse-glimmer-30b"


def _managed_model(**overrides: Any) -> AzureFoundryModel:
    return AzureFoundryModel(
        **{
            "base_url": MANAGED_URL,
            "subscription_id": VALID_SUB,
            "model": MANAGED_MODEL,
            "effort": "none",
            "token_provider": lambda _: "fixture.token",
            **overrides,
        }
    )


def test_managed_route_is_explicit_and_old_validator_stays_strict() -> None:
    assert validate_azure_base_url(MANAGED_URL, model=MANAGED_MODEL) == MANAGED_URL
    with pytest.raises(ValueError):
        validate_azure_base_url(MANAGED_URL)
    assert validate_azure_base_url(VALID_URL) == VALID_URL
    with pytest.raises(ValueError):
        _managed_model(base_url=VALID_URL)
    with pytest.raises(ValueError):
        _managed_model(model="muse-spark-1.3", effort="low")
    with pytest.raises(ValueError):
        _managed_model(model="muse-glimmer-30b", effort="low")


@pytest.mark.parametrize(
    "url",
    [
        "http://audit-fixture.services.ai.azure.com/openai/v1/",
        "https://audit-fixture.services.ai.azure.com.evil.example/openai/v1/",
        "https://nested.audit-fixture.services.ai.azure.com/openai/v1/",
        "https://audit-fixture.services.ai.azure.com:443/openai/v1/",
        "https://user@audit-fixture.services.ai.azure.com/openai/v1/",
        "https://audit-fixture.services.ai.azure.com/models/",
        "https://audit-fixture.services.ai.azure.com/openai/v1/?key=CANARY",
        "https://audit-fixture.services.ai.azure.com/openai/v1/#CANARY",
    ],
)
def test_managed_bad_route_refused_before_auth(url: str) -> None:
    provider = MagicMock(return_value="fixture.token")
    with pytest.raises(ValueError):
        _managed_model(base_url=url, token_provider=provider)
    provider.assert_not_called()


@pytest.mark.parametrize("effort", ["low", "xhigh", "max", "NONE"])
def test_managed_refuses_assumed_reasoning_effort(effort: str) -> None:
    with pytest.raises(ValueError, match="requires reasoning_effort 'none'"):
        _managed_model(effort=effort)
    with pytest.raises(ValueError, match="requires reasoning_effort 'none'"):
        validate_selection("azure_foundry", MANAGED_MODEL, "meta", effort)


def test_managed_wire_image_and_usage_contract(tmp_path: Path) -> None:
    import base64

    image = tmp_path / "generated.png"
    payload = _make_test_png()
    image.write_bytes(payload)
    model = _managed_model()
    wire = _make_azure_response(model=MANAGED_MODEL)
    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(wire)
    ) as opened:
        result = model.complete_with_image("synthetic only", TEST_SCHEMA, image)
    request = opened.call_args.args[0]
    sent = json.loads(request.data)
    assert request.full_url == MANAGED_URL + "chat/completions"
    assert sent["model"] == MANAGED_MODEL
    assert sent["max_tokens"] == 4096
    assert "reasoning_effort" not in sent
    assert "max_completion_tokens" not in sent
    part = sent["messages"][0]["content"][1]
    assert part["type"] == "image_url"
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == payload
    assert result.usage.model == MANAGED_MODEL
    assert result.usage.input_tokens == 227
    assert result.usage.output_tokens == 1054
    assert result.meta["deployment_type"] == "GlobalManagedCompute"
    assert result.meta["reasoning_effort"] is None
    assert result.meta["cost"]["status"] == "unavailable"
    assert model_family("azure_foundry", MANAGED_MODEL) == "meta"


@pytest.mark.parametrize(
    "response,reason",
    [
        (_make_azure_response(model="muse-spark-1.3"), "model mismatch"),
        (_make_azure_response(model=MANAGED_MODEL, content="{}"), "schema"),
        (_make_azure_response(model=MANAGED_MODEL, content="not JSON"), "valid JSON"),
        (_make_azure_response(model=MANAGED_MODEL, prompt_tokens=-1), "prompt_tokens"),
    ],
)
def test_managed_bad_image_reply_is_not_success(
    tmp_path: Path, response: bytes, reason: str
) -> None:
    image = tmp_path / "generated.png"
    image.write_bytes(_make_test_png())
    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(response)
    ):
        with pytest.raises(ModelError, match=reason):
            _managed_model().complete_with_image("synthetic only", TEST_SCHEMA, image)


def test_managed_failure_redacts_provider_body(tmp_path: Path) -> None:
    image = tmp_path / "generated.png"
    image.write_bytes(_make_test_png())
    error = urllib.error.HTTPError(
        MANAGED_URL, 503, "PROVIDER_CANARY", {}, io.BytesIO(b"PROVIDER_CANARY")
    )
    with patch.object(urllib.request.OpenerDirector, "open", side_effect=error):
        with pytest.raises(ModelError, match="HTTP 503") as raised:
            _managed_model().complete_with_image("synthetic only", TEST_SCHEMA, image)
    assert "PROVIDER_CANARY" not in str(raised.value)


def test_case_research_wire_declares_optional_passage_field_required() -> None:
    import copy

    from profiles.hylogger.validation.workflow import RESEARCH_CASE_SCHEMA

    original = copy.deepcopy(RESEARCH_CASE_SCHEMA)
    data = {
        "case_id": "teaching-case",
        "case_version": 1,
        "cited_evidence_ids": ["admitted"],
        "passage_citations": [],
        "supported_requirements": [],
        "unsupported_requirements": [],
        "research_summary": "Bounded teaching advisory.",
        "limitations": [],
    }
    reply = _make_azure_response(content=json.dumps(data))
    model = AzureFoundryModel(VALID_URL, VALID_SUB, token_provider=lambda _: "tok")
    with patch.object(
        urllib.request.OpenerDirector, "open", return_value=MockHTTPResponse(reply)
    ) as opened:
        model.complete("admitted evidence only", RESEARCH_CASE_SCHEMA)
    wire = json.loads(opened.call_args.args[0].data)["response_format"]["json_schema"][
        "schema"
    ]
    assert "passage_citations" in wire["required"]
    assert set(wire["required"]) == set(wire["properties"])
    assert RESEARCH_CASE_SCHEMA == original
    assert "passage_citations" not in RESEARCH_CASE_SCHEMA["required"]


def test_azure_required_projection_preserves_nested_constraints_and_annotations() -> (
    None
):
    import copy

    schema = {
        "type": "object",
        "properties": {
            "seed": {"type": "integer"},
            "n_rows": {"type": "integer"},
            "optional_proof": {
                "type": "object",
                "properties": {
                    "properties": {"type": "string"},
                    "proof": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 5},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
                "required": ["properties"],
                "additionalProperties": False,
            },
        },
        "required": ["seed", "n_rows"],
        "additionalProperties": False,
        "default": {"properties": {"annotation": {"type": "string"}}},
    }
    original = copy.deepcopy(schema)
    content = json.dumps(
        {
            "seed": 8,
            "n_rows": 1000,
            "optional_proof": {"properties": "data", "proof": [6]},
        }
    )
    model = AzureFoundryModel(VALID_URL, VALID_SUB, token_provider=lambda _: "tok")
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(_make_azure_response(content=content)),
    ) as opened:
        model.complete("bounded", schema)
    wire = json.loads(opened.call_args.args[0].data)["response_format"]["json_schema"][
        "schema"
    ]
    assert wire["required"] == ["seed", "n_rows", "optional_proof"]
    assert wire["properties"]["optional_proof"]["required"] == ["properties", "proof"]
    assert wire["default"] == original["default"]
    assert schema == original
    proof = wire["properties"]["optional_proof"]["properties"]["proof"]
    assert proof["minItems"] == 1 and proof["uniqueItems"] is True
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=MockHTTPResponse(
            _make_azure_response(content=content.replace("[6]", "[1]"))
        ),
    ):
        with pytest.raises(ModelError, match="schema"):
            model.complete("bounded", schema)
