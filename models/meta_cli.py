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
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import UNKNOWN_MODEL, Completion, ModelError, Usage

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

    def _argv(
        self,
        schema_file: str,
        prompt: str,
        image_paths: list[str | Path] | None = None,
    ) -> list[str]:
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
        for image_path in image_paths or []:
            argv += ["--image", str(image_path)]
        if self.allowed_root is not None:
            argv += ["--workspace", str(self.allowed_root)]
        argv += ["--contributor-mode", "on" if self.contributor else "off", prompt]
        return argv

    last_invocation: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False
    )
    """Effective permissions of the most recent child invocation.

    Records ``argv``, the pinned ``cwd``, the inherited environment
    *key names* (never values), the ``contributor`` flag, and the
    requested/returned model identity. The run log stores this record
    per model call; secret values never enter it.
    """

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run one ``muse exec`` argv under the restricted invocation.

        The child gets a pinned working directory, the allowlisted
        environment only, and exactly the argv built by :meth:`_argv` --
        no inherited MCP servers, tool dispatch, or peer routing, which
        the argv never names. Transport failures raise ModelError.
        """
        env = restricted_env()
        cwd = resolve_cwd(self.allowed_root)
        self.last_invocation = {
            "argv": list(argv),
            "cwd": cwd,
            "env_keys": sorted(env),
            "contributor": self.contributor,
            "model": self.model,
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

        The image is attached with ``muse exec --image <PATH>``; callers
        must only ever attach rendered synthetic artifacts, never raw
        source imagery. Parsing, usage, and errors match :meth:`complete`.
        """
        if not Path(image_path).is_file():
            raise ModelError(f"image file not found: {image_path}")
        schema_dict = dict(schema)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as handle:
            json.dump(schema_dict, handle)
            schema_file = handle.name
        try:
            proc = self._run(self._argv(schema_file, prompt, [image_path]))
        finally:
            Path(schema_file).unlink(missing_ok=True)
        completion = self._parse(proc, schema_dict)
        self.last_invocation = {
            **self.last_invocation,
            "returned_model": completion.usage.model,
        }
        completion.meta["invocation"] = dict(self.last_invocation)
        return completion

    def _parse(
        self, proc: subprocess.CompletedProcess[str], schema_dict: dict
    ) -> Completion:
        """Parse ``muse exec`` JSONL output into a structured Completion.

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
