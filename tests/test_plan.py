"""Plan tests: schema accept/reject, statistics-only prompt, CLI adapter.

All model contact is faked: :func:`propose` is exercised through a fake
:class:`StructuredModel`, and :class:`MuseCLIModel` through a stubbed
``subprocess.run``. No test here touches the network.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from models.base import Completion, ModelError, Usage
from models.meta_cli import MuseCLIModel
from profiles.hylogger.plan import PLAN_SCHEMA, PlanError, build_prompt, propose
from profiles.hylogger.reader import Sample
from profiles.hylogger.statistics import Profile


def _toy_profile(source_name: str = "TOY") -> Profile:
    samples = [
        Sample(depth_m=10.0, valid=True, swir_group="A", tir_group="X"),
        Sample(depth_m=10.5, valid=True, swir_group="A", tir_group="Y"),
        Sample(depth_m=11.0, valid=False, swir_group=None, tir_group=None),
    ]
    return Profile.from_samples(samples, source_name, "0" * 64)


class FakeModel:
    """A :class:`StructuredModel` double returning a canned answer."""

    def __init__(self, data: Any) -> None:
        self._data = data
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    def complete(self, prompt: str, schema: Any) -> Completion:
        self.prompts.append(prompt)
        self.schemas.append(dict(schema))
        return Completion(
            data=self._data, usage=Usage(model="fake"), meta={"model": "fake"}
        )


def test_propose_accepts_valid_plan() -> None:
    profile = _toy_profile()
    completion = propose(FakeModel({"seed": 446, "n_rows": 100}), profile)
    assert completion.data == {"seed": 446, "n_rows": 100}


@pytest.mark.parametrize(
    "data",
    [
        {"seed": 1},  # missing n_rows
        {"n_rows": 10},  # missing seed
        {"seed": 1, "n_rows": 0},  # n_rows below minimum
        {"seed": 1, "n_rows": -3},  # negative n_rows
        {"seed": 1, "n_rows": 10, "extra": True},  # additional property
        {"seed": "446", "n_rows": 10},  # seed wrong type
        {"seed": 446, "n_rows": 10.0},  # n_rows wrong type
        {"seed": True, "n_rows": 10},  # bool is not an integer seed
        {"seed": 1, "n_rows": False},  # bool is not an integer n_rows
        [1, 2],  # not an object
    ],
)
def test_propose_rejects_nonconforming_plan(data: Any) -> None:
    with pytest.raises(PlanError):
        propose(FakeModel(data), _toy_profile())


def test_propose_asks_for_plan_schema() -> None:
    model = FakeModel({"seed": 7, "n_rows": 3})
    propose(model, _toy_profile())
    assert model.schemas == [PLAN_SCHEMA]


def test_build_prompt_only_source_content_is_statistics() -> None:
    first = build_prompt(_toy_profile("HOLE-A"))
    second = build_prompt(_toy_profile("HOLE-B"))
    stats_first = _toy_profile("HOLE-A").to_json()
    stats_second = _toy_profile("HOLE-B").to_json()
    assert stats_first in first
    assert stats_second in second
    # Stripping the statistics leaves a profile-independent template.
    assert first.replace(stats_first, "") == second.replace(stats_second, "")


class _Proc:
    def __init__(
        self, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _terminal_event(text: str, terminal: str = "completed") -> str:
    return json.dumps(
        {
            "payload_type": "run.terminal.completed",
            "payload": {"kind": "run_terminal", "terminal": terminal, "text": text},
        }
    )


def _usage_event(entries: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "payload_type": "run.usage",
            "payload": {"kind": "run_usage", "models": entries},
        }
    )


def _run_stub(monkeypatch: pytest.MonkeyPatch, proc: _Proc) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def _fake_run(argv: Any, **kwargs: Any) -> _Proc:
        seen["argv"] = argv
        return proc

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return seen


def test_adapter_argv_and_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = "\n".join(
        [
            _usage_event(
                [
                    {
                        "model": "muse-spark-1.3",
                        "input_tokens": 10,
                        "cached_tokens": 1,
                        "output_tokens": 5,
                        "reasoning_tokens": 2,
                        "calls": 1,
                    }
                ]
            ),
            _terminal_event('{"seed": 446, "n_rows": 100}'),
        ]
    )
    seen = _run_stub(monkeypatch, _Proc(stdout=stdout))
    model = MuseCLIModel(model="muse-spark-1.3", effort="max", contributor=True)
    completion = model.complete("make a plan", PLAN_SCHEMA)

    argv = seen["argv"]
    assert argv[argv.index("--contributor-mode") + 1] == "on"
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("--model") + 1] == "muse-spark-1.3"
    assert "--output-schema" in argv
    assert argv[-1] == "make a plan"
    assert completion.data == {"seed": 446, "n_rows": 100}
    assert completion.usage == Usage(
        model="muse-spark-1.3",
        input_tokens=10,
        cached_tokens=1,
        output_tokens=5,
        reasoning_tokens=2,
        calls=1,
    )
    assert completion.meta["model"] == "muse-spark-1.3"
    assert completion.meta["effort"] == "max"
    assert completion.meta["contributor"] is True
    assert completion.meta["output_schema"] == PLAN_SCHEMA


def test_adapter_contributor_off(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _run_stub(
        monkeypatch, _Proc(stdout=_terminal_event('{"seed": 1, "n_rows": 2}'))
    )
    model = MuseCLIModel(contributor=False)
    model.complete("make a plan", PLAN_SCHEMA)
    argv = seen["argv"]
    assert argv[argv.index("--contributor-mode") + 1] == "off"


def test_adapter_aggregates_per_model_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "\n".join(
        [
            _usage_event(
                [
                    {"model": "a", "input_tokens": 3, "output_tokens": 4, "calls": 1},
                    {
                        "model": "b",
                        "input_tokens": 7,
                        "cached_tokens": 2,
                        "reasoning_tokens": 5,
                        "calls": 2,
                    },
                ]
            ),
            _terminal_event('{"seed": 1, "n_rows": 2}'),
        ]
    )
    _run_stub(monkeypatch, _Proc(stdout=stdout))
    completion = MuseCLIModel().complete("make a plan", PLAN_SCHEMA)
    assert completion.usage == Usage(
        model="muse-spark-1.3",
        input_tokens=10,
        cached_tokens=2,
        output_tokens=4,
        reasoning_tokens=5,
        calls=3,
    )


def test_adapter_prefers_structured_output_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "payload_type": "run.output.structured",
                    "payload": {
                        "kind": "structuredOutput",
                        "structuredOutput": {"seed": 9, "n_rows": 11},
                    },
                }
            ),
            _terminal_event('{"seed": 1, "n_rows": 2}'),
        ]
    )
    _run_stub(monkeypatch, _Proc(stdout=stdout))
    completion = MuseCLIModel().complete("make a plan", PLAN_SCHEMA)
    assert completion.data == {"seed": 9, "n_rows": 11}


def test_adapter_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_stub(monkeypatch, _Proc(returncode=2, stderr="boom"))
    with pytest.raises(ModelError):
        MuseCLIModel().complete("make a plan", PLAN_SCHEMA)


def test_adapter_refusal_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_stub(
        monkeypatch, _Proc(stdout=_terminal_event("refused", terminal="refused"))
    )
    with pytest.raises(ModelError):
        MuseCLIModel().complete("make a plan", PLAN_SCHEMA)


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        _terminal_event("not json"),
        _terminal_event("[1, 2]"),
        "this is not jsonl",
    ],
)
def test_adapter_malformed_answer_raises(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    _run_stub(monkeypatch, _Proc(stdout=stdout))
    with pytest.raises(ModelError):
        MuseCLIModel().complete("make a plan", PLAN_SCHEMA)
