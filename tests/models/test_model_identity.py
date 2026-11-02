"""Model identity integrity: requested vs observed, unknown, multi-model.

The hosted ``muse exec`` adapter and the local Glimmer adapter must
preserve the model identity the transport reports (observed) separately
from the name they were configured with (requested), record ``unknown``
when the transport supplies no usable name, and keep every per-model
usage entry. The runner records both sides plus the per-model breakdown
on every ``model_invocation`` event. A mismatch never refuses; both
sides are recorded. All subprocess and network use here is stubbed --
no live model calls.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from models.base import UNKNOWN_MODEL, Completion, Usage
from models.glimmer import GlimmerLocalModel
from models.meta_cli import MuseCLIModel

SCHEMA: dict[str, Any] = {"type": "object"}


def _terminal_event(text: str) -> str:
    return json.dumps(
        {
            "payload_type": "run.terminal.completed",
            "payload": {
                "kind": "run_terminal",
                "terminal": "completed",
                "text": text,
            },
        }
    )


def _usage_event(models: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {"kind": "run_usage", "models": models},
        }
    )


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _stub_run(monkeypatch: Any, proc: _Proc) -> None:
    def _fake_run(argv: Any, **kwargs: Any) -> _Proc:
        return proc

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _parse(model: MuseCLIModel, models: list[dict[str, Any]] | None) -> Completion:
    events = []
    if models is not None:
        events.append(_usage_event(models))
    events.append(_terminal_event(json.dumps({"seed": 1})))
    return model._parse(_Proc("\n".join(events)), dict(SCHEMA))


# --- hosted adapter: mismatch / missing / multi-model -----------------------


def test_mismatch_preserves_observed_and_requested() -> None:
    model = MuseCLIModel(model="requested-A")
    completion = _parse(
        model,
        [
            {
                "model": "served-B",
                "input_tokens": 10,
                "output_tokens": 5,
                "calls": 1,
            }
        ],
    )
    assert completion.usage.model == "served-B"
    assert completion.meta["model"] == "served-B"
    assert completion.meta["requested_model"] == "requested-A"
    assert completion.meta["models"] == [
        {
            "model": "served-B",
            "input_tokens": 10,
            "cached_tokens": 0,
            "output_tokens": 5,
            "reasoning_tokens": 0,
            "calls": 1,
        }
    ]
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (10, 5)


def test_missing_usage_records_unknown() -> None:
    model = MuseCLIModel(model="requested-A")
    completion = _parse(model, None)
    assert completion.usage.model == UNKNOWN_MODEL
    assert completion.usage.model == "unknown"
    assert completion.meta["model"] == "unknown"
    assert completion.meta["requested_model"] == "requested-A"
    assert completion.meta["models"] == []


def test_unusable_names_record_unknown() -> None:
    model = MuseCLIModel(model="requested-A")
    completion = _parse(
        model,
        [
            {"input_tokens": 4, "output_tokens": 2, "calls": 1},
            {"model": "   ", "input_tokens": 1, "output_tokens": 1},
            {"model": 7, "input_tokens": 1, "output_tokens": 1},
        ],
    )
    assert completion.usage.model == "unknown"
    assert [entry["model"] for entry in completion.meta["models"]] == [
        "unknown",
        "unknown",
        "unknown",
    ]
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (6, 4)


def test_multi_model_aggregates_with_per_model_breakdown() -> None:
    model = MuseCLIModel(model="requested-A")
    completion = _parse(
        model,
        [
            {"model": "model-C", "input_tokens": 3, "output_tokens": 1, "calls": 1},
            {"model": "model-B", "input_tokens": 10, "output_tokens": 5, "calls": 1},
        ],
    )
    assert completion.usage.model == "multiple:model-B+model-C"
    assert completion.meta["model"] == "multiple:model-B+model-C"
    assert completion.meta["requested_model"] == "requested-A"
    assert completion.meta["models"] == [
        {
            "model": "model-C",
            "input_tokens": 3,
            "cached_tokens": 0,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "calls": 1,
        },
        {
            "model": "model-B",
            "input_tokens": 10,
            "cached_tokens": 0,
            "output_tokens": 5,
            "reasoning_tokens": 0,
            "calls": 1,
        },
    ]
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (13, 6)


def test_totals_equal_sum_of_per_model_entries() -> None:
    model = MuseCLIModel(model="requested-A")
    entries = [
        {
            "model": "model-B",
            "input_tokens": 8,
            "cached_tokens": 2,
            "output_tokens": 4,
            "reasoning_tokens": 3,
            "calls": 1,
        },
        {"model": "model-C", "input_tokens": 1, "output_tokens": 1},
    ]
    completion = _parse(model, entries)
    for key in (
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "calls",
    ):
        assert getattr(completion.usage, key) == sum(
            entry[key] for entry in completion.meta["models"]
        )


def test_complete_records_requested_and_returned(monkeypatch: Any) -> None:
    proc = _Proc(
        "\n".join(
            [
                _usage_event(
                    [{"model": "served-B", "input_tokens": 2, "output_tokens": 1}]
                ),
                _terminal_event(json.dumps({"seed": 1})),
            ]
        )
    )
    _stub_run(monkeypatch, proc)
    model = MuseCLIModel(model="requested-A")
    completion = model.complete("make a plan", {"type": "object"})
    assert model.last_invocation["model"] == "requested-A"
    assert model.last_invocation["returned_model"] == "served-B"
    assert completion.meta["invocation"] == model.last_invocation


def test_complete_without_usage_records_unknown_returned(monkeypatch: Any) -> None:
    _stub_run(monkeypatch, _Proc(_terminal_event(json.dumps({"seed": 1}))))
    model = MuseCLIModel(model="requested-A")
    completion = model.complete("make a plan", {"type": "object"})
    assert model.last_invocation["model"] == "requested-A"
    assert model.last_invocation["returned_model"] == "unknown"
    assert completion.usage.model == "unknown"


# --- local adapter: missing identity ----------------------------------------


class _Stub:
    """A one-route OpenAI-compatible stub recording what it was sent."""

    def __init__(self, body: Any) -> None:
        class Route(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
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


def _glimmer_reply(note: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(dict(note))},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    reply.update(extra)
    return reply


GLIMMER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "critique": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["critique", "concerns"],
    "additionalProperties": False,
}

_NOTE = {"critique": "plausible bands", "concerns": ["thin tails"]}


def test_glimmer_missing_model_records_unknown() -> None:
    reply = _glimmer_reply(_NOTE)
    assert "model" not in reply
    with _Stub(reply) as stub:
        client = GlimmerLocalModel(model="requested-G", base_url=stub.url)
        completion = client.complete("Prompt.", GLIMMER_SCHEMA)
    assert completion.usage.model == "unknown"
    assert completion.meta["model"] == "unknown"
    assert completion.meta["requested_model"] == "requested-G"


def test_glimmer_blank_model_records_unknown() -> None:
    with _Stub(_glimmer_reply(_NOTE, model="  ")) as stub:
        client = GlimmerLocalModel(model="requested-G", base_url=stub.url)
        completion = client.complete("Prompt.", GLIMMER_SCHEMA)
    assert completion.usage.model == "unknown"
    assert completion.meta["requested_model"] == "requested-G"


# --- runner: invocation record keeps both sides ------------------------------


def _runner(tmp_path: Path) -> Any:
    from engine.core.manifest import ENGINE_API, validate_manifest
    from engine.core.runner import _Runner

    manifest = validate_manifest(
        {
            "id": "fake",
            "version": 1,
            "engine_api": ENGINE_API,
            "source": {"reader": "fake.reader"},
            "capabilities": {
                "decide": {"impl": "fake.decide", "calls_model": "planner"},
            },
            "graph": [{"step": "decide"}, {"gate": "review"}],
            "checks": [
                {
                    "id": "shape_ok",
                    "kind": "constraint",
                    "threshold": {"source": "literal"},
                }
            ],
            "models": {"roles": {"planner": {"model": "requested-A"}}},
            "limits": {},
        }
    )
    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    return _Runner(
        manifest=manifest,
        registry={"decide": (lambda ctx: None)},
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: None,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "",
    )


def test_runner_records_hosted_requested_returned_and_models(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)

    class HostedStyleClient:
        last_invocation = {
            "argv": ["muse", "exec"],
            "cwd": str(tmp_path),
            "env_keys": ["HOME", "PATH"],
            "contributor": True,
            "model": "requested-A",
            "returned_model": "served-B",
        }

    completion = Completion(
        data={"seed": 1},
        usage=Usage(model="served-B", input_tokens=2, output_tokens=1, calls=1),
        meta={
            "model": "served-B",
            "requested_model": "requested-A",
            "models": [
                {
                    "model": "served-B",
                    "input_tokens": 2,
                    "cached_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_tokens": 0,
                    "calls": 1,
                }
            ],
        },
    )
    runner.note_model_invocation("planner", HostedStyleClient(), completion)
    details = runner.log.events[-1]["details"]
    assert details["role"] == "planner"
    assert details["invocation"]["model"] == "requested-A"
    assert details["invocation"]["returned_model"] == "served-B"
    assert details["invocation"]["models"] == completion.meta["models"]


def test_runner_records_generic_client_requested_and_returned(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)

    class GenericClient:
        model = "asked-Y"

        def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
            raise AssertionError("must not be called")

    completion = Completion(
        data={"seed": 1},
        usage=Usage(model="served-X", input_tokens=1, output_tokens=1, calls=1),
        meta={"model": "served-X", "requested_model": "asked-Y"},
    )
    runner.note_model_invocation("planner", GenericClient(), completion)
    details = runner.log.events[-1]["details"]
    assert details["model"] == "asked-Y"
    assert details["returned_model"] == "served-X"


def test_runner_records_unknown_when_transport_supplies_none(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)

    class PlainClient:
        def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
            raise AssertionError("must not be called")

    completion = Completion(
        data={"seed": 1},
        usage=Usage(model="", input_tokens=1, output_tokens=1, calls=1),
        meta={},
    )
    runner.note_model_invocation("planner", PlainClient(), completion)
    details = runner.log.events[-1]["details"]
    assert details["returned_model"] == "unknown"
