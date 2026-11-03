"""Literal-loopback fixture transport cannot follow redirects or use proxies."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from models.base import ModelError
from models.glimmer import GlimmerLocalModel
from tests.test_glimmer import SCHEMA, _reply, _Stub


def test_local_only_transport_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    with _Stub(
        lambda payload: (200, _reply({"critique": "fixture", "concerns": []}))
    ) as stub:
        client = GlimmerLocalModel(base_url=stub.url, local_only=True)
        assert client.complete("fixture", SCHEMA).data["critique"] == "fixture"
        assert len(stub.seen) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_local_only_transport_does_not_follow_redirect(status):
    reached = []

    class Route(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.send_response(status)
            self.send_header("Location", "http://127.0.0.1:1/forbidden")
            self.end_headers()

        def do_GET(self):
            reached.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(_reply({"critique": "wrong", "concerns": []})).encode()
            )

    server = HTTPServer(("127.0.0.1", 0), Route)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = GlimmerLocalModel(
            base_url=f"http://127.0.0.1:{server.server_port}", local_only=True
        )
        with pytest.raises(ModelError, match=f"HTTP {status}"):
            client.complete("fixture", SCHEMA)
        assert reached == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_local_only_direct_adapter_refuses_remote_before_open(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *args, **kwargs: pytest.fail("no remote calls")
    )
    with pytest.raises(ModelError, match="literal loopback"):
        GlimmerLocalModel(base_url="https://example.com", local_only=True).complete(
            "fixture", SCHEMA
        )
