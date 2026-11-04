"""Provider schema projection keeps the original local acceptance constraints."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from models.azure_foundry import AzureFoundryModel
from models.base import ModelError, image_wire_schema
from models.meta_cli import MuseCLIModel
from tests.models.test_azure_foundry import (
    VALID_SUB,
    VALID_URL,
    MockHTTPResponse,
    _make_azure_response,
    _make_test_png,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "citations": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    },
    "required": ["citations"],
    "additionalProperties": False,
}


def test_projection_preserves_property_names_and_original_constraints():
    schema = {
        "type": "object",
        "properties": {
            "uniqueItems": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            }
        },
        "required": ["uniqueItems"],
        "additionalProperties": False,
    }
    wire = image_wire_schema(schema)
    assert "uniqueItems" in wire["properties"]
    assert "uniqueItems" not in wire["properties"]["uniqueItems"]
    assert schema["properties"]["uniqueItems"]["uniqueItems"] is True


@pytest.mark.parametrize("adapter", ["meta", "azure"])
def test_wire_omission_does_not_allow_duplicate_values(tmp_path, monkeypatch, adapter):
    image = tmp_path / "generated.png"
    image.write_bytes(_make_test_png())
    data = {"citations": ["same", "same"]}
    captured = []
    if adapter == "meta":

        def run(argv, **kwargs):
            wire = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
            captured.append(wire)
            usage = {
                "payload_type": "run.usage",
                "payload": {
                    "models": [
                        {
                            "model": "muse-spark-1.3",
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "calls": 1,
                        }
                    ]
                },
            }
            terminal = {
                "payload_type": "run.terminal.completed",
                "payload": {"terminal": "completed", "text": json.dumps(data)},
            }
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(usage) + "\n" + json.dumps(terminal),
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", run)
        model = MuseCLIModel()
        with pytest.raises(ModelError, match="violates requested schema"):
            model.complete_with_image("review", SCHEMA, image)
    else:

        def open_response(self, req, **kwargs):
            captured.append(
                json.loads(req.data)["response_format"]["json_schema"]["schema"]
            )
            return MockHTTPResponse(
                _make_azure_response(model="muse-glimmer-30b", content=json.dumps(data))
            )

        model = AzureFoundryModel(
            base_url=VALID_URL,
            subscription_id=VALID_SUB,
            model="muse-glimmer-30b",
            token_provider=lambda s: "fixture-token",
        )
        with patch("urllib.request.OpenerDirector.open", open_response):
            with pytest.raises(ModelError, match="does not conform"):
                model.complete_with_image("review", SCHEMA, image)
    assert "uniqueItems" not in captured[0]["properties"]["citations"]
    assert SCHEMA["properties"]["citations"]["uniqueItems"] is True
