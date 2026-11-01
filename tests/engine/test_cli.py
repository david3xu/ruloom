"""CLI lifecycle tests: accept, reject, modify, and failing-verdict paths.

All model contact is faked with a queue-based :class:`StructuredModel`
double. No test here touches the network. Source fixtures are small
synthetic scalar CSVs in the real ``StartDepth`` format; thresholds are
permissive test-only bands injected via ``RULOOM_THRESHOLDS``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine import cli as cli_module
from engine.audit.runlog import RunLog
from models.base import Completion, Usage
from profiles.hylogger.analysis.calibration import Band, Thresholds
from profiles.hylogger.generation.plan import PLAN_SCHEMA

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


def _write_source(tmp_path: Path, stem: str = "TOY-hole-scalars") -> Path:
    csv_path = tmp_path / f"{stem}.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i, (mask, swir, tir) in enumerate(COVERAGE):
        lines.append(f"{BASE_DEPTH + i * STEP},{mask},{swir},{tir}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = tmp_path / f"{stem[:-len('-scalars')]}-source.json"
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


def _write_thresholds(tmp_path: Path) -> Path:
    thresholds = Thresholds(
        bands={
            "spacing_median_m": Band("spacing_median_m", 0.005, 0.01),
            "repeated_depth_fraction": Band("repeated_depth_fraction", 0.0, 0.2),
            "valid_fraction": Band("valid_fraction", 0.5, 1.0),
            "swir_answer_rate": Band(
                "swir_answer_rate", 0.0, 1.0, vacuous=True
            ),
            "tir_answer_rate": Band("tir_answer_rate", 0.9, 1.0),
            "repeated_valid_fraction": Band(
                "repeated_valid_fraction", 0.0, 0.05
            ),
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


class FakeModel:
    """A :class:`StructuredModel` double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert dict(schema) == PLAN_SCHEMA
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=10, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plans: Sequence[Mapping[str, Any]],
    stdin_text: str,
    extra_argv: Sequence[str] | None = None,
) -> tuple[int, str, Path]:
    csv_path = _write_source(tmp_path)
    thresholds_path = _write_thresholds(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(thresholds_path))
    out_dir = tmp_path / "out"
    argv = ["run", str(csv_path), "--max-attempts", "2", "--out", str(out_dir)]
    if extra_argv:
        argv.extend(extra_argv)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = cli_module.main(argv, model=FakeModel(plans))
    return code, str(out_dir), csv_path


def _artifact_dir(out_dir: str) -> Path:
    """The run's own directory: every execution owns its history."""
    runs = Path(out_dir) / "runs"
    run_dirs = [c for c in runs.iterdir() if (c / "run.json").is_file()]
    assert len(run_dirs) == 1
    return run_dirs[0]


def test_accept_writes_release_and_verifies_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    code, out_dir, _ = _run(
        monkeypatch, tmp_path, [{"seed": 8, "n_rows": 8}], "accept\nLooks good — ship it.\n"
    )
    assert code == 0
    artifact = _artifact_dir(out_dir)
    for name in (
        "data.csv",
        "plan.json",
        "validation-report.json",
        "seed.txt",
        "provenance.md",
        "run.jsonl",
    ):
        assert (artifact / name).is_file()
    assert (artifact / "seed.txt").read_text(encoding="utf-8") == "8\n"

    log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert log.verify()
    stages = [event["stage"] for event in log.events]
    for expected in (
        "source_intake",
        "profile",
        "plan",
        "generate_validate_repair",
        "artifact",
        "human_review",
        "release",
    ):
        assert expected in stages
    review = next(
        event for event in log.events if event["stage"] == "human_review"
    )
    assert review["status"] == "accept"
    assert review["details"]["note"] == "Looks good — ship it."

    zip_path = artifact / "release.zip"
    assert zip_path.is_file()
    import hashlib

    zip_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(zip_path) as archive:
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
        checksums = archive.read("checksums.sha256").decode("utf-8")
        # checksums.sha256 attests the zipped members: the zipped run.jsonl
        # is the pre-release snapshot (the on-disk log gains the release
        # event afterwards), so verify against the archived bytes.
        for line in checksums.splitlines():
            digest, name = line.split("  ")
            assert (
                hashlib.sha256(archive.read(name)).hexdigest() == digest
            )
        zipped_log = RunLog.from_jsonl(
            archive.read("run.jsonl").decode("utf-8")
        )
    assert zipped_log.verify()
    assert [e["stage"] for e in zipped_log.events][-1] == "human_review"
    release = next(event for event in log.events if event["stage"] == "release")
    assert release["details"]["zip_sha256"] == zip_sha
    printed = capsys.readouterr().out
    assert str(zip_path) in printed
    assert f"sha256={zip_sha}" in printed


def test_reject_writes_no_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out_dir, _ = _run(
        monkeypatch, tmp_path, [{"seed": 8, "n_rows": 8}], "reject\nNot yet.\n"
    )
    assert code != 0
    artifact = _artifact_dir(out_dir)
    assert not (artifact / "release.zip").exists()
    assert not (artifact / "checksums.sha256").exists()
    log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert log.verify()
    review = next(
        event for event in log.events if event["stage"] == "human_review"
    )
    assert review["status"] == "reject"
    assert review["details"]["note"] == "Not yet."


def test_modify_writes_no_release_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out_dir, _ = _run(
        monkeypatch,
        tmp_path,
        [{"seed": 8, "n_rows": 8}],
        "modify\n  keep leading spaces\n",
    )
    assert code != 0
    artifact = _artifact_dir(out_dir)
    assert not (artifact / "release.zip").exists()
    log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert log.verify()
    review = next(
        event for event in log.events if event["stage"] == "human_review"
    )
    assert review["status"] == "modify"
    assert review["details"]["note"] == "  keep leading spaces"


def test_failing_plan_writes_no_release_and_prints_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    csv_path = _write_source(tmp_path)
    thresholds_path = _write_thresholds(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(thresholds_path))
    out_dir = tmp_path / "out"
    # No stdin: a failing verdict must never prompt for human review.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code = cli_module.main(
        ["run", str(csv_path), "--max-attempts", "1", "--out", str(out_dir)],
        model=FakeModel([{"seed": 0, "n_rows": 8}]),
    )
    assert code != 0
    artifact = _artifact_dir(str(out_dir))
    assert not (artifact / "release.zip").exists()
    printed = capsys.readouterr().out
    assert "[fail]" in printed
    log = RunLog.read_jsonl(artifact / "run.jsonl")
    assert log.verify()


def test_max_attempts_flag_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_path = _write_source(tmp_path)
    monkeypatch.setenv(
        "RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path))
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        cli_module.main(["run", str(csv_path)], model=FakeModel([]))
