"""CLI GenerationSpec tests: --spec files, refusals, and compat.

An explicit ``--spec PATH`` overlays a spec file on the flag-implied
defaults: a matching file runs end-to-end with byte-identical payload
bytes, while a contradicting file (wrong attempt bound, unknown
strategy, unreadable JSON) is refused before any model call with a
durable terminal record. An unknown profile is refused the same way.
The default invocation (no ``--spec``) behaves exactly as before.

All model contact is faked; no test here touches the network.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine import cli as cli_module
from engine.audit.runlog import RunLog

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


def _write_source(tmp_path: Path) -> Path:
    csv_path = tmp_path / "orders-tabular.csv"
    lines = [",".join(COLUMNS)]
    lines += [",".join(row[col] for col in COLUMNS) for row in ROWS]
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
    return csv_path


def _write_thresholds(tmp_path: Path) -> Path:
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"mean_relative_tolerance": 0.25}) + "\n")
    return path


class FakeModel:
    """A StructuredModel double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Any:
        from models.base import Completion, Usage

        self.calls += 1
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=5, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


def _run(
    monkeypatch: Any,
    tmp_path: Path,
    argv: Sequence[str],
    model: FakeModel,
    stdin_text: str = "accept\nFine.\n",
) -> int:
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    return cli_module.main(list(argv), model=model)


def _run_dirs(out: Path) -> list[Path]:
    return [
        child
        for child in (out / "runs").iterdir()
        if (child / "run.json").is_file()
    ]


def _matching_spec() -> dict[str, Any]:
    return {
        "spec_version": 1,
        "profile": "tabular",
        "outputs": ["scalar"],
        "strategy": "fitted_gaussian_disclosed",
        "limits": {"max_rows": 200000, "max_attempts": 2},
        "rights": {"output_terms_intent": None},
    }


def test_spec_file_matching_defaults_runs_identically(
    tmp_path: Path, monkeypatch: Any
) -> None:
    csv_path = _write_source(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_matching_spec()), encoding="utf-8")

    def run(extra: Sequence[str], out_name: str) -> Path:
        out = tmp_path / out_name
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
        code = _run(
            monkeypatch, tmp_path, argv, FakeModel([{"seed": 7, "n_rows": 20}])
        )
        assert code == 0
        run_dirs = _run_dirs(out)
        assert len(run_dirs) == 1
        return run_dirs[0]

    default_dir = run([], "out-default")
    spec_dir = run(["--spec", str(spec_path)], "out-spec")
    assert (default_dir / "data.csv").read_bytes() == (
        spec_dir / "data.csv"
    ).read_bytes()
    default_record = json.loads(
        (default_dir / "run.json").read_text(encoding="utf-8")
    )
    spec_record = json.loads((spec_dir / "run.json").read_text(encoding="utf-8"))
    assert (
        default_record["run"]["request"]["spec_digest"]
        == spec_record["run"]["request"]["spec_digest"]
    )
    for run_dir in (default_dir, spec_dir):
        with zipfile.ZipFile(run_dir / "release.zip") as archive:
            assert "evidence-report.md" in archive.namelist()
        assert RunLog.read_jsonl(run_dir / "run.jsonl").verify()


def test_spec_attempt_mismatch_refused_without_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    csv_path = _write_source(tmp_path)
    spec_path = tmp_path / "spec.json"
    overlay = _matching_spec()
    overlay["limits"] = {"max_attempts": 5}
    spec_path.write_text(json.dumps(overlay), encoding="utf-8")
    out = tmp_path / "out"
    model = FakeModel([{"seed": 7, "n_rows": 20}])
    code = _run(
        monkeypatch,
        tmp_path,
        [
            "run",
            "--profile",
            "tabular",
            str(csv_path),
            "--max-attempts",
            "2",
            "--out",
            str(out),
            "--spec",
            str(spec_path),
        ],
        model,
    )
    assert code != 0
    assert model.calls == 0
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert "contradicts the caller bound" in (record["error"] or "")


def test_spec_unknown_strategy_refused_without_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    csv_path = _write_source(tmp_path)
    spec_path = tmp_path / "spec.json"
    overlay = _matching_spec()
    overlay["strategy"] = "no-such-strategy"
    spec_path.write_text(json.dumps(overlay), encoding="utf-8")
    out = tmp_path / "out"
    model = FakeModel([{"seed": 7, "n_rows": 20}])
    code = _run(
        monkeypatch,
        tmp_path,
        [
            "run",
            "--profile",
            "tabular",
            str(csv_path),
            "--max-attempts",
            "2",
            "--out",
            str(out),
            "--spec",
            str(spec_path),
        ],
        model,
    )
    assert code != 0
    assert model.calls == 0
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert "unknown strategy" in (record["error"] or "")


def test_unreadable_spec_file_refused_without_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    csv_path = _write_source(tmp_path)
    out = tmp_path / "out"
    model = FakeModel([{"seed": 7, "n_rows": 20}])
    code = _run(
        monkeypatch,
        tmp_path,
        [
            "run",
            "--profile",
            "tabular",
            str(csv_path),
            "--max-attempts",
            "2",
            "--out",
            str(out),
            "--spec",
            str(tmp_path / "absent.json"),
        ],
        model,
    )
    assert code != 0
    assert model.calls == 0
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"


def test_unknown_profile_refused_without_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    csv_path = _write_source(tmp_path)
    out = tmp_path / "out"
    model = FakeModel([{"seed": 7, "n_rows": 20}])
    code = _run(
        monkeypatch,
        tmp_path,
        [
            "run",
            "--profile",
            "nosuchprofile",
            str(csv_path),
            "--max-attempts",
            "2",
            "--out",
            str(out),
        ],
        model,
    )
    assert code != 0
    assert model.calls == 0
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert "unknown profile" in (record["error"] or "")


def test_imaging_on_scalar_only_profile_refused_without_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    csv_path = _write_source(tmp_path)
    images = tmp_path / "images"
    images.mkdir()
    out = tmp_path / "out"
    model = FakeModel([{"seed": 7, "n_rows": 20}])
    code = _run(
        monkeypatch,
        tmp_path,
        [
            "run",
            "--profile",
            "tabular",
            str(csv_path),
            "--max-attempts",
            "2",
            "--out",
            str(out),
            "--images",
            str(images),
        ],
        model,
    )
    assert code != 0
    assert model.calls == 0
    run_dirs = _run_dirs(out)
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert "imaging" in (record["error"] or "")
