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
from profiles.hylogger.generation.plan import provider_schema
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
    assert model.schemas == [provider_schema(None)]
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


def test_repair_prompt_carries_only_supported_options() -> None:
    source = _source()
    profile = _profile(source)
    failed = [
        result for result in _run_checks_for_prompt(source, profile)
        if result.status == "fail"
    ]
    assert failed != []
    options = {"strategies": ["fitted_markov_joint"], "max_rows": 200000}
    prompt = repair_module.build_repair_prompt(
        profile, failed, {"seed": 1, "n_rows": 8}, options
    )
    for result in failed:
        assert result.name in prompt
    assert "fitted_markov_joint" in prompt
    assert "200000" in prompt
    assert profile.to_disclosed_json() in prompt
    for sample in source:
        assert repr(sample.depth_m) not in prompt


def _run_checks_for_prompt(source, profile):
    from profiles.hylogger.validation.checks import run_checks as _checks
    from profiles.hylogger.validation.dataset import read as _read
    from profiles.hylogger.generation.generate import generate as _generate

    payload = _generate(profile, {"seed": 0, "n_rows": 8})
    report = _checks(
        _read(payload),
        _thresholds(),
        source_samples=list(source),
        artifact_name="TOY:attempt-1",
        artifact_sha256="0" * 64,
    )
    return report.results


def test_repair_sends_strict_schema_with_bound_strategies() -> None:
    """The repair wire schema requires every declared property.

    Regression test for the live 400 on the repair path: the schema
    captured from the model call must be strict.
    """
    source = _source()
    profile = _profile(source)
    thresholds = _thresholds()
    model = FakeModel([{"seed": 8, "n_rows": 8, "strategy": "fitted_markov_joint"}])
    result = repair_module.repair(
        profile,
        source,
        thresholds,
        {"seed": 0, "n_rows": 8},
        model,
        max_attempts=2,
        allowed_strategies=["fitted_markov_joint"],
        supported_options={
            "strategies": ["fitted_markov_joint"],
            "max_rows": 200000,
        },
    )
    assert model.calls == 1
    (sent,) = model.schemas
    assert set(sent["required"]) == set(sent["properties"])
    assert set(sent["properties"]) == {"seed", "n_rows", "strategy"}
    assert result.plan["strategy"] == "fitted_markov_joint"


def test_repair_rejects_disallowed_revised_strategy() -> None:
    source = _source()
    profile = _profile(source)
    thresholds = _thresholds()
    model = FakeModel([{"seed": 8, "n_rows": 8, "strategy": "nope"}])
    with pytest.raises(repair_module.plan_module.PlanError):
        repair_module.repair(
            profile,
            source,
            thresholds,
            {"seed": 0, "n_rows": 8},
            model,
            max_attempts=2,
            allowed_strategies=["fitted_markov_joint"],
        )
    assert model.calls == 1
