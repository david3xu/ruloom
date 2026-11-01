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
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .base import Completion, ModelError, Usage


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
        image_paths: "list[str | Path] | None" = None,
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

    def _run(self, argv: list[str]) -> "subprocess.CompletedProcess[str]":
        """Run one ``muse exec`` argv; transport failures raise ModelError."""
        try:
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"muse exec timed out: {exc}") from exc
        except OSError as exc:
            raise ModelError(f"muse exec failed to start: {exc}") from exc

    def complete(
        self, prompt: str, schema: Mapping[str, Any]
    ) -> Completion:
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
        return self._parse(proc, schema_dict)

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
        return self._parse(proc, schema_dict)

    def _parse(
        self, proc: "subprocess.CompletedProcess[str]", schema_dict: dict
    ) -> Completion:
        """Parse ``muse exec`` JSONL output into a structured Completion."""
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
                    usage_models.extend(
                        m for m in models if isinstance(m, dict)
                    )
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
                raise ModelError(
                    f"structured answer is not valid JSON: {exc}"
                ) from exc
        if not isinstance(answer, dict):
            raise ModelError(
                f"structured answer is not a JSON object: {type(answer).__name__}"
            )

        totals = {
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "calls": 0,
        }
        for entry in usage_models:
            for key in totals:
                value = entry.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                totals[key] += value
        usage = Usage(model=self.model, **totals)
        meta = {
            "model": self.model,
            "effort": self.effort,
            "contributor": self.contributor,
            "output_schema": schema_dict,
        }
        return Completion(data=answer, usage=usage, meta=meta)
