"""Tests for the local Glimmer adapter (no model server needed).

A stub OpenAI-compatible endpoint runs on a loopback port in a helper
thread, so every transport path — success, refusal, timeout, HTTP error,
malformed and non-conforming answers — is exercised without touching any
real model or the network beyond localhost.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from models.base import ModelError
from models.glimmer import GlimmerLocalModel

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "critique": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["critique", "concerns"],
    "additionalProperties": False,
}


def _reply(
    data: dict[str, Any],
    *,
    model: str = "server-reported-glimmer",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(data)},
                "finish_reason": "stop",
            }
        ],
        "usage": (
            usage
            if usage is not None
            else {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        ),
    }


class _Stub:
    """A one-route OpenAI-compatible stub recording what it was sent."""

    def __init__(self, handler: Callable[[dict[str, Any]], tuple[int, Any]]):
        self._handler = handler
        self.seen: list[dict[str, Any]] = []
        self.paths: list[str] = []

        stub = self

        class Route(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                stub.seen.append(payload)
                stub.paths.append(self.path)
                status, body = stub._handler(payload)
                raw = (
                    body.encode("utf-8")
                    if isinstance(body, str)
                    else json.dumps(body).encode("utf-8")
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = HTTPServer(("127.0.0.1", 0), Route)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> _Stub:
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._server.shutdown()
        self._thread.join()
        self._server.server_close()


def test_success_records_server_model_id_and_usage() -> None:
    note = {"critique": "plausible bands", "concerns": ["thin tails"]}
    with _Stub(lambda payload: (200, _reply(note))) as stub:
        client = GlimmerLocalModel(model="configured-name", base_url=stub.url)
        completion = client.complete("Prompt.", SCHEMA)
    assert stub.paths == ["/v1/chat/completions"]
    assert stub.seen[0]["model"] == "configured-name"
    assert completion.data == note
    assert completion.usage.model == "server-reported-glimmer"
    assert completion.usage.input_tokens == 11
    assert completion.usage.output_tokens == 7
    assert completion.usage.calls == 1
    assert completion.meta["model"] == "server-reported-glimmer"
    assert completion.meta["requested_model"] == "configured-name"


def test_connection_refused_raises_model_error() -> None:
    client = GlimmerLocalModel(base_url="http://127.0.0.1:1")
    with pytest.raises(ModelError):
        client.complete("Prompt.", SCHEMA)


def test_http_error_raises_model_error() -> None:
    with _Stub(lambda payload: (500, {"error": "boom"})) as stub:
        client = GlimmerLocalModel(base_url=stub.url, timeout_s=30.0)
        with pytest.raises(ModelError):
            client.complete("Prompt.", SCHEMA)


def test_context_overflow_raises_actionable_model_error() -> None:
    """A 400 context-window refusal must name the remedy (GP-01 live find).

    The documented ``llama-server`` startup once used a context smaller
    than the real critique prompt (~8.4k tokens on PDD446); the adapter
    must fail loudly with the ``-c`` remedy, never a bare HTTP status.
    """
    body = {
        "error": {
            "code": 400,
            "message": (
                "request (8396 tokens) exceeds the available context "
                "size (8192 tokens), try increasing it"
            ),
            "type": "exceed_context_size_error",
        }
    }
    with _Stub(lambda payload: (400, body)) as stub:
        client = GlimmerLocalModel(base_url=stub.url, timeout_s=30.0)
        with pytest.raises(ModelError) as excinfo:
            client.complete("Prompt.", SCHEMA)
    message = str(excinfo.value)
    assert "context" in message
    assert "-c" in message


def test_invalid_json_reply_raises_model_error() -> None:
    with _Stub(lambda payload: (200, "not json{{")) as stub:
        client = GlimmerLocalModel(base_url=stub.url, timeout_s=30.0)
        with pytest.raises(ModelError):
            client.complete("Prompt.", SCHEMA)


def test_missing_choices_raises_model_error() -> None:
    with _Stub(lambda payload: (200, {"model": "x", "usage": {}})) as stub:
        client = GlimmerLocalModel(base_url=stub.url, timeout_s=30.0)
        with pytest.raises(ModelError):
            client.complete("Prompt.", SCHEMA)


def test_non_conforming_answer_raises_model_error() -> None:
    bad = {"comment": "wrong shape"}
    with _Stub(lambda payload: (200, _reply(bad))) as stub:
        client = GlimmerLocalModel(base_url=stub.url, timeout_s=30.0)
        with pytest.raises(ModelError):
            client.complete("Prompt.", SCHEMA)


def test_wrong_typed_answer_raises_model_error() -> None:
    bad = {"critique": "ok", "concerns": "not-a-list"}
    with _Stub(lambda payload: (200, _reply(bad))) as stub:
        client = GlimmerLocalModel(base_url=stub.url, timeout_s=30.0)
        with pytest.raises(ModelError):
            client.complete("Prompt.", SCHEMA)


def test_timeout_raises_model_error() -> None:
    import time

    def slow(payload: dict[str, Any]) -> tuple[int, Any]:
        time.sleep(5)
        return 200, _reply({"critique": "late", "concerns": []})

    with _Stub(slow) as stub:
        client = GlimmerLocalModel(base_url=stub.url, timeout_s=0.5)
        with pytest.raises(ModelError):
            client.complete("Prompt.", SCHEMA)


def test_stdlib_transport_only() -> None:
    """The adapter imports stdlib transport plus models.base — nothing else."""
    import ast

    import models.glimmer as module

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            imported.add((node.module or "").split(".")[0])
    imported.discard("")
    # GP-03 hygiene: the UP035 autofix moved `Mapping` to its canonical
    # `collections.abc` home and dropped the unused `socket` import, so
    # the pinned surface trades "socket" for "collections" (still
    # stdlib-only plus models.base; the "no subprocess" pin below the
    # assertion is unchanged).
    stdlib = {
        "collections",
        "json",
        "urllib",
        "dataclasses",
        "typing",
        "__future__",
    }
    assert imported <= (stdlib | {"models"}), imported
    assert "subprocess" not in imported
