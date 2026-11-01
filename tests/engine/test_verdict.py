"""Required-check contracts: coverage decides, only passing evidence accepts.

Unit tests for the engine-owned verdict: every applicable required check
covered exactly once with status ``pass``. Missing, duplicated,
unknown, erroneous, unsupported, or skipped required checks block
acceptance; invalid evidence (unknown ids, kind mismatches,
unrecognised statuses, wrong artifact hashes) is rejected loudly.
"""

from __future__ import annotations

import pytest

from engine.core.manifest import ENGINE_API, validate_manifest
from engine.core.records import EngineCheckResult
from engine.core.runner import RunnerError
from engine.core.verdict import (
    applicable_required_ids,
    decide,
    unconditional_required_ids,
)

ARTIFACT = "a" * 64
OTHER = "b" * 64


def _checks() -> list[dict]:
    return [
        {
            "id": "shape_ok",
            "kind": "constraint",
            "threshold": {"source": "literal"},
            "required": True,
        },
        {
            "id": "content_ok",
            "kind": "fidelity",
            "threshold": {"source": "relative_tolerance"},
            "required": True,
        },
        {
            "id": "image_ok",
            "kind": "fidelity",
            "threshold": {"source": "literal"},
            "required": True,
            "applicable_when": "images_present",
        },
    ]


def _result(
    check_id: str,
    kind: str,
    status: str,
    sha: str = ARTIFACT,
) -> EngineCheckResult:
    return EngineCheckResult(
        check_id=check_id,
        kind=kind,
        status=status,
        measured="ok",
        threshold={"source": "literal"},
        threshold_source="literal",
        artifact_sha256=sha,
        verifier="judge",
        attempt=1,
    )


def _required() -> list[str]:
    return ["content_ok", "shape_ok"]


def test_all_pass_accepts() -> None:
    verdict, failed = decide(
        [_result("shape_ok", "constraint", "pass"),
         _result("content_ok", "fidelity", "pass")],
        _checks(),
        required_ids=_required(),
        known_artifacts=[ARTIFACT],
    )
    assert verdict == "pass"
    assert failed == []


def test_missing_required_blocks() -> None:
    verdict, failed = decide(
        [_result("shape_ok", "constraint", "pass")],
        _checks(),
        required_ids=_required(),
        known_artifacts=[ARTIFACT],
    )
    assert verdict == "fail"
    assert failed == ["content_ok"]


def test_empty_results_block() -> None:
    verdict, failed = decide(
        [],
        _checks(),
        required_ids=_required(),
        known_artifacts=[ARTIFACT],
    )
    assert verdict == "fail"
    assert failed == ["content_ok", "shape_ok"]


def test_empty_required_set_cannot_pass() -> None:
    verdict, _ = decide(
        [_result("shape_ok", "constraint", "pass")],
        _checks(),
        required_ids=[],
        known_artifacts=[ARTIFACT],
    )
    assert verdict == "fail"


@pytest.mark.parametrize(
    "status", ["fail", "error", "unsupported", "skipped"]
)
def test_only_pass_satisfies(status: str) -> None:
    verdict, failed = decide(
        [_result("shape_ok", "constraint", status),
         _result("content_ok", "fidelity", "pass")],
        _checks(),
        required_ids=_required(),
        known_artifacts=[ARTIFACT],
    )
    assert verdict == "fail"
    assert failed == ["shape_ok"]


def test_duplicate_coverage_rejected() -> None:
    with pytest.raises(RunnerError, match="exactly once"):
        decide(
            [_result("shape_ok", "constraint", "pass"),
             _result("shape_ok", "constraint", "pass"),
             _result("content_ok", "fidelity", "pass")],
            _checks(),
            required_ids=_required(),
            known_artifacts=[ARTIFACT],
        )


def test_unknown_check_rejected() -> None:
    with pytest.raises(RunnerError, match="unknown check"):
        decide(
            [_result("shape_ok", "constraint", "pass"),
             _result("content_ok", "fidelity", "pass"),
             _result("invented", "constraint", "pass")],
            _checks(),
            required_ids=_required(),
            known_artifacts=[ARTIFACT],
        )


def test_kind_mismatch_rejected() -> None:
    with pytest.raises(RunnerError, match="does not match"):
        decide(
            [_result("shape_ok", "fidelity", "pass"),
             _result("content_ok", "fidelity", "pass")],
            _checks(),
            required_ids=_required(),
            known_artifacts=[ARTIFACT],
        )


def test_unrecognised_status_rejected() -> None:
    with pytest.raises(RunnerError, match="not recognised"):
        decide(
            [_result("shape_ok", "constraint", "skipped_typo"),
             _result("content_ok", "fidelity", "pass")],
            _checks(),
            required_ids=_required(),
            known_artifacts=[ARTIFACT],
        )


def test_unknown_artifact_hash_rejected() -> None:
    with pytest.raises(RunnerError, match="unknown artifact"):
        decide(
            [_result("shape_ok", "constraint", "pass", sha=OTHER),
             _result("content_ok", "fidelity", "pass")],
            _checks(),
            required_ids=_required(),
            known_artifacts=[ARTIFACT],
        )


def test_out_of_phase_hash_rejected() -> None:
    with pytest.raises(RunnerError, match="outside the current attempt"):
        decide(
            [_result("shape_ok", "constraint", "pass", sha=OTHER),
             _result("content_ok", "fidelity", "pass")],
            _checks(),
            required_ids=_required(),
            known_artifacts=[ARTIFACT, OTHER],
            phase_artifacts=[ARTIFACT],
        )


def test_conditional_checks_apply_only_when_met() -> None:
    assert applicable_required_ids(
        _checks(), conditions={"images_present": False}
    ) == ["shape_ok", "content_ok"]
    assert applicable_required_ids(
        _checks(), conditions={"images_present": True}
    ) == ["shape_ok", "content_ok", "image_ok"]


def test_unconditional_ids_exclude_conditional() -> None:
    assert unconditional_required_ids(_checks()) == [
        "shape_ok",
        "content_ok",
    ]


def test_required_defaults_to_true() -> None:
    manifest = validate_manifest(
        {
            "id": "fake",
            "version": 1,
            "engine_api": ENGINE_API,
            "source": {},
            "capabilities": {"judge": {"impl": "fake.judge"}},
            "graph": [{"step": "judge"}, {"gate": "review"}],
            "checks": [
                {
                    "id": "shape_ok",
                    "kind": "constraint",
                    "threshold": {"source": "literal"},
                }
            ],
            "models": {},
            "limits": {},
        }
    )
    assert unconditional_required_ids(manifest.checks) == ["shape_ok"]


def test_required_must_be_boolean() -> None:
    from engine.core.manifest import ManifestError

    with pytest.raises(ManifestError, match="'required'"):
        validate_manifest(
            {
                "id": "fake",
                "version": 1,
                "engine_api": ENGINE_API,
                "source": {},
                "capabilities": {"judge": {"impl": "fake.judge"}},
                "graph": [{"step": "judge"}, {"gate": "review"}],
                "checks": [
                    {
                        "id": "shape_ok",
                        "kind": "constraint",
                        "threshold": {"source": "literal"},
                        "required": "yes",
                    }
                ],
                "models": {},
                "limits": {},
            }
        )


def test_unknown_applicability_condition_rejected() -> None:
    from engine.core.manifest import ManifestError

    with pytest.raises(ManifestError, match="applicable_when"):
        validate_manifest(
            {
                "id": "fake",
                "version": 1,
                "engine_api": ENGINE_API,
                "source": {},
                "capabilities": {"judge": {"impl": "fake.judge"}},
                "graph": [{"step": "judge"}, {"gate": "review"}],
                "checks": [
                    {
                        "id": "shape_ok",
                        "kind": "constraint",
                        "threshold": {"source": "literal"},
                        "applicable_when": "tides_aligned",
                    }
                ],
                "models": {},
                "limits": {},
            }
        )
