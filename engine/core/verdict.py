"""Required-check contracts: verdicts computed only from check results.

The engine privately owns acceptance. A run is accepted only when every
applicable required check declared in the manifest is covered exactly
once by a valid result with status ``"pass"``. Anything else blocks
acceptance loudly:

* invalid evidence (unknown check id, kind mismatch, unrecognised
  status, unknown or mismatched artifact hash, duplicated coverage) is
  rejected with :class:`RunnerError <engine.core.runner.RunnerError>`;
* insufficient evidence (missing required checks, a required check with
  any status other than ``"pass"``, or an empty required set) fails the
  verdict, so the gate refuses release.

Checks may declare ``"required": false`` to opt out of the contract
(the default is required: fail closed), and ``"applicable_when"`` to
gate applicability on an engine-owned run fact (currently only
``"images_present"``, true once image reference statistics are
registered). Conditional checks never drive loop repair decisions --
they are decided at final acceptance -- because their applicability
evidence may only arrive after the loop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .records import VALID_CHECK_STATUSES, EngineCheckResult

#: Applicability condition: image reference statistics were registered.
CONDITION_IMAGES_PRESENT = "images_present"

#: Every applicability condition the engine resolves.
APPLICABILITY_CONDITIONS = (CONDITION_IMAGES_PRESENT,)


def is_required(entry: Mapping[str, Any]) -> bool:
    """Whether a manifest check entry participates in acceptance."""
    return bool(entry.get("required", True))


def applicability(entry: Mapping[str, Any]) -> str | None:
    """The applicability condition id, or ``None`` when always applicable."""
    condition = entry.get("applicable_when")
    if condition is None:
        return None
    return str(condition)


def applicable_required_ids(
    checks: Sequence[Mapping[str, Any]],
    *,
    conditions: Mapping[str, bool],
) -> list[str]:
    """Ids of required checks applicable under ``conditions``.

    A check with no ``applicable_when`` is always applicable; a check
    with one is applicable only when its condition holds.
    """
    ids: list[str] = []
    for entry in checks:
        if not is_required(entry):
            continue
        condition = applicability(entry)
        if condition is not None and not conditions.get(condition, False):
            continue
        check_id = entry.get("id")
        if isinstance(check_id, str) and check_id:
            ids.append(check_id)
    return ids


def unconditional_required_ids(
    checks: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Ids of required checks with no applicability condition.

    These are the only checks that drive per-attempt loop decisions.
    """
    return [
        str(entry["id"])
        for entry in checks
        if is_required(entry) and applicability(entry) is None
    ]


def _declared_by_id(
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(entry["id"]): entry
        for entry in checks
        if isinstance(entry.get("id"), str) and entry["id"]
    }


def validate_results(
    results: Sequence[EngineCheckResult],
    checks: Sequence[Mapping[str, Any]],
    *,
    known_artifacts: Sequence[str],
    phase_artifacts: Sequence[str] | None = None,
    where: str = "verdict",
) -> None:
    """Reject invalid check evidence loudly.

    Every result must name a declared check with a matching kind, carry
    a recognised status, and name a known artifact hash; when
    ``phase_artifacts`` is given the hash must belong to the current
    phase. Raises ``RunnerError`` (imported lazily to keep the layering
    one-directional: this module never imports the runner).
    """
    from .runner import RunnerError

    declared = _declared_by_id(checks)
    known = set(known_artifacts)
    phase = set(phase_artifacts) if phase_artifacts is not None else None
    for result in results:
        entry = declared.get(result.check_id)
        if entry is None:
            raise RunnerError(
                f"{where}: unknown check {result.check_id!r} recorded by "
                f"{result.verifier!r}: refusing (no release)"
            )
        if result.kind != entry.get("kind"):
            raise RunnerError(
                f"{where}: check {result.check_id!r} kind "
                f"{result.kind!r} does not match declared kind "
                f"{entry.get('kind')!r}: refusing (no release)"
            )
        if result.status not in VALID_CHECK_STATUSES:
            raise RunnerError(
                f"{where}: check {result.check_id!r} status "
                f"{result.status!r} is not recognised: refusing (no release)"
            )
        if result.artifact_sha256 not in known:
            raise RunnerError(
                f"{where}: check {result.check_id!r} names unknown artifact "
                f"{result.artifact_sha256!r}: refusing (no release)"
            )
        if phase is not None and result.artifact_sha256 not in phase:
            raise RunnerError(
                f"{where}: check {result.check_id!r} measures artifact "
                f"{result.artifact_sha256!r} outside the current attempt: "
                "refusing (no release)"
            )


def decide(
    results: Sequence[EngineCheckResult],
    checks: Sequence[Mapping[str, Any]],
    *,
    required_ids: Sequence[str],
    known_artifacts: Sequence[str],
    phase_artifacts: Sequence[str] | None = None,
    where: str = "verdict",
) -> tuple[str, list[str]]:
    """Decide pass/fail from validated coverage.

    Returns ``(verdict, failed)`` where ``failed`` lists every required
    check id that is not satisfied by exactly one ``"pass"`` result.
    Duplicated coverage is invalid evidence and raises. An empty
    required set can never pass.
    """
    from .runner import RunnerError

    validate_results(
        results,
        checks,
        known_artifacts=known_artifacts,
        phase_artifacts=phase_artifacts,
        where=where,
    )
    by_id: dict[str, list[EngineCheckResult]] = {}
    for result in results:
        by_id.setdefault(result.check_id, []).append(result)
    for check_id, group in by_id.items():
        if len(group) != 1:
            raise RunnerError(
                f"{where}: check {check_id!r} covered {len(group)} times, "
                "exactly once is required: refusing (no release)"
            )
    required = list(required_ids)
    if not required:
        return "fail", []
    failed = [
        check_id
        for check_id in sorted(required)
        if not (check_id in by_id and by_id[check_id][0].status == "pass")
    ]
    if failed:
        return "fail", failed
    return "pass", []
