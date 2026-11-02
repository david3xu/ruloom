"""Tabular wiring tests: registry coverage plus the generic CLI path.

All model contact is faked; no test here touches the network. The small
CSV fixture exercises the end-to-end run through
``ruloom run --profile tabular``.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from engine import cli as cli_module
from engine.audit.runlog import RunLog
from engine.core.manifest import load_manifest
from models.base import Completion, Usage
from profiles.tabular import capabilities as tab_capabilities
from profiles.tabular.generation import plan as plan_module

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
        assert dict(schema) == plan_module.provider_schema(
            ["fitted_gaussian_disclosed"]
        )
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=5, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


def test_registry_covers_manifest() -> None:
    root = Path(__file__).resolve().parents[3]
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
    run_dirs = [c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()]
    assert len(run_dirs) == 1
    artifact = run_dirs[0]
    for name in (
        "data.csv",
        "plan.json",
        "validation-report.json",
        "seed.txt",
        "provenance.md",
        "run.jsonl",
        "evidence-report.md",
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
    advisories = [event for event in stages if event == "describe:advisory"]
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
                "evidence-report.md",
                "checksums.sha256",
            ]
        )
    printed = capsys.readouterr().out
    assert "[describe] advisory summary recorded" in printed
    assert f"sha256={artifact.name}" not in printed  # artifact dir, not zip


def test_end_to_end_reject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    run_dirs = [c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()]
    assert len(run_dirs) == 1
    (artifact,) = run_dirs
    assert not (artifact / "release.zip").exists()
    log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert log.verify()
