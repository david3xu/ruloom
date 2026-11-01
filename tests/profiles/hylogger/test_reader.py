"""Reader tests: field mapping, no-answer normalization, dropped bad depths."""

import csv
from pathlib import Path

import pytest

from profiles.hylogger.intake.reader import (
    Sample,
    normalize_group,
    parse_depth,
    parse_valid,
    read_scalar_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PDD446 = REPO_ROOT / "data" / "sources" / "PDD446-scalars.csv"

requires_source_data = pytest.mark.skipif(
    not PDD446.is_file(),
    reason="requires git-ignored NVCL source data under data/ (absent in clean checkout)",
)


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def test_field_mapping(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "hole.csv",
        ["StartDepth", "EndDepth", "Final_Mask", "Grp1_sjCLST", "Grp1_sTSAS"],
        [["100.5", "100.6", "1", "SILICA", "CHLORITE"]],
    )
    samples = read_scalar_csv(path)
    assert samples == [
        Sample(depth_m=100.5, valid=True, swir_group="CHLORITE", tir_group="SILICA")
    ]


def test_field_mapping_column_order_independent(tmp_path: Path) -> None:
    # Real files carry the columns in different orders; names rule, not positions.
    path = _write_csv(
        tmp_path / "hole.csv",
        ["StartDepth", "EndDepth", "Grp1_sjCLST", "Final_Mask", "Grp1_sTSAS"],
        [["48.80334", "48.80334", "null", "0", "null"]],
    )
    samples = read_scalar_csv(path)
    assert samples == [
        Sample(depth_m=48.80334, valid=False, swir_group=None, tir_group=None)
    ]


@pytest.mark.parametrize(
    "cell",
    ["", "null", "NULL", "Null", "  null  ", "INVALID", "invalid", "Aspectral",
     "ASPECTRAL", "NOTAROK", "notarok", " NOTAROK "],
)
def test_no_answer_normalization(cell: str) -> None:
    assert normalize_group(cell) is None


def test_no_answer_none_passthrough() -> None:
    assert normalize_group(None) is None


def test_real_labels_survive() -> None:
    assert normalize_group("CHLORITE") == "CHLORITE"
    assert normalize_group("  WHITE-MICA ") == "WHITE-MICA"


@pytest.mark.parametrize("cell", ["", "  ", "abc", "NaN", "nan", "inf", "-", None])
def test_bad_depths_dropped(cell: object) -> None:
    assert parse_depth(cell) is None


def test_rows_without_usable_depth_dropped(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "hole.csv",
        ["StartDepth", "EndDepth", "Final_Mask", "Grp1_sjCLST", "Grp1_sTSAS"],
        [
            ["10.0", "10.1", "1", "SILICA", "CHLORITE"],
            ["", "10.2", "1", "SILICA", "CHLORITE"],
            ["not-a-depth", "10.3", "1", "SILICA", "CHLORITE"],
            ["NaN", "10.4", "1", "SILICA", "CHLORITE"],
            ["12.5", "12.6", "0", "null", "INVALID"],
        ],
    )
    samples = read_scalar_csv(path)
    assert [s.depth_m for s in samples] == [10.0, 12.5]
    assert samples[1] == Sample(
        depth_m=12.5, valid=False, swir_group=None, tir_group=None
    )


@pytest.mark.parametrize(
    ("cell", "expected"), [("1", True), ("0", False), (" 1 ", True), ("2", False), ("", False), (None, False)]
)
def test_valid_mapping(cell: object, expected: bool) -> None:
    assert parse_valid(cell) is expected


def test_missing_column_raises(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "hole.csv", ["StartDepth", "Final_Mask"], [["1.0", "1"]]
    )
    with pytest.raises(ValueError, match="missing required columns"):
        read_scalar_csv(path)


@requires_source_data
def test_pdd446_reads_and_normalizes() -> None:
    samples = read_scalar_csv(PDD446)
    assert len(samples) == 39300
    assert all(isinstance(s.depth_m, float) for s in samples)
    answered_swir = {s.swir_group for s in samples if s.swir_group is not None}
    assert "null" not in answered_swir
    assert "INVALID" not in answered_swir
    assert "NOTAROK" not in answered_swir
    assert "CHLORITE" in answered_swir


def test_synthetic_csv_round_trip(tmp_path: Path) -> None:
    from profiles.hylogger.validation.dataset import read_csv_bytes, to_csv_bytes

    samples = [
        Sample(depth_m=10.5, valid=True, swir_group="CHLORITE", tir_group=None),
        Sample(depth_m=11.0, valid=False, swir_group=None, tir_group=None),
    ]
    payload = to_csv_bytes(samples)
    assert payload.startswith(b"depth_m,valid,swir_group,tir_group\n")
    assert b"true" in payload and b"false" in payload
    assert read_csv_bytes(payload) == samples
