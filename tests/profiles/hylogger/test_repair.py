"""Repair-loop tests: pass-after-repair, exhaustion, prompt hygiene.

All model contact is faked with a queue-based :class:`StructuredModel`
double. No test here touches the network. Fixtures are small synthetic
holes in the style of ``tests/test_checks.py``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

import pytest

from models.base import Completion, Usage
from profiles.hylogger.generation import repair as repair_module
from profiles.hylogger.analysis.calibration import Band, Thresholds
from profiles.hylogger.generation.plan import PLAN_SCHEMA
from profiles.hylogger.intake.reader import Sample
from profiles.hylogger.analysis.statistics import Profile


def _thresholds() -> Thresholds:
    return Thresholds(
        bands={
            "spacing_median_m": Band("spacing_median_m", 0.005, 0.01),
            "repeated_depth_fraction": Band("repeated_depth_fraction", 0.0, 0.2),
            "valid_fraction": Band("valid_fraction", 0.5, 1.0),
            "swir_answer_rate": Band("swir_answer_rate", 0.0, 1.0, vacuous=True),
            "tir_answer_rate": Band("tir_answer_rate", 0.9, 1.0),
            "repeated_valid_fraction": Band(
                "repeated_valid_fraction", 0.0, 0.05
            ),
            "backward_steps": Band("backward_steps", 0, 0),
        },
        swir_vocabulary=frozenset({"A", "B"}),
        tir_vocabulary=frozenset({"X", "Y"}),
    )


def _source() -> list[Sample]:
    base = 123.456789
    step = 0.0078
    coverage: list[tuple[str | None, str | None]] = [
        ("A", "X"),
        ("B", None),
        (None, "Y"),
        ("A", "Y"),
        (None, None),
        ("B", "X"),
        (None, None),
        ("A", "X"),
    ]
    valids = [True, True, True, True, False, True, True, True]
    return [
        Sample(
            depth_m=base + i * step,
            valid=valids[i],
            swir_group=coverage[i][0],
            tir_group=coverage[i][1],
        )
        for i in range(8)
    ]


def _profile(source: Sequence[Sample]) -> Profile:
    return Profile.from_samples(source, "TOY", "0" * 64)


class FakeModel:
    """A :class:`StructuredModel` double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.prompts: list[str] = []
        self.schemas: list[dict] = []
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        self.prompts.append(prompt)
        self.schemas.append(dict(schema))
        data = dict(self._plans.pop(0))
        return Completion(
            data=data,
            usage=Usage(
                model="fake",
                input_tokens=10,
                output_tokens=5,
                calls=1,
            ),
            meta={"model": "fake"},
        )


def test_repair_passes_on_second_attempt() -> None:
    source = _source()
    profile = _profile(source)
    thresholds = _thresholds()
    model = FakeModel([{"seed": 8, "n_rows": 8}])

    result = repair_module.repair(
        profile,
        source,
        thresholds,
        {"seed": 0, "n_rows": 8},
        model,
        max_attempts=2,
    )

    assert result.report.verdict == "pass"
    assert result.plan == {"seed": 8, "n_rows": 8}
    assert len(result.history) == 2
    assert result.history[0].verdict == "fail"
    assert result.history[0].usage is None
    assert result.history[0].failed != []
    assert result.history[1].verdict == "pass"
    assert result.history[1].failed == []
    assert result.history[1].usage is not None
    assert result.history[1].usage.calls == 1
    assert model.calls == 1
    assert model.schemas == [PLAN_SCHEMA]
    for attempt in result.history:
        assert len(attempt.artifact_sha256) == 64
    assert result.report.artifact_sha256 == result.history[-1].artifact_sha256
    assert result.artifact_bytes is not None
    assert (
        hashlib.sha256(result.artifact_bytes).hexdigest()
        == result.report.artifact_sha256
    )


def test_exhaustion_returns_fail_without_repair_call() -> None:
    source = _source()
    profile = _profile(source)
    thresholds = _thresholds()
    model = FakeModel([{"seed": 8, "n_rows": 8}])

    result = repair_module.repair(
        profile,
        source,
        thresholds,
        {"seed": 0, "n_rows": 8},
        model,
        max_attempts=1,
    )

    assert result.report.verdict == "fail"
    assert result.report.failed != []
    assert result.plan == {"seed": 0, "n_rows": 8}
    assert len(result.history) == 1
    assert result.history[0].verdict == "fail"
    assert model.calls == 0


def test_repair_prompt_carries_failures_but_no_raw_depths() -> None:
    source = _source()
    profile = _profile(source)
    thresholds = _thresholds()
    model = FakeModel([{"seed": 8, "n_rows": 8}])

    result = repair_module.repair(
        profile,
        source,
        thresholds,
        {"seed": 0, "n_rows": 8},
        model,
        max_attempts=2,
    )

    assert model.calls == 1
    prompt = model.prompts[0]
    failed_names = result.history[0].failed
    assert failed_names != []
    for name in failed_names:
        assert name in prompt
    raw_depth = repr(source[0].depth_m)
    assert raw_depth == "123.456789"
    assert raw_depth not in prompt
    assert raw_depth not in profile.to_json()
    for sample in source:
        assert repr(sample.depth_m) not in prompt


def test_max_attempts_is_required_keyword() -> None:
    source = _source()
    profile = _profile(source)
    thresholds = _thresholds()
    model = FakeModel([])
    with pytest.raises(TypeError):
        repair_module.repair(  # type: ignore[call-arg]
            profile,
            source,
            thresholds,
            {"seed": 0, "n_rows": 8},
            model,
        )
