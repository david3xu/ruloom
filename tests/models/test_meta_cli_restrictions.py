"""Restricted model invocation: env stripped, cwd pinned, spawn gated, recorded.

The hosted ``muse exec`` adapter must run the child with a pinned
working directory, a minimal allowlisted environment (secrets never
inherited), and an argv that names no peer-dispatch capability; the
spawn gate refuses tool/dispatch flags before anything spawns, and
every call's requested vs observed permissions land in the run record.
The pinned directory is not filesystem confinement (absolute host
paths outside the root stay reachable). All subprocess use here is
stubbed -- no live model calls.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from models.base import Completion, ModelError, Usage
from models.meta_cli import (
    ALLOWED_ENV_KEYS,
    REQUESTED_PERMISSIONS,
    MuseCLIModel,
    enforce_spawn_policy,
    resolve_cwd,
    restricted_env,
)

PROBE_KEY = "RULOOM_PROBE_SECRET"
PROBE_VALUE = "probe-secret-value-that-must-never-leak"


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


class _Proc:
    def __init__(self, stdout: str = "") -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _stub_run(monkeypatch: pytest.MonkeyPatch, proc: _Proc) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def _fake_run(argv: Any, **kwargs: Any) -> _Proc:
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return proc

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return seen


def _answer(seed: int = 1) -> _Proc:
    return _Proc(stdout=_terminal_event(json.dumps({"seed": seed})))


def test_env_strips_probe_secret_and_unlisted_keys() -> None:
    source = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/fake-home",
        PROBE_KEY: PROBE_VALUE,
        "RULOOM_ROLE_PLANNER_MODEL": "sneaky-override",
        "MYSTERY_ROUTING": "peer-dispatch",
    }
    env = restricted_env(source)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/tmp/fake-home"
    assert PROBE_KEY not in env
    assert "RULOOM_ROLE_PLANNER_MODEL" not in env
    assert "MYSTERY_ROUTING" not in env
    assert PROBE_VALUE not in json.dumps(env)


def test_env_never_inherits_secret_like_keys() -> None:
    for key in (
        "RULOOM_PROBE_SECRET",
        "META_API_TOKEN",
        "MY_SECRET_STORE",
        "SERVICE_PASSWORD",
    ):
        env = restricted_env({"PATH": "/usr/bin", key: "shh"})
        assert key not in env, key
        assert "shh" not in json.dumps(env)


def test_env_allowlist_is_minimal_and_documented() -> None:
    assert set(ALLOWED_ENV_KEYS) == {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_MESSAGES",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "SystemRoot",
    }


def test_cwd_pinned_to_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _stub_run(monkeypatch, _answer())
    model = MuseCLIModel(allowed_root=tmp_path)
    model.complete("make a plan", {"type": "object"})
    assert seen["cwd"] == str(tmp_path)
    argv = seen["argv"]
    assert argv[argv.index("--workspace") + 1] == str(tmp_path)


def test_cwd_defaults_to_invoking_directory_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_run(monkeypatch, _answer())
    MuseCLIModel().complete("make a plan", {"type": "object"})
    assert seen["cwd"] == str(Path.cwd())
    assert resolve_cwd(None) == str(Path.cwd())


def test_child_env_is_explicit_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBE_KEY, PROBE_VALUE)
    seen = _stub_run(monkeypatch, _answer())
    MuseCLIModel(allowed_root=tmp_path).complete("make a plan", {"type": "object"})
    child_env = seen["env"]
    assert isinstance(child_env, dict)
    assert PROBE_KEY not in child_env
    assert "RULOOM_ROLE_PLANNER_MODEL" not in child_env
    assert PROBE_VALUE not in json.dumps(child_env)
    assert set(child_env) <= set(ALLOWED_ENV_KEYS)


def test_argv_carries_no_peer_dispatch_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _stub_run(monkeypatch, _answer())
    MuseCLIModel(allowed_root=tmp_path).complete("make a plan", {"type": "object"})
    argv = seen["argv"]
    assert argv[:2] == ["muse", "exec"]
    assert "--json" in argv
    assert argv[argv.index("--contributor-mode") + 1] == "on"
    lowered = [str(part).lower() for part in argv]
    assert not any(
        part.startswith(("--mcp", "--dispatch", "--peer", "--tool"))
        or "delegate" in part
        for part in lowered
    )


def test_effective_permissions_recorded_with_returned_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBE_KEY, PROBE_VALUE)
    seen = _stub_run(monkeypatch, _answer())
    model = MuseCLIModel(
        model="muse-spark-1.3",
        effort="max",
        contributor=True,
        allowed_root=tmp_path,
    )
    completion = model.complete("make a plan", {"type": "object"})
    record = model.last_invocation
    assert record["argv"] == seen["argv"]
    assert record["cwd"] == str(tmp_path)
    assert record["contributor"] is True
    assert record["model"] == "muse-spark-1.3"
    assert record["returned_model"] == completion.usage.model
    assert completion.meta["invocation"] == record
    assert PROBE_KEY not in record["env_keys"]
    assert PROBE_VALUE not in json.dumps(record)


def test_complete_with_image_records_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "synthetic.png"
    image.write_bytes(_make_minimal_test_png())
    usage = json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {
                "models": [
                    {
                        "model": "muse-spark-1.3",
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "calls": 1,
                    }
                ]
            },
        }
    )
    seen = _stub_run(monkeypatch, _Proc(usage + "\n" + _answer().stdout))
    model = MuseCLIModel(allowed_root=tmp_path)
    completion = model.complete_with_image("describe", {"type": "object"}, image)
    staged = Path(seen["argv"][seen["argv"].index("--image") + 1])
    assert seen["cwd"] == str(staged.parent)
    assert seen["cwd"] != str(tmp_path)
    assert "--image" in seen["argv"]
    assert model.last_invocation["cwd"] == str(staged.parent)
    assert completion.meta["invocation"] == model.last_invocation


def test_run_record_stores_effective_invocation_per_call(
    tmp_path: Path,
) -> None:
    """One fake-model tabular run logs its invocation to the run record."""
    import json as _json

    from engine.core.manifest import load_manifest
    from engine.core.runner import EXIT_OK, execute
    from profiles.tabular.capabilities import CAPABILITIES

    repo = Path(__file__).resolve().parents[2]
    manifest = load_manifest(repo / "profiles" / "tabular" / "profile.json")
    source = tmp_path / "sample-tabular.csv"
    source.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (tmp_path / "sample-tabular-source.json").write_text(
        _json.dumps(
            {
                "dataset_name": "invocation-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Invocation fixture.",
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    class RecordingClient:
        """A fake exposing a restricted-style invocation record."""

        def __init__(self) -> None:
            self.last_invocation = {
                "argv": ["muse", "exec", "--json", "prompt"],
                "cwd": str(tmp_path),
                "env_keys": ["HOME", "PATH"],
                "contributor": True,
                "model": "fake",
                "returned_model": "fake",
                "requested_permissions": {
                    "filesystem": "cwd-pinned-only",
                    "tools": "none",
                },
                "observed_permissions": {
                    "filesystem": "cwd-pinned-only",
                    "filesystem_limit": "absolute host paths outside "
                    "the root are not contained",
                    "tools": "none-named-on-argv",
                },
            }

        def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
            return Completion(
                data={"seed": 1, "n_rows": 100},
                usage=Usage(model="fake", input_tokens=1, output_tokens=1),
                meta={"model": "fake"},
            )

    os.environ[PROBE_KEY] = PROBE_VALUE
    try:
        code, result = execute(
            manifest,
            CAPABILITIES,
            source=source,
            max_attempts=1,
            model_factory=lambda role, config: RecordingClient(),
            out=out,
            say=lambda message: None,
            ask=lambda prompt: "accept",
        )
    finally:
        del os.environ[PROBE_KEY]
    assert code == EXIT_OK, result
    run_dir = out / "runs" / str(result["run_id"])
    events = [
        _json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    invocations = [event for event in events if event["stage"] == "model_invocation"]
    assert invocations, "no model_invocation event in the run record"
    details = invocations[0]["details"]
    assert details["role"] == "planner"
    assert details["client"] == "RecordingClient"
    assert details["invocation"]["cwd"] == str(tmp_path)
    assert details["invocation"]["contributor"] is True
    assert details["invocation"]["returned_model"] == "fake"
    assert details["invocation"]["requested_permissions"]["tools"] == "none"
    assert (
        "absolute host paths"
        in details["invocation"]["observed_permissions"]["filesystem_limit"]
    )
    assert PROBE_KEY not in details["invocation"]["env_keys"]
    assert PROBE_VALUE not in _json.dumps(invocations)


def test_requested_policy_names_no_tool_or_peer_capability() -> None:
    """The requested policy grants no tool, peer, or confinement it lacks."""
    assert REQUESTED_PERMISSIONS["tools"] == "none"
    assert REQUESTED_PERMISSIONS["network_peers"] == "none"
    assert REQUESTED_PERMISSIONS["filesystem"] == "cwd-pinned-only"


def test_spawn_gate_refuses_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool-flagged spawn is refused and the transport never starts."""
    seen = _stub_run(monkeypatch, _answer())
    model = MuseCLIModel(allowed_root=tmp_path)
    with pytest.raises(ModelError, match="outside the permission policy"):
        model._run(["muse", "exec", "--json", "--dispatch", "peer", "prompt"])
    assert seen == {}, "refused spawn must not reach the transport"


def test_spawn_gate_passes_real_argv_and_free_text(tmp_path: Path) -> None:
    """The real argv passes the gate; flag-like wording in text does not trip it."""
    model = MuseCLIModel(allowed_root=tmp_path)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    enforce_spawn_policy(model._argv(str(schema_file), "delegate --tool use"))
    enforce_spawn_policy(["muse", "exec", "--contributor-mode", "off", "prompt"])


def test_spawn_gate_passes_flag_like_prompt(tmp_path: Path) -> None:
    """A literal prompt beginning with a denied-looking flag passes.

    P3: the old scan refused any dash-leading string, so a prompt that
    starts ``--tool`` was denied despite the documented free-text
    promise. The structural gate exempts the prompt slot instead.
    """
    model = MuseCLIModel(allowed_root=tmp_path)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    enforce_spawn_policy(
        model._argv(str(schema_file), "--tool explain lateral dispatch to a peer")
    )
    enforce_spawn_policy(model._argv(str(schema_file), "--tool"))
    enforce_spawn_policy(model._argv(str(schema_file), "--dispatch=peer --mcp config"))


def test_spawn_gate_passes_flag_like_option_values() -> None:
    """Flag-like text in a known option's value slot passes."""
    enforce_spawn_policy(
        ["muse", "exec", "--workspace", "--tool", "--contributor-mode", "on", "hi"]
    )
    enforce_spawn_policy(
        ["muse", "exec", "--effort", "--dispatch", "--contributor-mode", "on", "hi"]
    )
    enforce_spawn_policy(
        ["muse", "exec", "--output-schema", "--peer", "--contributor-mode", "on", "hi"]
    )
    enforce_spawn_policy(
        ["muse", "exec", "--workspace=--tool", "--contributor-mode", "on", "hi"]
    )


def test_spawn_gate_still_refuses_real_options_in_option_slots(
    tmp_path: Path,
) -> None:
    """Real capability options are refused with values handled as pairs."""
    model = MuseCLIModel(allowed_root=tmp_path)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    # An appended capability option is refused even though the prompt passed.
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(
            model._argv(str(schema_file), "make a plan") + ["--tool", "delegate"]
        )
    # The value paired with a denied option does not hide the option.
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(["muse", "exec", "--json", "--dispatch", "peer", "prompt"])
    # The --opt=value spelling is judged by its option part.
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(
            [
                "muse",
                "exec",
                "--json",
                "--tool=delegate",
                "--contributor-mode",
                "on",
                "hi",
            ]
        )
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(
            [
                "muse",
                "exec",
                "--json",
                "--MCP-config=x",
                "--contributor-mode",
                "on",
                "hi",
            ]
        )
    # Consuming a flag-like value must not swallow the following option.
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(
            [
                "muse",
                "exec",
                "--workspace",
                "--tool",
                "--dispatch",
                "peer",
                "--contributor-mode",
                "on",
                "hi",
            ]
        )


def test_spawn_gate_refuses_trailing_denied_flag_and_inline_equals_swallow(
    tmp_path: Path,
) -> None:
    """Evasion holes: a trailing denied flag is refused; --opt=value swallows nothing.

    The prompt exemption fires only for the real ``--contributor-mode
    <on|off>`` tail, so a denied flag in final position -- appended
    after the prompt, bare, or trailing a mid-shape argv -- is scanned
    as an option and refused. An inline ``--opt=value`` carries its
    value after the ``=``, so a denied flag in the next element is
    still refused rather than swallowed as the value.
    """
    model = MuseCLIModel(allowed_root=tmp_path)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    # Hole A: denied flag in final position is an option, not the prompt.
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(model._argv(str(schema_file), "make a plan") + ["--tool"])
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(["muse", "exec", "--tool"])
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(
            ["muse", "exec", "--json", "--contributor-mode", "on", "hi", "--tool"]
        )
    # Hole B: inline "=" does not consume the next element.
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(
            [
                "muse",
                "exec",
                "--workspace=--tool",
                "--tool",
                "evil",
                "--contributor-mode",
                "on",
                "hi",
            ]
        )


def test_complete_records_requested_and_observed_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stubbed call records requested policy apart from observed effect."""
    monkeypatch.setenv(PROBE_KEY, PROBE_VALUE)
    _stub_run(monkeypatch, _answer())
    model = MuseCLIModel(allowed_root=tmp_path)
    completion = model.complete("make a plan", {"type": "object"})
    invocation = completion.meta["invocation"]
    requested = invocation["requested_permissions"]
    observed = invocation["observed_permissions"]
    assert requested["tools"] == "none"
    assert requested["workspace_root"] == str(tmp_path)
    assert observed["cwd"] == str(tmp_path)
    assert "absolute host paths" in observed["filesystem_limit"]
    assert requested != observed
    assert PROBE_VALUE not in json.dumps(invocation)


# --- Vision argv and invocation tests ---


def _make_minimal_test_png(width: int = 2, height: int = 2) -> bytes:
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


def test_scalar_argv_unchanged(tmp_path: Path) -> None:
    model = MuseCLIModel(allowed_root=tmp_path)
    schema_file = str(tmp_path / "schema.json")
    argv = model._argv(schema_file, "scalar prompt")

    assert argv == [
        "muse",
        "exec",
        "--json",
        "--effort",
        "max",
        "--model",
        "muse-spark-1.3",
        "--output-schema",
        schema_file,
        "--workspace",
        str(tmp_path),
        "--contributor-mode",
        "on",
        "scalar prompt",
    ]
    # No vision flags present on scalar call
    assert "--provider" not in argv
    assert "--max-model-steps" not in argv
    assert "--disable-shell" not in argv
    assert "--disable-write" not in argv
    assert "--disable-web-tools" not in argv
    assert "--no-foreign-personal-context" not in argv
    assert "--disable-reminders" not in argv
    assert "--image" not in argv


def test_vision_argv_structure(tmp_path: Path) -> None:
    model = MuseCLIModel(allowed_root=tmp_path)
    schema_file = str(tmp_path / "schema.json")
    img_path = tmp_path / "image.png"
    argv = model._argv(schema_file, "vision prompt", [img_path])

    assert "--provider" in argv
    prov_idx = argv.index("--provider")
    assert argv[prov_idx + 1] == "meta"

    assert "--base-url" in argv
    base_idx = argv.index("--base-url")
    assert argv[base_idx + 1] == "https://api.meta.ai/v1"

    assert "--preset" in argv
    preset_idx = argv.index("--preset")
    assert argv[preset_idx + 1] == "empty"

    assert "--max-model-steps" in argv
    step_idx = argv.index("--max-model-steps")
    assert argv[step_idx + 1] == "1"

    assert "--disable-shell" in argv
    assert "--disable-write" in argv
    assert "--disable-web-tools" in argv
    assert "--no-foreign-personal-context" in argv
    assert "--disable-reminders" in argv
    assert "--disable-request-user-input" in argv

    assert "--image" in argv
    img_idx = argv.index("--image")
    assert argv[img_idx + 1] == str(img_path)

    assert argv[-3:] == ["--contributor-mode", "on", "vision prompt"]
    # Verify built vision argv passes spawn policy
    enforce_spawn_policy(argv)


def test_complete_with_image_stages_in_private_temp_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller_img = tmp_path / "mutable_caller.png"
    caller_img.write_bytes(_make_minimal_test_png(2, 2))

    captured_argv: list[str] = []
    expected_bytes = caller_img.read_bytes()

    def mock_run(argv: list[str], **kwargs: Any) -> _Proc:
        captured_argv.extend(argv)
        caller_img.write_bytes(b"changed by caller after validation")
        staged = Path(argv[argv.index("--image") + 1])
        assert staged.read_bytes() == expected_bytes
        assert kwargs["cwd"] == str(staged.parent)
        assert argv[argv.index("--workspace") + 1] == str(staged.parent)
        assert set(p.name for p in staged.parent.iterdir()) == {
            staged.name,
            "response-schema.json",
        }
        assert staged.stat().st_mode & 0o777 == 0o400
        assert staged.parent.stat().st_mode & 0o777 == 0o700
        usage_line = json.dumps(
            {
                "payload_type": "run.usage",
                "payload": {
                    "kind": "run_usage",
                    "models": [
                        {
                            "model": "muse-spark-1.3",
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "calls": 1,
                        }
                    ],
                },
            }
        )
        term_line = _terminal_event(json.dumps({"seed": 1}))
        return _Proc(stdout=f"{usage_line}\n{term_line}")

    monkeypatch.setattr(subprocess, "run", mock_run)

    model = MuseCLIModel(model="muse-spark-1.3")
    model.complete_with_image("prompt", {"type": "object"}, caller_img)

    assert "--image" in captured_argv
    passed_path_str = captured_argv[captured_argv.index("--image") + 1]
    passed_path = Path(passed_path_str)

    # Must be staged into a private temp directory, NOT caller's path
    assert passed_path != caller_img
    assert "ruloom-staged-img-" in str(passed_path)

    assert not passed_path.exists()


def test_complete_with_image_refuses_unverified_model(tmp_path: Path) -> None:
    img = tmp_path / "img.png"
    img.write_bytes(_make_minimal_test_png(2, 2))

    model = MuseCLIModel(model="arbitrary-unverified-model")
    with pytest.raises(ModelError, match="does not support vision capability"):
        model.complete_with_image("prompt", {"type": "object"}, img)


def test_vision_argv_spawn_policy_refuses_injected_flags(tmp_path: Path) -> None:
    model = MuseCLIModel()
    schema_file = str(tmp_path / "schema.json")
    img_path = tmp_path / "image.png"
    argv = model._argv(schema_file, "vision prompt", [img_path])

    # Injecting denied capability option before prompt
    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(argv[:-3] + ["--tool", "bash"] + argv[-3:])

    with pytest.raises(ModelError, match="outside the permission policy"):
        enforce_spawn_policy(argv[:-3] + ["--dispatch"] + argv[-3:])


def test_complete_with_image_validates_png_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = MuseCLIModel()
    spawn_called = False

    def mock_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal spawn_called
        spawn_called = True
        return _answer()

    monkeypatch.setattr(subprocess, "run", mock_run)

    corrupt_png = tmp_path / "bad.png"
    corrupt_png.write_bytes(b"CORRUPT_NOT_PNG" * 4)

    with pytest.raises(ModelError, match="invalid PNG magic header"):
        model.complete_with_image("prompt", {"type": "object"}, corrupt_png)

    assert not spawn_called


def test_complete_with_image_records_cost_and_media_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img_path = tmp_path / "test.png"
    img_bytes = _make_minimal_test_png(width=2, height=2)
    img_path.write_bytes(img_bytes)

    usage_line = json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {
                "kind": "run_usage",
                "models": [
                    {
                        "model": "muse-spark-1.3",
                        "input_tokens": 150,
                        "output_tokens": 50,
                        "calls": 1,
                    }
                ],
                "cost": {"amount": 0.042, "currency": "USD", "source": "cli"},
            },
        }
    )
    terminal_line = _terminal_event(json.dumps({"seed": 777}))

    proc = _Proc(stdout=f"{usage_line}\n{terminal_line}")
    _stub_run(monkeypatch, proc)

    model = MuseCLIModel()
    completion = model.complete_with_image("critique", {"type": "object"}, img_path)

    assert completion.data == {"seed": 777}
    assert completion.meta["cost"] == {
        "status": "reported",
        "amount": 0.042,
        "currency": "USD",
        "source": "cli",
    }

    media = completion.meta["media"]
    assert media["media_type"] == "image/png"
    assert media["width"] == 2
    assert media["height"] == 2
    assert media["size_bytes"] == len(img_bytes)
    assert media["digest"].startswith("sha256:")
    assert "data:image" not in str(completion.meta)

    invocation = completion.meta["invocation"]
    assert invocation["media"]["digest"] == media["digest"]
    assert invocation["media"]["width"] == 2


def test_complete_with_image_unavailable_cost_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img_path = tmp_path / "test.png"
    img_path.write_bytes(_make_minimal_test_png(2, 2))

    usage_line = json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {
                "kind": "run_usage",
                "models": [
                    {
                        "model": "muse-spark-1.3",
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "calls": 1,
                    }
                ],
            },
        }
    )
    terminal_line = _terminal_event(json.dumps({"seed": 888}))

    proc = _Proc(stdout=f"{usage_line}\n{terminal_line}")
    _stub_run(monkeypatch, proc)

    model = MuseCLIModel()
    completion = model.complete_with_image("critique", {"type": "object"}, img_path)
    assert completion.meta["cost"] == {
        "status": "unavailable",
        "reason": "transport reports no valid monetary cost",
    }


def test_complete_with_image_stderr_redacted_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img_path = tmp_path / "test.png"
    img_path.write_bytes(_make_minimal_test_png(2, 2))

    secret = "SUPER_SECRET_TOKEN_DO_NOT_LEAK"
    proc = _Proc()
    proc.returncode = 1
    proc.stderr = f"Sensitive stderr output: {secret}"

    _stub_run(monkeypatch, proc)

    model = MuseCLIModel()
    with pytest.raises(ModelError) as exc_info:
        model.complete_with_image("prompt", {"type": "object"}, img_path)

    msg = str(exc_info.value)
    # Generic error; secret stderr is redacted
    assert msg == "muse exec vision call failed with exit code 1"
    assert secret not in msg


def test_parse_vision_refuses_model_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img_path = tmp_path / "test.png"
    img_path.write_bytes(_make_minimal_test_png(2, 2))

    usage_line = json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {
                "kind": "run_usage",
                "models": [
                    {
                        "model": "unexpected-model",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "calls": 1,
                    }
                ],
            },
        }
    )
    terminal_line = _terminal_event(json.dumps({"seed": 1}))
    _stub_run(monkeypatch, _Proc(stdout=f"{usage_line}\n{terminal_line}"))

    model = MuseCLIModel(model="muse-spark-1.3")
    with pytest.raises(ModelError, match="muse exec vision model mismatch"):
        model.complete_with_image("prompt", {"type": "object"}, img_path)


def test_parse_vision_refuses_negative_or_missing_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img_path = tmp_path / "test.png"
    img_path.write_bytes(_make_minimal_test_png(2, 2))

    # Missing calls counter
    usage_line = json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {
                "kind": "run_usage",
                "models": [
                    {
                        "model": "muse-spark-1.3",
                        "input_tokens": 10,
                        "output_tokens": 5,
                    }
                ],
            },
        }
    )
    terminal_line = _terminal_event(json.dumps({"seed": 1}))
    _stub_run(monkeypatch, _Proc(stdout=f"{usage_line}\n{terminal_line}"))

    model = MuseCLIModel(model="muse-spark-1.3")
    with pytest.raises(
        ModelError, match="missing or negative token usage counter 'calls'"
    ):
        model.complete_with_image("prompt", {"type": "object"}, img_path)


def test_parse_vision_refuses_schema_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img_path = tmp_path / "test.png"
    img_path.write_bytes(_make_minimal_test_png(2, 2))

    usage_line = json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {
                "kind": "run_usage",
                "models": [
                    {
                        "model": "muse-spark-1.3",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "calls": 1,
                    }
                ],
            },
        }
    )
    # Output fails schema requirement 'seed' integer
    terminal_line = _terminal_event(json.dumps({"seed": "not-an-integer"}))
    _stub_run(monkeypatch, _Proc(stdout=f"{usage_line}\n{terminal_line}"))

    model = MuseCLIModel(model="muse-spark-1.3")
    schema = {
        "type": "object",
        "properties": {"seed": {"type": "integer"}},
        "required": ["seed"],
    }
    with pytest.raises(ModelError, match="vision output violates requested schema"):
        model.complete_with_image("prompt", schema, img_path)


@pytest.mark.parametrize(
    "wire",
    [
        '{"payload_type":"structured","payload":{"structuredOutput":{"note":"one","note":"two"}}}',
        '{"payload_type":"structured","payload":{"structuredOutput":{"value":NaN}}}',
    ],
)
def test_vision_rejects_invalid_json_in_event_envelope(wire: str) -> None:
    with pytest.raises(ModelError, match="malformed JSONL"):
        MuseCLIModel()._parse_vision(_Proc(wire), {"type": "object"})


def test_vision_schema_error_redacts_rejected_value() -> None:
    secret = "provider-error-canary-do-not-echo"
    wire = _terminal_event(json.dumps({"note": secret}))
    with pytest.raises(ModelError, match="violates requested schema") as err:
        MuseCLIModel()._parse_vision(
            _Proc(wire), {"type": "object", "properties": {"note": {"type": "integer"}}}
        )
    assert secret not in str(err.value)


def test_invalid_vision_schema_refused_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "synthetic.png"
    image.write_bytes(_make_minimal_test_png())
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(True))
    with pytest.raises(ModelError, match="invalid vision output schema"):
        MuseCLIModel().complete_with_image(
            "review", {"type": "invented-schema-type"}, image
        )
    assert not called
