"""Tabular analysis tests: loading the fidelity tolerance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from profiles.tabular.analysis.calibration import Thresholds, load_thresholds


def test_load_thresholds_reads_tolerance(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.json"
    path.write_text(
        json.dumps({"mean_relative_tolerance": 0.25}, indent=2) + "\n",
        encoding="utf-8",
    )
    assert load_thresholds(path).mean_relative_tolerance == pytest.approx(0.25)
    assert Thresholds().mean_relative_tolerance == pytest.approx(0.25)
