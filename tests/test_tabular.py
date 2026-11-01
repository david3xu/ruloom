"""Tabular profile tests: seeded generation plus the generic CLI path.

All model contact is faked; no test here touches the network. The small
CSV fixture exercises schema/shape checks and the single fidelity check
end to end through ``ruloom run --profile tabular``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine import cli as cli_module
from engine.manifest import load_manifest
from engine.runlog import RunLog
from models.base import Completion, Usage
from profiles.tabular import checks as checks_module
from profiles.tabular import dataset as dataset_module
from profiles.tabular import plan as plan_module
from profiles.tabular import statistics as statistics_module
from profiles.tabular.generate import generate as generate_fn
from profiles.tabular import capabilities as tab_capabilities
from profiles.tabular.calibration import Thresholds

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
    path.write_text(
        json.dumps({"mean_relative_tolerance": 0.25}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


class FakeModel:
    """A StructuredModel double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert dict(schema) == plan_module.PLAN_SCHEMA
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=5, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


def _profile() -> Any:
    return statistics_module.TabularProfile.from_rows(
        COLUMNS, ROWS, "orders-tabular", "abc123"
    )


def test_statistics_are_aggregates_only() -> None:
    profile = _profile()
    blob = profile.to_json()
    # No raw cell value may leak into the model-visible statistics.
    assert "orders-tabular" in blob
    assert profile.stats["n_rows"] == 20
    assert profile.stats["columns"] == COLUMNS
    assert profile.stats["numeric"]["value"]["mean"] == pytest.approx(12.0)
    assert set(profile.stats["text"]["label"]["values"]) == {"a", "b"}


def test_generate_is_deterministic() -> None:
    profile = _profile()
    first = generate_fn(profile, {"seed": 7, "n_rows": 20})
    second = generate_fn(profile, {"seed": 7, "n_rows": 20})
    assert first == second
    third = generate_fn(profile, {"seed": 8, "n_rows": 20})
    assert third != first


def test_checks_pass_on_matching_artifact() -> None:
    profile = _profile()
    payload = generate_fn(profile, {"seed": 7, "n_rows": 20})
    columns, rows = dataset_module.read(payload)
    report = checks_module.run_checks(
        columns,
        rows,
        Thresholds(),
        plan_n_rows=20,
        profile_columns=profile.stats["columns"],
        profile_mean=profile.stats["numeric"]["value"]["mean"],
        mean_column="value",
        artifact_sha256="abc",
    )
    assert report.verdict == "pass", [r.detail for r in report.results]


def test_checks_fail_on_wrong_shape() -> None:
    report = checks_module.run_checks(
        ["id", "value"],
        [{"id": "1", "value": "2"}],
        Thresholds(),
        plan_n_rows=20,
        profile_columns=COLUMNS,
        profile_mean=12.0,
        mean_column="value",
        artifact_sha256="abc",
    )
    assert report.verdict == "fail"
    assert "schema_columns" in report.failed
    assert "row_count" in report.failed


def test_registry_covers_manifest() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = load_manifest(root / "profiles" / "tabular" / "profile.json")
    assert set(tab_capabilities.CAPABILITIES) == set(manifest.capabilities)


def test_end_to_end_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    csv_path = _write_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out_dir = tmp_path / "out"
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nFine table.\n"))
    code = cli_module.main(
        [
            "run",
            "--profile",
            "tabular",
            str(csv_path),
            "--max-attempts",
            "2",
            "--out",
            str(out_dir),
        ],
        model=FakeModel([{"seed": 7, "n_rows": 20}]),
    )
    assert code == 0
    children = list((out_dir / "runs").iterdir())
    assert len(children) == 1
    artifact = children[0]
    for name in (
        "data.csv",
        "plan.json",
        "validation-report.json",
        "seed.txt",
        "provenance.md",
        "run.jsonl",
        "release.zip",
        "checksums.sha256",
    ):
        assert (artifact / name).is_file()
    plan = json.loads((artifact / "plan.json").read_text(encoding="utf-8"))
    assert plan == {"seed": 7, "n_rows": 20}
    report = json.loads(
        (artifact / "validation-report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "pass"
    # Advisory step ran and was logged, but never decided the verdict.
    log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert log.verify()
    stages = [event["stage"] for event in log.events]
    assert "describe:advisory" in stages
    advisories = [
        event for event in stages if event == "describe:advisory"
    ]
    assert advisories
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        assert sorted(archive.namelist()) == sorted(
            [
                "data.csv",
                "plan.json",
                "validation-report.json",
                "seed.txt",
                "provenance.md",
                "run.jsonl",
                "checksums.sha256",
            ]
        )
    printed = capsys.readouterr().out
    assert "[describe] advisory summary recorded" in printed
    assert f"sha256={artifact.name}" not in printed  # artifact dir, not zip


def test_end_to_end_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_path = _write_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out_dir = tmp_path / "out"
    monkeypatch.setattr("sys.stdin", io.StringIO("reject\nNot this one.\n"))
    code = cli_module.main(
        [
            "run",
            "--profile",
            "tabular",
            str(csv_path),
            "--max-attempts",
            "1",
            "--out",
            str(out_dir),
        ],
        model=FakeModel([{"seed": 7, "n_rows": 20}]),
    )
    assert code != 0
    (artifact,) = list((out_dir / "runs").iterdir())
    assert not (artifact / "release.zip").exists()
    log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert log.verify()
