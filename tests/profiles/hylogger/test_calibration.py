"""Calibration tests: scalars, split-half variation, bands, thresholds IO."""

import json
from pathlib import Path

import pytest

from profiles.hylogger.analysis.calibration import (
    ATTRIBUTION,
    LICENCE,
    Thresholds,
    derive_thresholds,
    hole_scalars,
    load_thresholds,
    marginal_tvd,
    scalar_summary,
    write_thresholds,
)
from profiles.hylogger.intake.reader import Sample

HEADER = "StartDepth,EndDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST\n"


def _row(depth, valid, swir, tir):
    return f"{depth},{depth + 1},{1 if valid else 0},{swir or ''},{tir or ''}\n"


def _toy() -> list[Sample]:
    return [
        Sample(depth_m=10.0, valid=True, swir_group="A", tir_group="X"),
        Sample(depth_m=10.5, valid=True, swir_group="A", tir_group="Y"),
        Sample(depth_m=11.0, valid=False, swir_group="B", tir_group="X"),
        Sample(depth_m=11.0, valid=True, swir_group=None, tir_group="X"),
        Sample(depth_m=12.0, valid=True, swir_group="B", tir_group=None),
        Sample(depth_m=11.5, valid=True, swir_group="B", tir_group="X"),
    ]


def test_scalar_summary() -> None:
    s = scalar_summary(_toy())
    assert s["n"] == 6
    assert s["spacing_median_m"] == 0.5
    assert s["repeated_depth_fraction"] == round(1 / 5, 6)
    assert s["backward_steps"] == 1
    assert s["valid_fraction"] == round(5 / 6, 6)
    assert s["swir_answer_rate"] == 0.8
    assert s["tir_answer_rate"] == 0.8
    assert s["repeated_valid_fraction"] == 0.5


def test_marginal_tvd() -> None:
    assert marginal_tvd({"A": 0.5, "B": 0.5}, {"A": 0.5, "B": 0.5}) == 0.0
    assert marginal_tvd({"A": 1.0}, {"B": 1.0}) == 1.0
    assert marginal_tvd({"A": 0.75, "B": 0.25}, {"A": 0.25, "B": 0.75}) == 0.5
    assert marginal_tvd(None, {"A": 1.0}) is None
    assert marginal_tvd(None, None) is None


def test_hole_scalars_split_half() -> None:
    hole = hole_scalars(_toy(), source_name="toy")
    assert hole.source_name == "toy"
    assert hole.split_half_delta["backward_steps"] == 1
    assert hole.split_half_tvd["swir"] == pytest.approx(1.0, abs=1e-9)
    assert hole.split_half_tvd["tir"] == pytest.approx(0.5, abs=1e-9)


def _write_hole(path: Path, rows: list[str]) -> None:
    path.write_text(HEADER + "".join(rows))


def _calib_dir(tmp_path: Path) -> Path:
    hole_a = [
        _row(10.0 + i * 0.008, True, "A", "X") for i in range(6)
    ]
    hole_a[2] = _row(10.008, True, "A", "X")  # repeated depth
    hole_b = [
        _row(20.0 + i * 0.008, True, "", "Y") for i in range(6)
    ]
    hole_b[2] = _row(20.016, False, "", "Y")
    _write_hole(tmp_path / "HOLEA-scalars.csv", hole_a)
    _write_hole(tmp_path / "HOLEB-scalars.csv", hole_b)
    return tmp_path


def test_derive_bands_backward_exact_and_vacuous(tmp_path: Path) -> None:
    thresholds = derive_thresholds(_calib_dir(tmp_path))
    assert thresholds.bands["backward_steps"].lo == 0
    assert thresholds.bands["backward_steps"].hi == 0
    swir = thresholds.bands["swir_answer_rate"]
    assert swir.vacuous  # rates 1.0 vs 0.0 span the unit interval
    assert swir.lo == 0.0 and swir.hi == 1.0
    spacing = thresholds.bands["spacing_median_m"]
    assert not spacing.vacuous
    assert spacing.lo <= 0.008 <= spacing.hi
    for name in ("valid_fraction", "tir_answer_rate", "repeated_valid_fraction"):
        band = thresholds.bands[name]
        assert 0.0 <= band.lo <= band.hi <= 1.0
    assert set(thresholds.swir_vocabulary) == {"A"}
    assert set(thresholds.tir_vocabulary) == {"X", "Y"}


def test_derive_composition_evidence(tmp_path: Path) -> None:
    thresholds = derive_thresholds(_calib_dir(tmp_path))
    for channel in ("swir", "tir"):
        comp = thresholds.composition[channel]
        assert comp["n_holes"] >= 1
        assert 0.0 <= comp["median"] <= 1.0
        assert 0.0 <= comp["p95"] <= 1.0
        assert "not universally checkable" in comp["note"]


def test_derive_provenance_and_round_trip(tmp_path: Path) -> None:
    calib = _calib_dir(tmp_path)
    thresholds = derive_thresholds(calib)
    assert thresholds.provenance["licence"] == LICENCE == "CC-BY-4.0"
    assert thresholds.provenance["attribution"] == ATTRIBUTION
    assert len(thresholds.provenance["holes"]) == 2
    for hole in thresholds.provenance["holes"]:
        assert len(hole["sha256"]) == 64
    out = tmp_path / "thresholds.json"
    write_thresholds(thresholds, out)
    raw = json.loads(out.read_text())
    assert raw["provenance"]["licence"] == "CC-BY-4.0"
    loaded = load_thresholds(out)
    assert loaded.to_dict() == thresholds.to_dict()


def test_derive_no_csvs_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        derive_thresholds(tmp_path)


def test_thresholds_from_dict_defaults() -> None:
    thresholds = Thresholds.from_dict({})
    assert thresholds.bands == {}
    assert thresholds.swir_vocabulary == frozenset()
    assert thresholds.provenance == {}
