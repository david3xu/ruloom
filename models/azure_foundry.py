"""Azure AI Foundry structured-model adapter for Meta Muse models.

The adapter POSTs ``{base_url}chat/completions`` against an explicit verified
Azure OpenAI v1 endpoint (``https://<account>.openai.azure.com/openai/v1/``)
using Entra ID bearer tokens acquired via the Azure CLI (``az account
get-access-token``) scoped to a mandatory canonical subscription UUID.

Standard library + ``jsonschema`` only: no hosted SDK, no Meta account calls,
no API keys, no query parameters, and no ambient default account.

The adapter validates the returned JSON against the requested schema using
``jsonschema``, verifies that the observed model matches the requested model,
enforces non-negative bounded token usage without double-counting reasoning
tokens, discards provider-returned reasoning token blobs, and never logs
authorization headers or bearer tokens.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import jsonschema

from .base import (
    Completion,
    ModelCapabilities,
    ModelError,
    StructuredModel,
    Usage,
    image_wire_schema,
    monetary_cost,
    structured_image_schema,
)
from .image_input import read_validated_image
from .local_transport import _NoRedirect

#: Declared capabilities per supported Azure Foundry model.
#: Spark on Azure remains text-only; Glimmer multimodal protocol is implemented
#: with declared protocol format and honest metadata regarding unverified capacity.
AZURE_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    "muse-spark-1.3": ModelCapabilities(
        input_modalities=("text",),
        structured_output=True,
        image_media_types=(),
        max_image_bytes=0,
        max_image_pixels=0,
    ),
    "muse-glimmer-30b": ModelCapabilities(
        input_modalities=("text", "image"),
        structured_output=True,
        image_media_types=("image/png",),
        max_image_bytes=4 * 1024 * 1024,
        max_image_pixels=4_000_000,
    ),
}

#: Exact default model identifier supported by this Azure deployment slice.
SUPPORTED_MODEL = "muse-spark-1.3"

#: Set of supported models for Azure Foundry.
SUPPORTED_MODELS = frozenset(AZURE_MODEL_CAPABILITIES.keys())

#: Default and maximum completion token bounds.
DEFAULT_MAX_COMPLETION_TOKENS = 4096
MAX_ALLOWED_COMPLETION_TOKENS = 16384

#: Default reasoning effort; 'max' is explicitly refused by backend.
DEFAULT_REASONING_EFFORT = "xhigh"
SUPPORTED_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})

#: Maximum response payload size (10 MB).
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

#: Maximum allowed request timeout in seconds (5 minutes).
MAX_ALLOWED_TIMEOUT_SECONDS = 300.0


def _validate_timeout(timeout: float) -> float:
    """Validate that timeout is a finite positive number within supported range."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or math.isnan(timeout)
        or math.isinf(timeout)
        or timeout <= 0.0
        or timeout > MAX_ALLOWED_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"timeout must be a finite positive number <= {MAX_ALLOWED_TIMEOUT_SECONDS}, got {timeout!r}"
        )
    return float(timeout)


#: UUID canonical regex pattern (8-4-4-4-12 hex).
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: Valid DNS label pattern for account subdomains.
ACCOUNT_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

#: Verified Azure OpenAI host suffix.
AZURE_OPENAI_HOST_SUFFIX = ".openai.azure.com"

#: Resource audience for Azure OpenAI / Cognitive Services.
COGNITIVE_SERVICES_RESOURCE = "https://cognitiveservices.azure.com"

#: Conservative charset for Entra ID bearer tokens (no whitespace or control chars).
BEARER_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+\Z")

#: Transport request ID conservative format (bounded alphanumeric + delimiters).
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")

#: Allowed message keys returned by backend choices.
SUPPORTED_MESSAGE_KEYS = frozenset(
    {"role", "content", "reasoning_content", "refusal", "tool_calls", "function_call"}
)


def _validate_bearer_token(token: Any) -> str:
    """Validate that token contains only safe bearer charset characters."""
    if not isinstance(token, str) or not token or not BEARER_TOKEN_RE.fullmatch(token):
        raise ModelError("invalid bearer token character set") from None
    return token


def _sanitize_request_id(val: Any) -> str:
    """Validate and bound request ID to a safe transport identifier."""
    if isinstance(val, str) and REQUEST_ID_RE.fullmatch(val):
        return val
    return "unavailable"


def _format_schema_path(exc_path: Any, schema: Mapping[str, Any]) -> str:
    """Format schema validation path, suppressing any provider-supplied keys.

    Only schema-defined property names and integer array indices are preserved.
    Provider-supplied keys matching additionalProperties or patternProperties
    are replaced with '[key]' to prevent echoing unvalidated input.
    """
    if not exc_path:
        return "root"

    parts: list[str] = []
    curr: Any = schema
    for seg in exc_path:
        if isinstance(seg, int) and not isinstance(seg, bool):
            parts.append(str(seg))
            if isinstance(curr, Mapping):
                items = curr.get("items")
                if isinstance(items, Mapping):
                    curr = items
                elif (
                    isinstance(items, list)
                    and seg < len(items)
                    and isinstance(items[seg], Mapping)
                ):
                    curr = items[seg]
                else:
                    curr = None
            else:
                curr = None
        elif isinstance(seg, str):
            curr_props = curr.get("properties") if isinstance(curr, Mapping) else None
            if isinstance(curr_props, Mapping) and seg in curr_props:
                parts.append(seg)
                curr = curr_props[seg]
            else:
                parts.append("[key]")
                if isinstance(curr, Mapping):
                    add_props = curr.get("additionalProperties")
                    if isinstance(add_props, Mapping):
                        curr = add_props
                    else:
                        curr = None
                else:
                    curr = None
        else:
            parts.append("[item]")
            curr = None

    return "/".join(parts) if parts else "root"


def _reject_constant(val: str) -> None:
    """Reject NaN, Infinity, -Infinity in JSON content."""
    raise ValueError(f"non-finite number literal forbidden: {val}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate object keys in JSON content."""
    d: dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key in JSON object: {k}")
        d[k] = v
    return d


def validate_subscription_id(value: str) -> str:
    """Validate that value is a non-empty canonical UUID string."""
    if not isinstance(value, str) or not UUID_RE.match(value.strip()):
        raise ValueError(
            f"subscription_id must be a canonical UUID (8-4-4-4-12 hex), got {value!r}"
        )
    return value.strip().lower()


def validate_azure_base_url(value: str) -> str:
    """Validate and normalize a verified Azure OpenAI v1 base URL.

    Admitted route: ``https://<account>.openai.azure.com/openai/v1/``
    Arbitrary HTTPS hosts, custom ports, userinfo, query, fragment, and
    unverified paths (e.g. services.ai/models) are strictly refused.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Azure base URL must be a non-empty string")
    try:
        url = urllib.parse.urlsplit(value.strip())
    except Exception as exc:
        raise ValueError(f"invalid Azure base URL: {exc}") from exc

    if url.scheme != "https":
        raise ValueError(f"Azure base URL must use HTTPS, got scheme {url.scheme!r}")
    if url.username or url.password:
        raise ValueError("Azure base URL cannot contain credentials")
    if ":" in url.netloc:
        raise ValueError("Azure base URL cannot specify a port")
    if url.query:
        raise ValueError("Azure base URL cannot contain query parameters")
    if url.fragment:
        raise ValueError("Azure base URL cannot contain a URL fragment")

    hostname = (url.hostname or "").lower()
    if not hostname.endswith(AZURE_OPENAI_HOST_SUFFIX):
        raise ValueError(
            f"Azure base URL host {hostname!r} must end with {AZURE_OPENAI_HOST_SUFFIX!r}"
        )

    account = hostname[: -len(AZURE_OPENAI_HOST_SUFFIX)]
    if not account or not ACCOUNT_LABEL_RE.match(account):
        raise ValueError(f"Azure base URL account label {account!r} is invalid")

    norm_path = url.path.rstrip("/")
    if norm_path != "/openai/v1":
        raise ValueError(f"Azure base URL path must be '/openai/v1/', got {url.path!r}")

    return f"https://{hostname}/openai/v1/"


def acquire_entra_token(
    subscription_id: str,
    resource: str = COGNITIVE_SERVICES_RESOURCE,
    timeout: float = 15.0,
) -> str:
    """Acquire an Entra ID bearer token via Azure CLI for the given subscription."""
    valid_timeout = _validate_timeout(timeout)
    sub_uuid = validate_subscription_id(subscription_id)
    cmd = [
        "az",
        "account",
        "get-access-token",
        "--resource",
        resource,
        "--subscription",
        sub_uuid,
        "--output",
        "json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=valid_timeout,
            check=False,
        )
    except FileNotFoundError:
        raise ModelError(
            "Azure CLI ('az') is not installed or not found on PATH"
        ) from None
    except PermissionError:
        raise ModelError("permission denied executing Azure CLI ('az')") from None
    except subprocess.TimeoutExpired:
        raise ModelError("Azure CLI token acquisition timed out") from None
    except OSError:
        raise ModelError("failed to execute Azure CLI ('az')") from None

    if proc.returncode != 0:
        raise ModelError(
            f"Azure CLI token acquisition failed with exit code {proc.returncode}"
        ) from None

    try:
        data = json.loads(proc.stdout)
    except Exception:
        raise ModelError("Azure CLI returned unparseable JSON token response") from None

    if not isinstance(data, dict):
        raise ModelError("Azure CLI returned non-object JSON token response") from None

    az_sub = data.get("subscription")
    if az_sub is not None:
        if not isinstance(az_sub, str) or az_sub.strip().lower() != sub_uuid:
            raise ModelError(
                "Azure CLI token subscription does not match requested subscription"
            ) from None

    token = data.get("accessToken") or data.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ModelError("Azure CLI response missing accessToken") from None

    return _validate_bearer_token(token.strip())


class AzureFoundryModel(StructuredModel):
    """Structured model adapter for Meta Muse models on Azure AI Foundry."""

    def __init__(
        self,
        base_url: str,
        subscription_id: str,
        model: str = SUPPORTED_MODEL,
        effort: str = DEFAULT_REASONING_EFFORT,
        max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
        timeout: float = 60.0,
        token_provider: Callable[[str], str] | None = None,
    ) -> None:
        self.base_url = validate_azure_base_url(base_url)
        self.subscription_id = validate_subscription_id(subscription_id)

        if model not in SUPPORTED_MODELS:
            raise ValueError(
                f"unsupported azure_foundry model {model!r}; supported models: {sorted(SUPPORTED_MODELS)}"
            )
        self.model = model
        self.capabilities = AZURE_MODEL_CAPABILITIES[model]

        if effort == "max":
            raise ValueError(
                "Azure Foundry rejects reasoning_effort 'max'; supported values: minimal, low, medium, high, xhigh"
            )
        if effort not in SUPPORTED_EFFORTS:
            raise ValueError(
                f"unsupported azure_foundry reasoning effort {effort!r}; "
                f"supported values: {sorted(SUPPORTED_EFFORTS)}"
            )
        self.effort = effort

        if (
            type(max_completion_tokens) is not int
            or isinstance(max_completion_tokens, bool)
            or max_completion_tokens <= 0
            or max_completion_tokens > MAX_ALLOWED_COMPLETION_TOKENS
        ):
            raise ValueError(
                f"max_completion_tokens must be a positive integer <= {MAX_ALLOWED_COMPLETION_TOKENS}, "
                f"got {max_completion_tokens!r}"
            )
        self.max_completion_tokens = max_completion_tokens
        self.timeout = _validate_timeout(timeout)
        self._token_provider = token_provider

    def _get_token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider(self.subscription_id)
        return acquire_entra_token(self.subscription_id)

    def _call_endpoint(
        self,
        messages: list[dict[str, Any]],
        schema: Mapping[str, Any],
        *,
        vision: bool = False,
    ) -> Completion:
        """Internal dispatch to Azure Foundry chat completions endpoint."""
        if not isinstance(schema, Mapping):
            raise ModelError(
                f"schema must be a Mapping, got {type(schema).__name__}"
            ) from None
        try:
            validator_cls = jsonschema.validators.validator_for(dict(schema))
            validator_cls.check_schema(dict(schema))
        except jsonschema.SchemaError:
            raise ModelError("invalid JSON schema") from None

        token = _validate_bearer_token(self._get_token())

        payload = {
            "model": self.model,
            "messages": messages,
            "reasoning_effort": self.effort,
            "max_completion_tokens": self.max_completion_tokens,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": image_wire_schema(schema) if vision else dict(schema),
                },
            },
        }

        endpoint = f"{self.base_url}chat/completions"
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
        except ValueError:
            raise ModelError("invalid request headers") from None

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        t0 = time.monotonic()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise ModelError(
                        f"Azure Foundry response exceeded maximum allowed size ({MAX_RESPONSE_BYTES} bytes)"
                    ) from None
        except ModelError:
            raise
        except urllib.error.HTTPError as exc:
            # Do NOT dump raw response body or stderr to avoid echoing secrets
            raise ModelError(f"Azure Foundry HTTP {exc.code} for endpoint") from None
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            ValueError,
        ):
            raise ModelError("Azure Foundry network connection error") from None

        elapsed = time.monotonic() - t0

        try:
            resp_dict = json.loads(
                raw_bytes.decode("utf-8"),
                **(
                    {
                        "parse_constant": _reject_constant,
                        "object_pairs_hook": _reject_duplicates,
                    }
                    if vision
                    else {}
                ),
            )
        except Exception:
            raise ModelError("Azure Foundry returned invalid JSON response") from None

        if not isinstance(resp_dict, dict):
            raise ModelError("Azure Foundry response is not a JSON object") from None

        # Observed model verification
        observed_model = resp_dict.get("model")
        if not isinstance(observed_model, str) or not observed_model.strip():
            raise ModelError(
                "Azure Foundry response missing model identifier"
            ) from None
        observed_model = observed_model.strip()
        if observed_model.lower() == "unknown":
            raise ModelError("Azure Foundry response reported unknown model") from None
        if observed_model != self.model:
            raise ModelError(
                f"Azure Foundry model mismatch: requested {self.model!r}"
            ) from None

        # Choice verification
        choices = resp_dict.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ModelError(
                "Azure Foundry response choices must be a list containing exactly one choice"
            ) from None
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ModelError("Azure Foundry choice must be an object") from None

        finish_reason = choice.get("finish_reason")
        if finish_reason != "stop":
            raise ModelError(
                "Azure Foundry response finish_reason is not 'stop'"
            ) from None

        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelError("Azure Foundry choice message must be an object") from None

        if set(message.keys()) - SUPPORTED_MESSAGE_KEYS:
            raise ModelError(
                "Azure Foundry message contains unsupported fields"
            ) from None

        if message.get("refusal"):
            raise ModelError("Azure Foundry returned model refusal") from None

        if message.get("function_call"):
            raise ModelError(
                "Azure Foundry returned unexpected function call; function calls are forbidden"
            ) from None

        if bool(message.get("tool_calls")):
            raise ModelError(
                "Azure Foundry returned unexpected tool calls; tool calls are forbidden"
            ) from None

        content_str = message.get("content")
        if not isinstance(content_str, str):
            raise ModelError("Azure Foundry message content must be a string") from None

        # Content JSON parsing (strict: rejects non-finite numbers and duplicate keys)
        try:
            data = json.loads(
                content_str,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicates,
            )
        except Exception:
            raise ModelError(
                "Azure Foundry message content is not valid JSON"
            ) from None

        if not isinstance(data, dict):
            raise ModelError(
                "Azure Foundry structured data must be a JSON object"
            ) from None

        # Local jsonschema validation
        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as exc:
            path_str = _format_schema_path(exc.path, schema)
            raise ModelError(
                f"Azure Foundry output does not conform to requested schema at {path_str}"
            ) from None
        except Exception:
            if vision:
                raise ModelError(
                    "Azure Foundry vision response schema could not be resolved"
                ) from None
            raise

        # Token usage validation
        usage_raw = resp_dict.get("usage")
        if not isinstance(usage_raw, dict):
            raise ModelError("Azure Foundry response missing usage block") from None

        prompt_tokens = usage_raw.get("prompt_tokens")
        completion_tokens = usage_raw.get("completion_tokens")
        if (
            type(prompt_tokens) is not int
            or isinstance(prompt_tokens, bool)
            or prompt_tokens < 0
        ):
            raise ModelError(
                "Azure Foundry prompt_tokens must be non-negative integer"
            ) from None
        if (
            type(completion_tokens) is not int
            or isinstance(completion_tokens, bool)
            or completion_tokens < 0
        ):
            raise ModelError(
                "Azure Foundry completion_tokens must be non-negative integer"
            ) from None

        prompt_details = usage_raw.get("prompt_tokens_details") or {}
        cached_tokens = (
            prompt_details.get("cached_tokens", 0)
            if isinstance(prompt_details, dict)
            else 0
        )
        if (
            type(cached_tokens) is not int
            or isinstance(cached_tokens, bool)
            or cached_tokens < 0
            or cached_tokens > prompt_tokens
        ):
            raise ModelError(
                "Azure Foundry cached_tokens must be non-negative integer <= prompt_tokens"
            ) from None

        completion_details = usage_raw.get("completion_tokens_details") or {}
        reasoning_tokens = (
            completion_details.get("reasoning_tokens", 0)
            if isinstance(completion_details, dict)
            else 0
        )
        if (
            type(reasoning_tokens) is not int
            or isinstance(reasoning_tokens, bool)
            or reasoning_tokens < 0
            or reasoning_tokens > completion_tokens
        ):
            raise ModelError(
                "Azure Foundry reasoning_tokens must be non-negative integer <= completion_tokens"
            ) from None

        total_tokens = usage_raw.get("total_tokens")
        if total_tokens is not None:
            if (
                type(total_tokens) is not int
                or isinstance(total_tokens, bool)
                or total_tokens != prompt_tokens + completion_tokens
            ):
                raise ModelError(
                    "Azure Foundry total_tokens does not match prompt_tokens + completion_tokens"
                ) from None

        usage = Usage(
            model=observed_model,
            input_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            output_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            calls=1,
        )

        hostname = urllib.parse.urlsplit(self.base_url).hostname or ""
        request_id = _sanitize_request_id(resp_dict.get("id"))

        # Reasoning content blob is discarded immediately and not retained
        meta: dict[str, Any] = {
            "requested_model": self.model,
            "deployment": self.model,
            "model": observed_model,
            "model_family": "meta",
            "provider": "azure",
            "endpoint_host": hostname,
            "subscription_id": self.subscription_id,
            "reasoning_effort": self.effort,
            "request_id": request_id,
            "timing_s": elapsed,
        }
        if "cost" in resp_dict or "cost" in usage_raw:
            meta["_raw_cost"] = resp_dict.get("cost") or usage_raw.get("cost")

        return Completion(data=data, usage=usage, meta=meta)

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        """Call Azure Foundry chat completions and return validated Completion.

        Raises ModelError if authentication fails, network fails, response
        violates schema, finish reason is not 'stop', or model mismatch occurs.
        """
        messages = [{"role": "user", "content": prompt}]
        completion = self._call_endpoint(messages=messages, schema=schema)
        completion.meta.pop("_raw_cost", None)
        return completion

    def complete_with_image(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        image_path: str | Path,
    ) -> Completion:
        """Call Azure Foundry with an attached image and return validated Completion.

        Spark on Azure remains text-only; an image call on muse-spark-1.3 refuses
        BEFORE token acquisition or any network dispatch.
        """
        if not self.capabilities.supports_image:
            raise ModelError(
                f"Azure Foundry model {self.model!r} does not support image modality"
            )

        schema = structured_image_schema(schema)
        payload_bytes, media_type, width, height = read_validated_image(
            image_path, self.capabilities
        )
        digest = hashlib.sha256(payload_bytes).hexdigest()
        b64_img = base64.b64encode(payload_bytes).decode("ascii")
        data_url = f"data:{media_type};base64,{b64_img}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ]

        completion = self._call_endpoint(messages=messages, schema=schema, vision=True)

        # Attach media provenance without raw base64 pixels
        completion.meta["media"] = {
            "digest": f"sha256:{digest}",
            "media_type": media_type,
            "size_bytes": len(payload_bytes),
            "width": width,
            "height": height,
        }

        # Cost: explicitly unavailable with reason unless actual cost present in response
        raw_cost = completion.meta.pop("_raw_cost", None)
        completion.meta["cost"] = monetary_cost(raw_cost, source="azure")
        completion.meta["adapter"] = "azure_foundry"
        completion.meta["evidence_kind"] = "provider_reported"
        completion.meta["schema_validation"] = {
            "local": "full",
            "wire_unsupported_keywords": ["uniqueItems"],
        }

        # Declared protocol specification distinguished from live validation
        completion.meta["protocol"] = {
            "modality": "text+image_url",
            "format": "openai_v1_chat_completions",
        }
        return completion
