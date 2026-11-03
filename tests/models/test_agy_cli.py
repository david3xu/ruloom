"""AGY transport contract; all calls here are fixtures, never live models."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from models.agy_cli import AgyCLIModel
from models.base import ModelError
from models.selection import family_default, validate_selection

SCHEMA = {
    "type": "object",
    "properties": {"seed": {"type": "integer"}},
    "required": ["seed"],
    "additionalProperties": False,
}


def reply(
    *,
    model="claude-opus-5-5-high",
    status="SUCCESS",
    data=None,
    tool="finish",
    usage=None,
):
    events = [
        {
            "event": "init",
            "init": {
                "model": model,
                "tools": ["finish", "run_command"],
                "permission_mode": "always-ask",
            },
        },
        {
            "event": "step_update",
            "step_update": {"step_type": "tool", "tool_name": tool},
        },
        {
            "event": "result",
            "result": {
                "status": status,
                "structured_output": data if data is not None else {"seed": 42},
                "usage": usage
                if usage is not None
                else {
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "cache_read_tokens": 7,
                    "thinking_tokens": 3,
                },
            },
        },
    ]
    return subprocess.CompletedProcess(
        [], 0, "\n".join(json.dumps(x) for x in events), ""
    )


def test_parse_uses_terminal_counts_once_and_cli_identity():
    client = AgyCLIModel()
    answer = client._parse(reply(), SCHEMA)
    assert answer.data == {"seed": 42}
    assert answer.usage.input_tokens == 12
    assert answer.usage.cached_tokens == 7
    assert answer.usage.reasoning_tokens == 3
    assert answer.usage.calls == 1
    assert answer.meta["model_family"] == "claude"
    assert (
        "backend weights not independently attested" in answer.meta["identity_evidence"]
    )
    assert client.last_invocation["observed_permissions"]["tools_exposed"] == [
        "finish",
        "run_command",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "gemini-4-argon-high"},
        {"status": "ERROR"},
        {"data": {"seed": True}},
        {"data": {"seed": 42, "extra": 0}},
        {"tool": "run_command"},
        {"tool": "invoke_subagent"},
        {"usage": {"input_tokens": -1}},
        {"usage": {"output_tokens": True}},
    ],
)
def test_rejects_failure_fallback_tools_and_bad_schema(changes):
    with pytest.raises(ModelError):
        AgyCLIModel()._parse(reply(**changes), SCHEMA)


@pytest.mark.parametrize(
    "output",
    [
        "garbage",
        "[]",
        "{}",
        '{"event":"result","result":null}',
        reply().stdout + "\n" + reply().stdout,
    ],
)
def test_malformed_or_ambiguous_stream_refuses(output):
    with pytest.raises(ModelError):
        AgyCLIModel()._parse(subprocess.CompletedProcess([], 0, output, ""), SCHEMA)


def test_missing_reported_identity_stays_unknown():
    assert AgyCLIModel()._parse(reply(model=None), SCHEMA).usage.model == "unknown"


def test_subprocess_contract_and_receipt(monkeypatch):
    observed = {}
    monkeypatch.setenv("PRIVATE_TOKEN", "must-not-leak")

    def run(argv, **kwargs):
        observed.update(kwargs)
        assert "PRIVATE_TOKEN" not in kwargs["env"]
        assert Path(kwargs["cwd"]).is_dir()
        assert (
            json.loads(Path(argv[argv.index("--json-schema") + 1]).read_text())
            == SCHEMA
        )
        assert argv[argv.index("--effort") + 1] == "high"
        assert argv[argv.index("--model") + 1] == "claude-opus-5-5-high"
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["permissions"] == {"allow": [], "deny": ["*"]}
        assert not any(
            x in argv
            for x in ["--continue", "--conversation", "--dangerously-skip-permissions"]
        )
        return reply()

    monkeypatch.setattr("models.agy_cli.subprocess.run", run)
    answer = AgyCLIModel().complete("--tool literal prompt", SCHEMA)
    assert not Path(observed["cwd"]).exists()
    assert (
        answer.meta["invocation"]["observed_permissions"]["confinement"]
        == "not-established"
    )
    assert answer.meta["invocation"]["tools_used"] == ["finish"]


def test_nonzero_exit_never_passes():
    with pytest.raises(ModelError, match="exited 9"):
        AgyCLIModel()._parse(
            subprocess.CompletedProcess([], 9, reply().stdout, "failure"), SCHEMA
        )


@pytest.mark.parametrize(
    "family,effort,expected",
    [
        ("claude", "high", "claude-opus-5-5-high"),
        ("gemini", "low", "gemini-4-argon-low"),
        ("gpt", "medium", "gpt-oss-120b-medium"),
    ],
)
def test_family_defaults(family, effort, expected):
    assert family_default("agy_cli", family, effort) == expected
    validate_selection("agy_cli", expected, family, effort)


@pytest.mark.parametrize(
    "adapter,model,family,effort",
    [
        ("agy_cli", "claude-opus-5-5-high", "gemini", "high"),
        ("agy_cli", "claude-opus-5-5-high", "claude", "low"),
        ("agy_cli", "invented", "claude", "high"),
        ("meta_cli", "muse-spark-1.3", "claude", "high"),
    ],
)
def test_selection_conflicts_fail(adapter, model, family, effort):
    with pytest.raises(ValueError):
        validate_selection(adapter, model, family, effort)


def test_model_family_config_and_precedence():
    from engine.cli import default_client, resolve_role_bindings
    from engine.core.manifest import load_manifest
    from profiles import manifest_path

    manifest = load_manifest(manifest_path("tabular"))
    config, sources = resolve_role_bindings(
        manifest,
        cli_adapters=["planner=agy_cli"],
        cli_families=["planner=claude"],
        cli_models=["planner=claude-opus-5-5-high"],
        cli_efforts=["planner=high"],
        env={"RULOOM_ROLE_PLANNER_FAMILY": "gemini"},
    )
    assert config["planner"]["family"] == "claude"
    assert config["planner"]["provider"] == "agy"
    assert sources["planner"]["family"] == "cli"
    assert isinstance(default_client("planner", config["planner"]), AgyCLIModel)
    config, _ = resolve_role_bindings(
        manifest,
        cli_adapters=["planner=agy_cli"],
        cli_families=["planner=gemini"],
        cli_efforts=["planner=high"],
        env={},
    )
    assert config["planner"]["model"] == "gemini-4-argon-high"


def test_agy_refused_offline_local_only_and_open_weight():
    from engine.cli import default_client
    from engine.core.model_policy import enforce_model_policy

    config = {"adapter": "agy_cli", "model": "claude-opus-5-5-high"}
    for policy in ["offline", "local-only"]:
        with pytest.raises(ValueError):
            enforce_model_policy(policy, config)
    with pytest.raises(ValueError):
        default_client("planner", {**config, "requires": {"open_weight": True}})
