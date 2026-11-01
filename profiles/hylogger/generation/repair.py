"""Repair loop: retry generation with model-proposed plan fixes.

Each attempt generates synthetic bytes from a plan, reads them back, and
runs the deterministic checks. On failure (and while attempts remain) the
model is asked for a revised plan; the repair prompt carries only the
failed :class:`CheckResult` JSON, the current plan, and the profile
statistics -- never raw source rows or depths. Revised plans are validated
with :func:`plan.validate_plan`. Exhaustion returns the last failing
report unchanged; a failing verdict is never converted to pass.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from models.base import StructuredModel, Usage

from ..validation.checks import run_checks as _run_checks
from ..validation.dataset import read as _read_artifact
from .generate import generate as _generate_artifact
from . import plan as plan_module
from ..analysis.calibration import Thresholds
from ..validation.checks import CheckReport, CheckResult
from ..intake.reader import Sample
from ..analysis.statistics import Profile


@dataclass(frozen=True)
class Attempt:
    """One generation attempt and its check outcome."""

    plan: dict[str, Any]
    artifact_sha256: str
    verdict: str
    failed: list[str] = field(default_factory=list)
    usage: Usage | None = None


@dataclass
class RepairResult:
    """Final plan, bytes, report, and per-attempt history."""

    plan: dict[str, Any]
    artifact_bytes: bytes
    report: CheckReport
    history: list[Attempt] = field(default_factory=list)


def build_repair_prompt(
    profile: Profile,
    failed: Sequence[CheckResult],
    current_plan: Mapping[str, Any],
) -> str:
    """Build the repair prompt for failed checks.

    Only failed-check JSON, the current plan, and profile statistics are
    included; no raw source rows or depths are ever embedded.
    """
    failed_payload = [
        {"name": result.name, "status": result.status, "detail": result.detail}
        for result in failed
    ]
    return (
        "The previous generation plan failed checks. Propose a revised plan.\n"
        "Reply with a JSON object with exactly two keys:\n"
        '- "seed": any integer (the deterministic seed for the generator).\n'
        '- "n_rows": a positive integer count of rows to generate.\n'
        "Failed checks (JSON):\n"
        + json.dumps(failed_payload, sort_keys=True, indent=2)
        + "\nCurrent plan (JSON):\n"
        + json.dumps(dict(current_plan), sort_keys=True)
        + "\nProfile statistics (the only source-derived content):\n"
        + profile.to_json()
    )


def repair(
    profile: Profile,
    source_samples: Sequence[Sample],
    thresholds: Thresholds,
    initial_plan: Mapping[str, Any],
    model: StructuredModel,
    *,
    max_attempts: int,
) -> RepairResult:
    """Generate, check, and repair a synthetic hole.

    ``max_attempts`` is required and has no default; it bounds the total
    number of generation attempts (initial plan plus repairs). The first
    attempt uses ``initial_plan`` without calling the model. Each failing
    attempt (while attempts remain) triggers one model call for a revised
    plan, validated with :func:`plan.validate_plan`.
    """
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise TypeError(
            f"max_attempts must be an integer, got {max_attempts!r}"
        )
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
    if not isinstance(profile, Profile):
        raise TypeError(
            f"profile must be a Profile, got {type(profile).__name__}"
        )
    current_plan = plan_module.validate_plan(initial_plan)

    source_list = list(source_samples)
    history: list[Attempt] = []
    pending_usage: Usage | None = None

    artifact_bytes = b""
    report: CheckReport | None = None

    for attempt_number in range(1, max_attempts + 1):
        artifact_bytes = _generate_artifact(profile, current_plan)
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        samples = _read_artifact(artifact_bytes)
        report = _run_checks(
            samples,
            thresholds,
            source_samples=source_list,
            artifact_name=f"{profile.source_name}:attempt-{attempt_number}",
            artifact_sha256=artifact_sha256,
        )
        history.append(
            Attempt(
                plan=dict(current_plan),
                artifact_sha256=artifact_sha256,
                verdict=report.verdict,
                failed=list(report.failed),
                usage=pending_usage,
            )
        )
        pending_usage = None
        if report.verdict == "pass":
            return RepairResult(
                plan=dict(current_plan),
                artifact_bytes=artifact_bytes,
                report=report,
                history=history,
            )
        if attempt_number == max_attempts:
            break
        failed_results = [r for r in report.results if r.status == "fail"]
        prompt = build_repair_prompt(profile, failed_results, current_plan)
        completion = model.complete(prompt, plan_module.PLAN_SCHEMA)
        current_plan = plan_module.validate_plan(completion.data)
        pending_usage = completion.usage

    assert report is not None
    return RepairResult(
        plan=dict(current_plan),
        artifact_bytes=artifact_bytes,
        report=report,
        history=history,
    )
