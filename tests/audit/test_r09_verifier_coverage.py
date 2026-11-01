"""R09 required-check contracts: only validated passing evidence accepts.

The engine computes the verdict only from check results that belong to
the current attempt, name the exact artifact hash they measured, carry a
recognised status, and cover every required check exactly once. A
verifier that records nothing fails coverage; a result naming the wrong
artifact hash is rejected as invalid. Neither can reach acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.core.manifest import validate_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute


def _write_source(path: Path) -> Path:
    csv_path = path / "sample-tabular.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (path / "sample-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "r09-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _manifest():
    return validate_manifest(
        {
            "id": "fake",
            "version": 1,
            "engine_api": 1,
            "source": {},
            "capabilities": {
                "generate": {"impl": "fake.generate"},
                "validate": {"impl": "fake.validate", "writes_verdict": True},
                "repair": {"impl": "fake.repair"},
            },
            "graph": [
                {
                    "loop": ["generate", "validate"],
                    "on_fail": "repair",
                    "max_attempts": "max_attempts",
                },
                {"gate": "human_review"},
            ],
            "checks": [
                {
                    "id": "required_check",
                    "kind": "constraint",
                    "threshold": {"source": "literal"},
                }
            ],
        }
    )


def _registry(validate: Any) -> dict[str, Any]:
    return {
        "generate": lambda ctx: ctx.record_artifact(
            "data.csv", "text/csv", b"unchecked"
        ),
        "validate": validate,
        "repair": lambda ctx: None,
    }


def test_noop_verifier_must_not_release(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    code, result = execute(
        _manifest(),
        _registry(lambda ctx: None),
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: None,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code != EXIT_OK or result["status"] != "released"


def test_recorded_failure_blocks_release(tmp_path: Path) -> None:
    """Control: a verifier that records a failing check blocks release,
    so the defect above is missing coverage, not a broken gate."""

    def validate(ctx: Any) -> None:
        ctx.record_checks(
            [
                {
                    "check_id": "required_check",
                    "kind": "constraint",
                    "status": "fail",
                    "measured": "no data examined",
                    "threshold": "must examine data",
                    "threshold_source": "literal",
                    "detail": "control failure",
                }
            ]
        )

    source = _write_source(tmp_path)
    code, result = execute(
        _manifest(),
        _registry(validate),
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: None,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code != EXIT_OK
    assert result["status"] != "released"


def test_wrong_artifact_hash_is_rejected(tmp_path: Path) -> None:
    """A result naming an artifact hash it did not measure is invalid."""

    def validate(ctx: Any) -> None:
        ctx.record_checks(
            [
                {
                    "check_id": "required_check",
                    "kind": "constraint",
                    "status": "pass",
                    "measured": "ok",
                    "threshold": {"source": "literal"},
                    "threshold_source": "literal",
                    "artifact_sha256": "0" * 64,
                    "detail": "stale proof for another artifact",
                }
            ]
        )

    source = _write_source(tmp_path)
    code, result = execute(
        _manifest(),
        _registry(validate),
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: None,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    assert result["status"] != "released"


def test_explicit_correct_hash_is_accepted(tmp_path: Path) -> None:
    """A result naming the exact artifact hash it measured satisfies."""

    def validate(ctx: Any) -> None:
        sha = ctx.latest_artifact().sha256
        ctx.record_checks(
            [
                {
                    "check_id": "required_check",
                    "kind": "constraint",
                    "status": "pass",
                    "measured": "ok",
                    "threshold": {"source": "literal"},
                    "threshold_source": "literal",
                    "artifact_sha256": sha,
                    "detail": "measured this artifact",
                }
            ]
        )

    source = _write_source(tmp_path)
    code, result = execute(
        _manifest(),
        _registry(validate),
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: None,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_OK
    assert result["status"] == "released"
