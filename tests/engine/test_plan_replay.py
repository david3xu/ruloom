"""Fixed-plan replay tests: deterministic data without a fresh planner call.

Replaying a saved ``plan.json`` via ``ruloom run --plan PLAN`` reuses the
exact saved seed/row count with no planner model call, so two replays of
one fixed plan yield byte-identical ``data.csv`` bytes. The release ZIP is
explicitly NOT the reproducibility comparator: it differs across runs by
construction (per-run identity and log), while the data digest matches.

All model contact is faked with a queue-based ``StructuredModel`` double.
All sources here are TOY fixture evidence (small synthetic CSVs), labelled
as such; no test here touches the network.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from engine import cli as cli_module
from engine.audit.runlog import RunLog
from models.base import Completion, Usage
from profiles.hylogger.analysis.calibration import Band, Thresholds

BASE_DEPTH = 123.456789
STEP = 0.0078
COVERAGE: list[tuple[str, str, str]] = [
    ("1", "A", "X"),
    ("1", "B", ""),
    ("1", "", "Y"),
    ("1", "A", "Y"),
    ("0", "", ""),
    ("1", "B", "X"),
    ("0", "", ""),
    ("1", "A", "X"),
]


def _write_hylogger_source(tmp_path: Path, stem: str = "TOY-hole-scalars") -> Path:
    """TOY fixture source (labelled fixture evidence, not measured data)."""
    csv_path = tmp_path / f"{stem}.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i, (mask, swir, tir) in enumerate(COVERAGE):
        lines.append(f"{BASE_DEPTH + i * STEP},{mask},{swir},{tir}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = tmp_path / f"{stem[: -len('-scalars')]}-source.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_name": "TOY-hole",
                "licence": "CC-BY-4.0",
                "attribution": "Toy attribution for tests.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _write_hylogger_thresholds(tmp_path: Path) -> Path:
    thresholds = Thresholds(
        bands={
            "spacing_median_m": Band("spacing_median_m", 0.005, 0.01),
            "repeated_depth_fraction": Band("repeated_depth_fraction", 0.0, 0.2),
            "valid_fraction": Band("valid_fraction", 0.5, 1.0),
            "swir_answer_rate": Band("swir_answer_rate", 0.0, 1.0, vacuous=True),
            "tir_answer_rate": Band("tir_answer_rate", 0.9, 1.0),
            "repeated_valid_fraction": Band("repeated_valid_fraction", 0.0, 0.05),
            "backward_steps": Band("backward_steps", 0, 0),
        },
        swir_vocabulary=frozenset({"A", "B"}),
        tir_vocabulary=frozenset({"X", "Y"}),
    )
    path = tmp_path / "thresholds.json"
    path.write_text(
        json.dumps(thresholds.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _write_tabular_source(tmp_path: Path) -> Path:
    """TOY tabular fixture source (labelled fixture evidence)."""
    columns = ["id", "value", "label"]
    rows = [
        {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
        for i in range(20)
    ]
    csv_path = tmp_path / "orders-tabular.csv"
    lines = [",".join(columns)]
    lines += [",".join(row[col] for col in columns) for row in rows]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "orders-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "orders-demo",
                "licence": "CC-BY-4.0",
                "attribution": "Demo fixture for tests.",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "tabular-thresholds.json").write_text(
        json.dumps({"mean_relative_tolerance": 0.25}) + "\n", encoding="utf-8"
    )
    return csv_path


class FakeModel:
    """A ``StructuredModel`` double replaying queued plans (fixture only)."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=10, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


class RefusingModel:
    """A model double that fails the test if it is ever called."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Any:
        self.calls += 1
        raise AssertionError("fixed-plan replay must not call the planner model")


def _run_dirs(out: Path) -> list[Path]:
    return [
        child for child in (out / "runs").iterdir() if (child / "run.json").is_file()
    ]


def _run_hylogger(
    monkeypatch: pytest.MonkeyPatch,
    csv_path: Path,
    out_dir: Path,
    model: Any,
    extra_argv: Sequence[str] | None = None,
) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nReplay fixture.\n"))
    argv = [
        "run",
        str(csv_path),
        "--max-attempts",
        "2",
        "--out",
        str(out_dir),
    ]
    if extra_argv:
        argv.extend(extra_argv)
    return cli_module.main(argv, model=model)


def test_fixed_plan_replays_identical_data_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replays of one saved plan yield identical data, no planner call."""
    csv_path = _write_hylogger_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_hylogger_thresholds(tmp_path)))
    seed_dir = tmp_path / "out-seed"
    code = _run_hylogger(
        monkeypatch, csv_path, seed_dir, FakeModel([{"seed": 8, "n_rows": 8}])
    )
    assert code == 0
    seed_runs = _run_dirs(seed_dir)
    assert len(seed_runs) == 1
    saved_plan = seed_runs[0] / "plan.json"
    assert json.loads(saved_plan.read_text(encoding="utf-8")) == {
        "seed": 8,
        "n_rows": 8,
        "strategy": "fitted_markov_joint",
    }

    replay_dirs: list[Path] = []
    for name in ("out-replay-1", "out-replay-2"):
        out_dir = tmp_path / name
        model = RefusingModel()
        code = _run_hylogger(
            monkeypatch, csv_path, out_dir, model, ["--plan", str(saved_plan)]
        )
        assert code == 0
        assert model.calls == 0
        run_dirs = _run_dirs(out_dir)
        assert len(run_dirs) == 1
        replay_dirs.append(run_dirs[0])

    first_data = (replay_dirs[0] / "data.csv").read_bytes()
    second_data = (replay_dirs[1] / "data.csv").read_bytes()
    assert first_data == second_data
    assert (
        hashlib.sha256(first_data).hexdigest()
        == hashlib.sha256(second_data).hexdigest()
    )
    assert (replay_dirs[0] / "seed.txt").read_text(encoding="utf-8") == "8\n"
    log = RunLog.read_jsonl(replay_dirs[0] / "run.jsonl")
    assert log.verify()
    plan_events = [e for e in log.events if e["stage"] == "plan"]
    assert plan_events and plan_events[0]["details"].get("replay") is True


def test_release_zip_is_not_the_reproducibility_comparator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identical replay data still ships differing per-run ZIPs by design."""
    csv_path = _write_hylogger_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_hylogger_thresholds(tmp_path)))
    seed_dir = tmp_path / "out-seed"
    code = _run_hylogger(
        monkeypatch, csv_path, seed_dir, FakeModel([{"seed": 8, "n_rows": 8}])
    )
    assert code == 0
    saved_plan = _run_dirs(seed_dir)[0] / "plan.json"

    run_dirs: list[Path] = []
    for name in ("out-a", "out-b"):
        out_dir = tmp_path / name
        code = _run_hylogger(
            monkeypatch, csv_path, out_dir, RefusingModel(), ["--plan", str(saved_plan)]
        )
        assert code == 0
        run_dirs.append(_run_dirs(out_dir)[0])

    assert (run_dirs[0] / "data.csv").read_bytes() == (
        run_dirs[1] / "data.csv"
    ).read_bytes()
    first_zip = (run_dirs[0] / "release.zip").read_bytes()
    second_zip = (run_dirs[1] / "release.zip").read_bytes()
    assert (
        hashlib.sha256(first_zip).hexdigest() != hashlib.sha256(second_zip).hexdigest()
    )


def test_invalid_plan_file_refused_without_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-object plan file is refused before any model call."""
    csv_path = _write_hylogger_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_hylogger_thresholds(tmp_path)))
    bad_plan = tmp_path / "bad-plan.json"
    bad_plan.write_text(json.dumps([1, 2, 3]) + "\n", encoding="utf-8")
    out_dir = tmp_path / "out-bad"
    model = FakeModel([{"seed": 8, "n_rows": 8}])
    code = _run_hylogger(
        monkeypatch, csv_path, out_dir, model, ["--plan", str(bad_plan)]
    )
    assert code != 0
    assert model.calls == 0
    run_dirs = _run_dirs(out_dir)
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"


def test_schema_invalid_fixed_plan_refused_before_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-invalid fixed plan fails before generation, no model call."""
    csv_path = _write_hylogger_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_hylogger_thresholds(tmp_path)))
    bad_plan = tmp_path / "schema-bad-plan.json"
    bad_plan.write_text(json.dumps({"n_rows": 8}) + "\n", encoding="utf-8")
    out_dir = tmp_path / "out-schema-bad"
    model = RefusingModel()
    code = _run_hylogger(
        monkeypatch, csv_path, out_dir, model, ["--plan", str(bad_plan)]
    )
    assert code != 0
    assert model.calls == 0
    run_dirs = _run_dirs(out_dir)
    assert len(run_dirs) == 1


def test_tabular_fixed_plan_replay_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed-plan mechanism is generic: tabular replays match too."""
    csv_path = _write_tabular_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(tmp_path / "tabular-thresholds.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nReplay fixture.\n"))

    def run_tabular(out: Path, model: Any, extra: Sequence[str]) -> int:
        argv = [
            "run",
            "--profile",
            "tabular",
            str(csv_path),
            "--max-attempts",
            "2",
            "--out",
            str(out),
        ] + list(extra)
        return cli_module.main(argv, model=model)

    seed_out = tmp_path / "out-seed"
    assert run_tabular(seed_out, FakeModel([{"seed": 7, "n_rows": 20}]), []) == 0
    saved_plan = _run_dirs(seed_out)[0] / "plan.json"

    first_out, second_out = tmp_path / "out-1", tmp_path / "out-2"
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nReplay fixture.\n"))
    assert run_tabular(first_out, RefusingModel(), ["--plan", str(saved_plan)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nReplay fixture.\n"))
    assert run_tabular(second_out, RefusingModel(), ["--plan", str(saved_plan)]) == 0
    first = _run_dirs(first_out)[0] / "data.csv"
    second = _run_dirs(second_out)[0] / "data.csv"
    assert first.read_bytes() == second.read_bytes()
