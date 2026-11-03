"""Structured AGY calls with explicit family/model/effort and no fallback.

AGY may expose built-in tools despite requested deny settings. We reject a
response that used anything except its schema-finalisation tool, but this
post-call audit is NOT tool or filesystem confinement. Authentication and
provider routing remain owned by the installed AGY CLI.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from .base import UNKNOWN_MODEL, Completion, ModelError, Usage
from .meta_cli import restricted_env
from .selection import model_family, validate_selection


@dataclass
class AgyCLIModel:
    model: str = "claude-opus-5-5-high"
    effort: str = "high"
    cli: str = "agy"
    timeout_s: float = 1200.0
    last_invocation: dict[str, Any] = field(default_factory=dict, init=False)

    def _argv(self, schema_file: str, prompt: str, log_file: str) -> list[str]:
        family = model_family("agy_cli", self.model)
        validate_selection("agy_cli", self.model, family, self.effort)
        return [
            self.cli,
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--sandbox",
            "--disable-slash-commands",
            "--settings",
            json.dumps(
                {
                    "permissions": {"allow": [], "deny": ["*"]},
                    "allowNonWorkspaceAccess": False,
                }
            ),
            "--output-format",
            "stream-json",
            "--json-schema",
            schema_file,
            "--log-file",
            log_file,
            "--print-timeout",
            f"{self.timeout_s:g}s",
            "--print",
            prompt,
        ]

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        with tempfile.TemporaryDirectory(prefix="ruloom-agy-") as temp:
            root = Path(temp)
            schema_file = root / "schema.json"
            schema_file.write_text(json.dumps(dict(schema)), encoding="utf-8")
            argv = self._argv(str(schema_file), prompt, str(root / "agy.log"))
            env = restricted_env()
            self.last_invocation = {
                "argv": argv,
                "cwd": temp,
                "env_keys": sorted(env),
                "model": self.model,
                "effort": self.effort,
                "model_family": model_family("agy_cli", self.model),
                "requested_permissions": {
                    "tools": "deny-all",
                    "workspace_access": "temporary-only",
                },
                "observed_permissions": {"confinement": "not-established"},
            }
            try:
                proc = subprocess.run(
                    argv,
                    cwd=temp,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_s,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ModelError(f"AGY call failed: {type(exc).__name__}") from exc
            answer = self._parse(proc, dict(schema))
        self.last_invocation["returned_model"] = answer.usage.model
        answer.meta["invocation"] = dict(self.last_invocation)
        return answer

    def _parse(
        self, proc: subprocess.CompletedProcess[str], schema: dict
    ) -> Completion:
        if proc.returncode != 0:
            raise ModelError(
                f"AGY exited {proc.returncode}: {proc.stderr.strip()[-1000:]}"
            )
        init: dict = {}
        result: dict | None = None
        used: set[str] = set()
        try:
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
                if event.get("event") == "init":
                    if init:
                        raise ValueError("multiple init events")
                    init = event.get("init", {})
                    if not isinstance(init, dict):
                        raise ValueError("invalid init event")
                    self.last_invocation["observed_permissions"] = {
                        "permission_mode": init.get("permission_mode", "unknown"),
                        "tools_exposed": init.get("tools", []),
                        "confinement": "not-established",
                    }
                if event.get("event") == "step_update":
                    step = event.get("step_update", {})
                    if step.get("step_type") == "tool":
                        used.add(str(step.get("tool_name", "unknown")))
                if event.get("event") == "result":
                    if result is not None:
                        raise ValueError("multiple terminal results")
                    result = event.get("result")
                    if not isinstance(result, dict):
                        raise ValueError("invalid result event")
            self.last_invocation["tools_used"] = sorted(used)
            if used - {"finish"}:
                raise ValueError(f"unexpected tool use: {sorted(used - {'finish'})}")
            if result is None or result.get("status") != "SUCCESS":
                raise ValueError("no successful terminal result")
            observed = init.get("model") or UNKNOWN_MODEL
            if observed != UNKNOWN_MODEL and observed != self.model:
                raise ValueError(
                    f"requested {self.model!r}, CLI reported {observed!r}; no fallback"
                )
            data = result.get("structured_output")
            if data is None:
                data = json.loads(result.get("response", ""))
            jsonschema.validate(data, schema)
            if not isinstance(data, dict):
                raise ValueError("structured answer is not an object")
            counts = result.get("usage", {})

            def count(name: str) -> int:
                value = counts.get(name, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"invalid usage {name}")
                return value

            usage = Usage(
                model=observed,
                input_tokens=count("input_tokens"),
                output_tokens=count("output_tokens"),
                cached_tokens=count("cache_read_tokens"),
                reasoning_tokens=count("thinking_tokens"),
                calls=1,
            )
        except (
            ValueError,
            TypeError,
            AttributeError,
            jsonschema.ValidationError,
        ) as exc:
            raise ModelError(f"AGY response refused: {exc}") from exc
        return Completion(
            data,
            usage,
            {
                "requested_model": self.model,
                "model": observed,
                "effort": self.effort,
                "model_family": model_family("agy_cli", self.model),
                "identity_evidence": "AGY init.model only; backend weights not independently attested",
                "conversation_id": result.get("conversation_id"),
                "duration_seconds": result.get("duration_seconds"),
                "tools_used": sorted(used),
            },
        )
