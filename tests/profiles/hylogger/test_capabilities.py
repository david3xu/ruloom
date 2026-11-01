"""Tests for the HyLogger capability implementations.

The registry must cover every manifest capability id, and the generic
engine path (``--profile hylogger``) must reproduce the compatibility
path byte for byte: same artifact hash, same release members.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine import cli as cli_module
from engine.core.manifest import load_manifest
from engine.audit.runlog import RunLog
from models.base import Completion, Usage
from profiles.hylogger import capabilities as hy_capabilities
from profiles.hylogger.generation.plan import provider_schema

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


def _write_source(tmp_path: Path) -> Path:
    csv_path = tmp_path / "TOY-cap-scalars.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i, (mask, swir, tir) in enumerate(COVERAGE):
        lines.append(f"{BASE_DEPTH + i * STEP},{mask},{swir},{tir}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "TOY-cap-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "TOY-cap",
                "licence": "CC-BY-4.0",
                "attribution": "Toy attribution for tests.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _write_thresholds(tmp_path: Path) -> Path:
    from profiles.hylogger.analysis.calibration import Band, Thresholds

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
    """A StructuredModel double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        assert dict(schema) == provider_schema(["fitted_markov_joint"])
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=10, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


def _manifest() -> Any:
    root = Path(__file__).resolve().parents[3]
    return load_manifest(root / "profiles" / "hylogger" / "profile.json")


def test_registry_covers_manifest() -> None:
    manifest = _manifest()
    assert set(hy_capabilities.CAPABILITIES) == set(manifest.capabilities)


def test_registry_permissions_match_manifest() -> None:
    manifest = _manifest()
    assert manifest.capabilities["plan"]["calls_model"] == "planner"
    assert manifest.capabilities["validate"]["writes_verdict"] is True
    assert manifest.capabilities["intake"]["reads_raw_source"] is True
    assert "calls_model" not in manifest.capabilities["generate"]


def test_generic_path_matches_compat_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--profile hylogger`` must reproduce the compat artifact byte for byte."""
    csv_path = _write_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))

    def run(extra: list[str]) -> Path:
        out_dir = tmp_path / f"out-{len(extra)}"
        argv = ["run", str(csv_path), "--max-attempts", "2",
                "--out", str(out_dir)] + extra
        monkeypatch.setattr("sys.stdin", io.StringIO("accept\nok\n"))
        code = cli_module.main(argv, model=FakeModel([{"seed": 8, "n_rows": 8}]))
        assert code == 0
        run_dirs = [
            c for c in (out_dir / "runs").iterdir()
            if (c / "run.json").is_file()
        ]
        assert len(run_dirs) == 1
        return run_dirs[0]

    compat = run([])
    generic = run(["--profile", "hylogger"])
    # Unique persistent runs: identical payload bytes, distinct run ids.
    assert compat.name != generic.name
    for name in ("data.csv", "plan.json", "validation-report.json",
                 "seed.txt"):
        assert (compat / name).read_bytes() == (generic / name).read_bytes()
    for name in ("data.csv", "plan.json", "validation-report.json",
                 "seed.txt", "evidence-report.md"):
        assert (compat / name).is_file()
        assert (generic / name).is_file()
    # checksums.sha256 also attests run.jsonl, which carries the per-run
    # id, so only the lines for the identical members match across runs.
    def attested(artifact: Path) -> dict[str, str]:
        # checksums.sha256 lines are "<sha>  <name>"; index by member name.
        return dict(
            (parts[1], parts[0])
            for parts in (
                line.split("  ")
                for line in (artifact / "checksums.sha256").read_text(
                    encoding="utf-8"
                ).splitlines()
            )
        )

    compat_sums, generic_sums = attested(compat), attested(generic)
    for name in ("data.csv", "plan.json", "validation-report.json",
                 "seed.txt"):
        assert compat_sums[name] == generic_sums[name]
    # Provenance carries the per-run id, so only its content shape matches.
    for artifact in (compat, generic):
        record = json.loads((artifact / "run.json").read_text(encoding="utf-8"))
        lines = (artifact / "provenance.md").read_text(
            encoding="utf-8"
        ).splitlines()
        assert lines[0] == "# Provenance"
        assert any(line.startswith("Run: ") for line in lines)
        assert any(
            line.startswith("Artifact sha256: " + record["artifact_sha256"])
            for line in lines
        )
    for artifact in (compat, generic):
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
        assert RunLog.read_jsonl(artifact / "run.jsonl").verify()
