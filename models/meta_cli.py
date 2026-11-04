"""Run structured prompts through ``muse exec`` with an output schema.

The adapter shells out to::

    muse exec --json --effort <effort> --model <model> \\
        --output-schema <schema file> [--workspace <allowed root>] \\
        --contributor-mode on|off <prompt>

and parses the JSONL events on stdout: the structured answer (the
``structuredOutput`` event when the CLI emits one, otherwise the terminal
event's text, which the CLI constrains to the schema) plus the ``run.usage``
event's per-model token counts. A refusal, a nonzero exit, or a malformed
answer raises :class:`ModelError`.

Permission control, stated accurately: the adapter filters the child
environment to an allowlist, pins the child working directory, and
spawns a tool-free argv (no MCP, tool, dispatch, or peer flags are ever
forwarded or named; the spawn gate refuses them). The pinned directory
is NOT filesystem confinement: a child naming an absolute host path
outside the root can still reach it. Requested permissions
(:data:`REQUESTED_PERMISSIONS`) are recorded separately from the
observed effective permissions on every call.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from .base import (
    UNKNOWN_MODEL,
    Completion,
    ModelCapabilities,
    ModelError,
    Usage,
    image_wire_schema,
    monetary_cost,
    structured_image_schema,
)
from .image_input import read_validated_image

#: Verified model identity for the Muse vision fallback.
SUPPORTED_VISION_MODEL = "muse-spark-1.3"

#: Base URL for Meta API provider calls.
META_API_BASE_URL = "https://api.meta.ai/v1"

#: Environment keys the child process may inherit (explicit allowlist).
#: Everything else -- including every ``RULOOM_*`` setting, API tokens,
#: and MCP/tool configuration -- is stripped before the child starts, so
#: ambient caller secrets can never leak into the model invocation.
ALLOWED_ENV_KEYS = (
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
)

#: Secret hints: any allowlisted key containing one of these substrings
#: (case-insensitive) is still stripped, so a future allowlist edit can
#: never smuggle a credential into the child environment.
SECRET_HINTS = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASSWD",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
    "PRIVATE",
)


def _looks_secret(key: str) -> bool:
    """True when ``key`` smells like a credential carrier."""
    upper = str(key).upper()
    return any(hint in upper for hint in SECRET_HINTS)


#: Requested permission policy for every child the adapter spawns.
#: ``filesystem`` names a pinned working directory only -- relative
#: child writes land in the pinned root, but absolute host paths
#: outside the root are NOT contained by this boundary. ``tools`` and
#: ``network_peers`` grant nothing: the adapter forwards no MCP, tool,
#: dispatch, or peer routing and names no such flag. ``environment``
#: is the allowlist minus secret-like keys.
REQUESTED_PERMISSIONS: dict[str, str] = {
    "filesystem": "cwd-pinned-only",
    "tools": "none",
    "network_peers": "none",
    "environment": "allowlist-only",
}

#: Flag prefixes the spawn gate refuses in option position. Only
#: dash-leading argv parts sitting in an option slot are checked, so
#: flag-like text in value slots (option values, paths) and in the
#: prompt slot (the final argv element of a ``muse exec`` argv) can
#: never trip it. ``--contributor-mode`` is the one explicit capability
#: flag and is allowed; everything naming MCP, tool, dispatch, peer, or
#: delegate routing as an option is denied at spawn.
DENIED_SPAWN_FLAG_PREFIXES = (
    "--mcp",
    "--tool",
    "--dispatch",
    "--peer",
    "--delegate",
)

#: Options of the ``muse exec`` argv that take no value.
_SPAWN_NO_VALUE_OPTIONS = frozenset(
    {
        "--json",
        "--disable-shell",
        "--disable-write",
        "--disable-web-tools",
        "--no-foreign-personal-context",
        "--disable-reminders",
        "--disable-request-user-input",
    }
)

#: Options of the ``muse exec`` argv that consume exactly one following
#: argv element as their value (see :meth:`MuseCLIModel._argv`). A value
#: is never refused, even when it looks like a flag.
_SPAWN_SINGLE_VALUE_OPTIONS = frozenset(
    {
        "--effort",
        "--model",
        "--output-schema",
        "--image",
        "--workspace",
        "--contributor-mode",
        "--provider",
        "--base-url",
        "--preset",
        "--max-model-steps",
    }
)


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


def requested_permissions(allowed_root: str | Path | None) -> dict[str, str]:
    """Describe the permission policy requested for one child spawn."""
    policy = dict(REQUESTED_PERMISSIONS)
    if allowed_root is not None:
        policy["workspace_root"] = str(Path(allowed_root))
    else:
        policy["workspace_root"] = "invoking-directory"
    return policy


def observed_permissions(cwd: str, env_keys: list[str]) -> dict[str, object]:
    """Describe the effective permissions one spawn actually provided.

    This is the observed side of the requested/observed split: the
    pinned directory the child ran in, the environment key names it
    inherited, and the tool-free outcome of the spawn gate -- plus the
    confinement limit the boundary does not provide (absolute host
    paths outside the root stay reachable).
    """
    return {
        "filesystem": "cwd-pinned-only",
        "filesystem_limit": "absolute host paths outside the root are not contained",
        "tools": "none-named-on-argv",
        "tools_limit": "the CLI's own runtime behaviour is outside product control",
        "network_peers": "none-granted",
        "environment": "allowlist-only",
        "cwd": cwd,
        "env_keys": sorted(env_keys),
    }


def _is_muse_exec_shape(argv: list[str]) -> bool:
    """True when ``argv`` has the ``muse exec`` shape we build.

    :meth:`MuseCLIModel._argv` always ends with the
    ``--contributor-mode <on|off> <prompt>`` tail, so only an argv
    with the ``exec`` subcommand AND that exact tail is trusted to
    hold the prompt in its final element. Only then is the final
    element exempt from the option scan. Anything else -- a bare
    ``["muse", "exec", "--tool"]``, a real argv with a capability
    option appended after the prompt, or any hand-built transport --
    is scanned strictly, so a trailing denied flag can never hide in
    the prompt slot.
    """
    texts = [str(part) for part in argv]
    if len(texts) < 4 or texts[1] != "exec":
        return False
    return texts[-3].lower() == "--contributor-mode" and texts[-2].lower() in (
        "on",
        "off",
    )


def enforce_spawn_policy(argv: list[str]) -> None:
    """Refuse a child argv that requests tool/dispatch capability.

    The adapter builds a tool-free argv by construction; this gate
    makes that property ENFORCED for every caller of the spawn path.
    It validates the known option/value structure instead of scanning
    every string indiscriminately:

    * a dash-leading part is refused only when it sits in an option
      slot and names MCP, tool, dispatch, peer, or delegate routing
      (``--opt=value`` spellings are judged by the option part before
      the ``=``), raising :class:`ModelError` before anything spawns;
    * the single value following a known single-value option
      (``--effort``, ``--model``, ``--output-schema``, ``--image``,
      ``--workspace``, ``--contributor-mode``, ``--provider``,
      ``--base-url``, ``--preset``, ``--max-model-steps``) is never
      scanned, so a flag-like path or value cannot trip the gate;
    * the prompt -- the final element of a ``muse exec`` shaped argv,
      recognised only by the real ``--contributor-mode <on|off>``
      tail :meth:`MuseCLIModel._argv` always emits -- is never
      scanned, so a literal prompt beginning ``--tool`` (or any other
      denied-looking wording) passes while real capability options
      appended around it, including a trailing denied flag, are still
      refused;

    Single-dash spellings (``-tool``) are not refused: no supported
    ``muse exec`` flag has that shape, so the gate leaves that noted
    limitation in place rather than guessing at short-flag meanings.
    """
    texts = [str(part) for part in argv]
    prompt_index = len(texts) - 1 if _is_muse_exec_shape(texts) else -1
    expect_value = False
    for index, text in enumerate(texts):
        if index == prompt_index:
            continue
        if expect_value:
            expect_value = False
            continue
        if not text.startswith("-"):
            continue
        option = text.split("=", 1)[0]
        lowered = option.lower()
        if lowered.startswith(DENIED_SPAWN_FLAG_PREFIXES):
            raise ModelError(
                f"child spawn refused: tool/dispatch capability "
                f"{text!r} is outside the permission policy"
            )
        if lowered in _SPAWN_SINGLE_VALUE_OPTIONS and "=" not in text:
            # A bare "--opt" consumes the next element as its value.
            # An inline "--opt=value" already carries its value after
            # the "=", so the next element stays in its own slot and a
            # denied flag there is still refused.
            expect_value = True


def restricted_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the child environment: allowlist minus secret-like keys.

    ``source`` defaults to the live process environment; tests inject a
    controlled mapping (including a probe secret) instead. Only keys in
    :data:`ALLOWED_ENV_KEYS` survive, and secret-like keys never do, so
    the returned mapping carries key names the gate may record without
    ever carrying secret values into the log.
    """
    raw: Mapping[str, str] = os.environ if source is None else source
    keep: dict[str, str] = {}
    for key in ALLOWED_ENV_KEYS:
        if key in raw and not _looks_secret(key):
            value = raw[key]
            if isinstance(value, str):
                keep[key] = value
    return keep


def resolve_cwd(allowed_root: str | Path | None) -> str:
    """Pin the child working directory explicitly (never inherit blindly).

    An explicit ``allowed_root`` pins both the ``--workspace`` flag and
    the child cwd to that root. Without one the child runs in the
    invoking process's working directory -- the repo checkout under
    normal CLI use -- resolved and recorded at call time. Either way
    the effective directory is explicit in the invocation record.
    """
    if allowed_root is not None:
        return str(Path(allowed_root))
    return str(Path.cwd())


@dataclass
class MuseCLIModel:
    """A :class:`StructuredModel` backed by the ``muse`` CLI."""

    model: str = "muse-spark-1.3"
    effort: str = "max"
    contributor: bool = True
    allowed_root: str | Path | None = None
    cli: str = "muse"
    timeout_s: float = 1200.0

    @property
    def capabilities(self) -> ModelCapabilities:
        """Declared adapter capabilities scoped strictly to verified model identity."""
        if self.model == SUPPORTED_VISION_MODEL:
            return ModelCapabilities(
                input_modalities=("text", "image"),
                structured_output=True,
                image_media_types=("image/png",),
                max_image_bytes=4 * 1024 * 1024,
                max_image_pixels=4_000_000,
            )
        return ModelCapabilities(
            input_modalities=("text",),
            structured_output=True,
            image_media_types=(),
            max_image_bytes=0,
            max_image_pixels=0,
        )

    def _argv(
        self,
        schema_file: str,
        prompt: str,
        image_paths: list[str | Path] | None = None,
    ) -> list[str]:
        if not image_paths:
            argv = [
                self.cli,
                "exec",
                "--json",
                "--effort",
                self.effort,
                "--model",
                self.model,
                "--output-schema",
                schema_file,
            ]
            if self.allowed_root is not None:
                argv += ["--workspace", str(self.allowed_root)]
            argv += ["--contributor-mode", "on" if self.contributor else "off", prompt]
            return argv

        # Vision invocation: explicitly select meta provider, set base URL and empty preset,
        # bound to 1 model step, and disable shell, write, web tools, foreign context,
        # reminders, and request-user-input.
        argv = [
            self.cli,
            "exec",
            "--json",
            "--provider",
            "meta",
            "--base-url",
            META_API_BASE_URL,
            "--preset",
            "empty",
            "--effort",
            self.effort,
            "--model",
            self.model,
            "--output-schema",
            schema_file,
            "--max-model-steps",
            "1",
            "--disable-shell",
            "--disable-write",
            "--disable-web-tools",
            "--no-foreign-personal-context",
            "--disable-reminders",
            "--disable-request-user-input",
        ]
        for image_path in image_paths:
            argv += ["--image", str(image_path)]
        # Vision context is restricted to the staged image/schema workspace.
        argv += ["--workspace", str(Path(image_paths[0]).parent)]
        argv += ["--contributor-mode", "on" if self.contributor else "off", prompt]
        return argv

    last_invocation: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False
    )
    """Effective permissions of the most recent child invocation.

    Records ``argv``, the pinned ``cwd``, the inherited environment
    *key names* (never values), the ``contributor`` flag, and the
    requested/returned model identity, plus ``requested_permissions``
    (the policy asked for) separately from ``observed_permissions``
    (the effective permissions the spawn provided, including the
    confinement limit it does not provide). The run log stores this
    record per model call; secret values never enter it.
    """

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run one ``muse exec`` argv under the restricted invocation.

        The child gets a pinned working directory, the allowlisted
        environment only, and exactly the argv built by :meth:`_argv` --
        no inherited MCP servers, tool dispatch, or peer routing, which
        the argv never names. The spawn gate refuses tool/dispatch
        capability flags before anything spawns. Requested permissions
        are recorded separately from the observed effective
        permissions. Transport failures raise ModelError.
        """
        enforce_spawn_policy(list(argv))
        env = restricted_env()
        cwd = resolve_cwd(self.allowed_root)
        self.last_invocation = {
            "argv": list(argv),
            "cwd": cwd,
            "env_keys": sorted(env),
            "contributor": self.contributor,
            "model": self.model,
            "requested_permissions": requested_permissions(self.allowed_root),
            "observed_permissions": observed_permissions(cwd, sorted(env)),
        }
        try:
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_s,
                env=env,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"muse exec timed out: {exc}") from exc
        except OSError as exc:
            raise ModelError(f"muse exec failed to start: {exc}") from exc

    def _run_vision(
        self, argv: list[str], workspace: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run vision child invocation with generic/redacted errors."""
        enforce_spawn_policy(list(argv))
        env = restricted_env()
        cwd = str(workspace)
        self.last_invocation = {
            "argv": list(argv),
            "cwd": cwd,
            "env_keys": sorted(env),
            "contributor": self.contributor,
            "model": self.model,
            "requested_permissions": requested_permissions(workspace),
            "observed_permissions": observed_permissions(cwd, sorted(env)),
        }
        try:
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_s,
                env=env,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            raise ModelError("muse exec vision call timed out") from None
        except OSError:
            raise ModelError("muse exec failed to start") from None

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        """Run ``prompt`` under ``schema`` and return the structured answer."""
        schema_dict = dict(schema)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as handle:
            json.dump(schema_dict, handle)
            schema_file = handle.name
        try:
            proc = self._run(self._argv(schema_file, prompt))
        finally:
            Path(schema_file).unlink(missing_ok=True)
        completion = self._parse(proc, schema_dict)
        self.last_invocation = {
            **self.last_invocation,
            "returned_model": completion.usage.model,
        }
        completion.meta["invocation"] = dict(self.last_invocation)
        return completion

    def complete_with_image(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        image_path: str | Path,
    ) -> Completion:
        """Run ``prompt`` with one attached image under ``schema``.

        The caller's image is strictly validated against capability limits, then
        the immutable validated payload is staged to a private temporary directory
        before invocation. Fails closed on any model mismatch, invalid schema,
        malformed output, or unverified counters.
        """
        if self.model != SUPPORTED_VISION_MODEL or not self.capabilities.supports_image:
            raise ModelError(
                f"model {self.model!r} does not support vision capability; "
                f"verified vision model is {SUPPORTED_VISION_MODEL!r}"
            )

        payload, media_type, width, height = read_validated_image(
            image_path, self.capabilities
        )
        digest = hashlib.sha256(payload).hexdigest()

        schema_dict = structured_image_schema(schema)
        with tempfile.TemporaryDirectory(prefix="ruloom-staged-img-") as staged_dir:
            staged_path = Path(staged_dir) / f"staged-{digest[:16]}.png"
            staged_path.write_bytes(payload)
            try:
                staged_path.chmod(0o400)
            except OSError:
                pass

            schema_file = Path(staged_dir) / "response-schema.json"
            schema_file.write_text(
                json.dumps(image_wire_schema(schema_dict)), encoding="utf-8"
            )
            argv = self._argv(str(schema_file), prompt, [staged_path])
            proc = self._run_vision(argv, Path(staged_dir))

            completion = self._parse_vision(proc, schema_dict)

        media_meta = {
            "digest": f"sha256:{digest}",
            "media_type": media_type,
            "size_bytes": len(payload),
            "width": width,
            "height": height,
        }
        self.last_invocation = {
            **self.last_invocation,
            "returned_model": completion.usage.model,
            "media": media_meta,
        }
        completion.meta["invocation"] = dict(self.last_invocation)
        completion.meta["media"] = media_meta
        return completion

    def _parse(
        self, proc: subprocess.CompletedProcess[str], schema_dict: dict
    ) -> Completion:
        """Parse ``muse exec`` JSONL output into a structured Completion (scalar path).

        Model identity policy: ``Usage.model`` and ``meta["model"]`` carry
        the *observed* identity the transport reported -- ``UNKNOWN_MODEL``
        when it reported none, a ``multiple:...`` aggregate when it
        reported several -- while ``meta["requested_model"]`` keeps the
        requested name and ``meta["models"]`` keeps the normalised
        per-model entries in transport order. A mismatch between requested
        and observed never refuses; both are recorded.
        """
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip()[-2000:]
            raise ModelError(f"muse exec exited {proc.returncode}: {tail}")

        structured: Any = None
        terminal_text: str | None = None
        terminal_kind: str | None = None
        usage_models: list[dict[str, Any]] = []
        for line in (proc.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ModelError(f"muse exec emitted malformed JSONL: {exc}") from exc
            if not isinstance(event, dict):
                raise ModelError("muse exec emitted a non-object event")
            payload_type = str(event.get("payload_type", ""))
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = str(payload.get("kind", ""))
            if payload_type == "run.usage" or kind == "run_usage":
                models = payload.get("models")
                if isinstance(models, list):
                    usage_models.extend(m for m in models if isinstance(m, dict))
            if (
                "struct" in payload_type.lower()
                or "struct" in kind.lower()
                or "structuredOutput" in payload
                or "structured_output" in payload
            ):
                structured = payload.get(
                    "structuredOutput", payload.get("structured_output")
                )
                if structured is None:
                    structured = payload.get("output", payload.get("text"))
            if payload_type == "run.terminal.completed" or kind == "run_terminal":
                terminal_kind = payload.get("terminal")
                text = payload.get("text")
                if isinstance(text, str):
                    terminal_text = text

        if terminal_kind is not None and terminal_kind != "completed":
            raise ModelError(f"model run ended with terminal={terminal_kind!r}")

        answer = structured if structured is not None else terminal_text
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            raise ModelError("muse exec produced no structured answer")
        if isinstance(answer, str):
            try:
                answer = json.loads(answer)
            except json.JSONDecodeError as exc:
                raise ModelError(f"structured answer is not valid JSON: {exc}") from exc
        if not isinstance(answer, dict):
            raise ModelError(
                f"structured answer is not a JSON object: {type(answer).__name__}"
            )

        per_model: list[dict[str, Any]] = []
        for entry in usage_models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("model")
            observed_name = (
                name if isinstance(name, str) and name.strip() else UNKNOWN_MODEL
            )
            counts: dict[str, Any] = {"model": observed_name}
            for key in (
                "input_tokens",
                "cached_tokens",
                "output_tokens",
                "reasoning_tokens",
                "calls",
            ):
                value = entry.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int):
                    counts[key] = 0
                else:
                    counts[key] = value
            per_model.append(counts)
        totals = {
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "calls": 0,
        }
        for entry in per_model:
            for key in totals:
                totals[key] += entry[key]
        if not per_model:
            observed = UNKNOWN_MODEL
        else:
            distinct = list(dict.fromkeys(entry["model"] for entry in per_model))
            if len(distinct) == 1:
                observed = distinct[0]
            else:
                observed = "multiple:" + "+".join(sorted(distinct))
        usage = Usage(model=observed, **totals)
        meta = {
            "model": observed,
            "requested_model": self.model,
            "models": per_model,
            "effort": self.effort,
            "contributor": self.contributor,
            "output_schema": schema_dict,
        }
        return Completion(data=answer, usage=usage, meta=meta)

    def _parse_vision(
        self, proc: subprocess.CompletedProcess[str], schema_dict: dict
    ) -> Completion:
        """Parse ``muse exec`` JSONL output strictly for vision calls.

        Enforces exact observed model identity matching requested model,
        strict JSON syntax without non-finite numbers or duplicates,
        jsonschema validation, complete non-negative token usage counters,
        and redacted generic provider errors.
        """
        if proc.returncode != 0:
            raise ModelError(
                f"muse exec vision call failed with exit code {proc.returncode}"
            )

        structured: Any = None
        terminal_text: str | None = None
        terminal_kind: str | None = None
        usage_models: list[dict[str, Any]] = []
        raw_cost: Any = None

        for line in (proc.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(
                    line,
                    parse_constant=_reject_constant,
                    object_pairs_hook=_reject_duplicates,
                )
            except ValueError:
                raise ModelError(
                    "muse exec emitted malformed JSONL in vision call"
                ) from None
            if not isinstance(event, dict):
                raise ModelError("muse exec emitted a non-object event in vision call")
            payload_type = str(event.get("payload_type", ""))
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = str(payload.get("kind", ""))
            if payload_type == "run.usage" or kind == "run_usage":
                models = payload.get("models")
                if isinstance(models, list):
                    if not all(isinstance(m, dict) for m in models):
                        raise ModelError("invalid vision usage model entries")
                    usage_models.extend(models)
                if "cost" in payload:
                    raw_cost = payload["cost"]
                elif "total_cost" in payload:
                    raw_cost = payload["total_cost"]
            if (
                "struct" in payload_type.lower()
                or "struct" in kind.lower()
                or "structuredOutput" in payload
                or "structured_output" in payload
            ):
                structured = payload.get(
                    "structuredOutput", payload.get("structured_output")
                )
                if structured is None:
                    structured = payload.get("output", payload.get("text"))
            if payload_type == "run.terminal.completed" or kind == "run_terminal":
                terminal_kind = payload.get("terminal")
                text = payload.get("text")
                if isinstance(text, str):
                    terminal_text = text

        if terminal_kind != "completed":
            raise ModelError("vision model run did not complete")

        raw_answer = structured if structured is not None else terminal_text
        if raw_answer is None or (
            isinstance(raw_answer, str) and not raw_answer.strip()
        ):
            raise ModelError("muse exec produced no structured answer for vision call")

        if isinstance(raw_answer, str):
            try:
                answer = json.loads(
                    raw_answer,
                    parse_constant=_reject_constant,
                    object_pairs_hook=_reject_duplicates,
                )
            except ValueError:
                raise ModelError("vision structured answer is not valid JSON") from None
        elif isinstance(raw_answer, dict):
            try:
                answer = json.loads(
                    json.dumps(raw_answer),
                    parse_constant=_reject_constant,
                    object_pairs_hook=_reject_duplicates,
                )
            except ValueError:
                raise ModelError("vision structured answer is invalid") from None
        else:
            raise ModelError(
                f"vision structured answer is not a JSON object: {type(raw_answer).__name__}"
            )

        if not isinstance(answer, dict):
            raise ModelError(
                f"vision structured answer is not a JSON object: {type(answer).__name__}"
            )

        # Strict schema validation
        try:
            jsonschema.validate(instance=answer, schema=schema_dict)
        except jsonschema.ValidationError:
            raise ModelError("vision output violates requested schema") from None
        except Exception:
            raise ModelError("vision response schema could not be resolved") from None

        # Model identity verification: must report exactly the requested model
        if not usage_models:
            raise ModelError("muse exec reported no usage models for vision call")

        per_model: list[dict[str, Any]] = []
        for entry in usage_models:
            name = entry.get("model")
            if (
                not isinstance(name, str)
                or not name.strip()
                or name.strip().lower() == UNKNOWN_MODEL
            ):
                raise ModelError(
                    "muse exec reported missing or unknown model identity in vision call"
                )
            observed_name = name.strip()

            counts: dict[str, Any] = {"model": observed_name}
            for key in ("input_tokens", "output_tokens", "calls"):
                val = entry.get(key)
                if type(val) is not int or isinstance(val, bool) or val < 0:
                    raise ModelError(
                        f"missing or negative token usage counter {key!r} in vision call"
                    )
                counts[key] = val
            if counts["calls"] < 1:
                raise ModelError("token usage calls must be >= 1 in vision call")

            for key in ("cached_tokens", "reasoning_tokens"):
                val = entry.get(key, 0)
                if type(val) is not int or isinstance(val, bool) or val < 0:
                    raise ModelError(
                        f"invalid token usage counter {key!r} in vision call"
                    )
                counts[key] = val

            per_model.append(counts)

        distinct = list(dict.fromkeys(entry["model"] for entry in per_model))
        if len(distinct) != 1:
            raise ModelError(
                f"muse exec vision call reported multiple models: {distinct}"
            )
        observed = distinct[0]
        if observed != self.model:
            raise ModelError(
                f"muse exec vision model mismatch: requested {self.model!r}"
            )

        totals = {
            "input_tokens": sum(e["input_tokens"] for e in per_model),
            "cached_tokens": sum(e["cached_tokens"] for e in per_model),
            "output_tokens": sum(e["output_tokens"] for e in per_model),
            "reasoning_tokens": sum(e["reasoning_tokens"] for e in per_model),
            "calls": sum(e["calls"] for e in per_model),
        }
        usage = Usage(model=observed, **totals)

        cost_meta = monetary_cost(raw_cost, source="cli")

        meta = {
            "model": observed,
            "requested_model": self.model,
            "models": per_model,
            "effort": self.effort,
            "contributor": self.contributor,
            "output_schema": schema_dict,
            "cost": cost_meta,
            "provider": "meta",
            "adapter": "meta_cli",
            "evidence_kind": "provider_reported",
            "schema_validation": {
                "local": "full",
                "wire_unsupported_keywords": ["uniqueItems"],
            },
        }
        return Completion(data=answer, usage=usage, meta=meta)
