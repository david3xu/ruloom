"""A local OpenAI-compatible structured-model adapter (Muse Glimmer).

The adapter POSTs ``{base_url}/v1/chat/completions`` with the configured
model name and parses the OpenAI-format reply (``choices[0].message``
content plus the ``usage`` block) into a :class:`Completion` whose data
conforms to the requested schema. Anything that stops that contract -- a
refused connection, a timeout, a non-2xx status, or a non-conforming
answer -- surfaces as :class:`ModelError`, the same family the hosted
``muse exec`` adapter raises.

Standard library only (``urllib``): no hosted call, no contributor mode,
no API key. The ``model`` name configured here is sent to the server, but
the adapter RECORDS the model id the server returns (``Usage.model`` and
``meta["model"]``), so run evidence always names what actually answered.

Text only. The Glimmer image weights (``mmproj``) are not present on the
provisioned machine, so this adapter never attaches images; the HyLogger
vision critic seat stays with the Spark multimodal adapter from Loop 8.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .base import Completion, ModelError, Usage

#: Default base URL of the local ``llama-server`` instance.
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: Default model name sent to the server (the server-reported id is what
#: gets recorded; see :meth:`GlimmerLocalModel.complete`).
DEFAULT_MODEL = "muse-glimmer-30B"

#: Cap on a single critique answer. Glimmer is a reasoning model: it
#: spends completion budget thinking in ``reasoning_content`` before the
#: final JSON lands in ``message.content``, so the budget must cover both
#: the thinking and the short advisory note.
MAX_TOKENS = 4096

#: Minimum ``llama-server -c`` context for the text-critique seat. The
#: live-measured critique prompt on PDD446 is ~8.4k tokens; the server
#: must also fit the ``MAX_TOKENS`` completion plus reasoning headroom.
#: The documented startup command (README, demo-script) uses this value.
#: A smaller context fails loudly with an actionable ``ModelError``
#: (see :meth:`GlimmerLocalModel._post`), never a silent skip.
RECOMMENDED_CONTEXT_TOKENS = 32768


def _check_conformance(data: Any, schema: Mapping[str, Any]) -> None:
    """Reject ``data`` that cannot satisfy a simple object ``schema``.

    Only the shapes this project requests are enforced: the answer must
    be an object, every ``required`` key must be present, and present
    required values must match their declared primitive ``type``
    (``string``, ``array``, ``object``, ``integer``, ``number``,
    ``boolean``). Anything else raises :class:`ModelError`.
    """
    if not isinstance(data, dict):
        raise ModelError(
            f"local model answer is not a JSON object: {type(data).__name__}"
        )
    required = schema.get("required", []) if isinstance(schema, Mapping) else []
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    if not isinstance(required, list) or not isinstance(properties, Mapping):
        return
    kinds = {
        "string": str,
        "array": list,
        "object": dict,
        "boolean": bool,
    }
    for key in required:
        if not isinstance(key, str) or key not in data:
            raise ModelError(f"local model answer is missing required key {key!r}")
        spec = properties.get(key, {})
        want = spec.get("type") if isinstance(spec, Mapping) else None
        if want in kinds and not isinstance(data[key], kinds[want]):
            raise ModelError(
                f"local model answer key {key!r} is not {want}: "
                f"{type(data[key]).__name__}"
            )
        if want == "integer" and (
            not isinstance(data[key], int) or isinstance(data[key], bool)
        ):
            raise ModelError(f"local model answer key {key!r} is not integer")
        if want == "number" and (
            not isinstance(data[key], (int, float)) or isinstance(data[key], bool)
        ):
            raise ModelError(f"local model answer key {key!r} is not number")


def _strip_fences(text: str) -> str:
    """Remove one optional markdown code fence around a JSON answer."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


@dataclass
class GlimmerLocalModel:
    """A :class:`StructuredModel` backed by a local OpenAI-compatible server.

    It POSTs ``{base_url}/v1/chat/completions`` (default
    ``http://127.0.0.1:8080``) with ``urllib`` only.
    """

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 1200.0
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def endpoint(self) -> str:
        """The chat-completions URL this adapter POSTs to."""
        return self.base_url.rstrip("/") + "/v1/chat/completions"

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST ``payload`` and return the decoded response object."""
        body = json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint(),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                **dict(self.extra_headers),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[-2000:]
            except Exception:  # noqa: BLE001 -- best-effort error detail
                detail = ""
            lowered = detail.lower()
            if exc.code == 400 and ("exceed" in lowered and "context" in lowered):
                raise ModelError(
                    "local model prompt exceeds the server context "
                    f"window: {detail} Restart llama-server with a "
                    "larger context, e.g. "
                    f"-c {RECOMMENDED_CONTEXT_TOKENS} (see README), "
                    "so prompt + reasoning + completion fit."
                ) from exc
            raise ModelError(
                f"local model server returned HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ModelError(
                f"local model server unreachable at {self.endpoint()}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise ModelError(
                f"local model call timed out after {self.timeout_s}s"
            ) from exc
        except OSError as exc:
            raise ModelError(f"local model transport failed: {exc}") from exc
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelError(f"local model server emitted invalid JSON: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise ModelError("local model server emitted a non-object reply")
        return decoded

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        """Run ``prompt`` under ``schema`` against the local server."""
        schema_dict = dict(schema)
        reply = self._post(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": MAX_TOKENS,
                "response_format": {"type": "json_object"},
            }
        )
        try:
            choices = reply["choices"]
            content = choices[0]["message"]["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(
                "local model reply has no choices[0].message.content"
            ) from exc
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, Mapping)
            )
        if not isinstance(content, str) or not content.strip():
            raise ModelError("local model reply carried no text content")
        try:
            data = json.loads(_strip_fences(content))
        except json.JSONDecodeError as exc:
            raise ModelError(f"local model answer is not valid JSON: {exc}") from exc
        _check_conformance(data, schema_dict)
        usage_block = reply.get("usage")
        if not isinstance(usage_block, Mapping):
            usage_block = {}
        reported = reply.get("model")
        recorded = reported if isinstance(reported, str) and reported else self.model

        def _count(key: str) -> int:
            value = usage_block.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int):
                return 0
            return value

        usage = Usage(
            model=recorded,
            input_tokens=_count("prompt_tokens"),
            output_tokens=_count("completion_tokens"),
            calls=1,
        )
        meta = {
            "model": recorded,
            "requested_model": self.model,
            "base_url": self.base_url,
            "output_schema": schema_dict,
        }
        return Completion(data=data, usage=usage, meta=meta)
