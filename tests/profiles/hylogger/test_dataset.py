"""Dataset tests: synthetic CSV round-trip, null labels, error paths."""

import pytest

from profiles.hylogger.intake.reader import Sample
from profiles.hylogger.validation.dataset import (
    HEADER,
    read,
    read_csv_bytes,
    to_csv_bytes,
)


def _samples() -> list[Sample]:
    return [
        Sample(depth_m=10.5, valid=True, swir_group="kaolinite", tir_group="quartz"),
        Sample(depth_m=11.0, valid=False, swir_group=None, tir_group=None),
        Sample(depth_m=12.25, valid=True, swir_group=None, tir_group="feldspar"),
    ]


def test_header_names() -> None:
    assert HEADER == ("depth_m", "valid", "swir_group", "tir_group")


def test_round_trip() -> None:
    original = _samples()
    parsed = read_csv_bytes(to_csv_bytes(original))
    assert [(s.depth_m, s.valid, s.swir_group, s.tir_group) for s in parsed] == [
        (s.depth_m, s.valid, s.swir_group, s.tir_group) for s in original
    ]


def test_valid_written_as_true_false() -> None:
    text = to_csv_bytes(_samples()).decode("utf-8")
    rows = text.splitlines()[1:]
    assert rows[0].split(",")[1] == "true"
    assert rows[1].split(",")[1] == "false"


def test_null_labels_are_empty_cells() -> None:
    text = to_csv_bytes(_samples()).decode("utf-8")
    rows = text.splitlines()[1:]
    assert rows[1].endswith(",,")
    parsed = read_csv_bytes(to_csv_bytes(_samples()))
    assert parsed[1].swir_group is None
    assert parsed[1].tir_group is None


def test_read_alias_matches() -> None:
    assert read is read_csv_bytes
    assert read(to_csv_bytes(_samples()))[0].swir_group == "kaolinite"


def test_accepts_text_input() -> None:
    assert read_csv_bytes(to_csv_bytes(_samples()).decode("utf-8"))[0].depth_m == 10.5


def test_bad_header_raises() -> None:
    with pytest.raises(ValueError, match="unexpected header"):
        read_csv_bytes(b"depth_m,valid\n10.5,true\n")


def test_bad_valid_cell_raises() -> None:
    with pytest.raises(ValueError, match="bad valid cell"):
        read_csv_bytes(b"depth_m,valid,swir_group,tir_group\n10.5,maybe,,\n")


def test_bad_depth_cell_raises() -> None:
    with pytest.raises(ValueError, match="bad depth cell"):
        read_csv_bytes(b"depth_m,valid,swir_group,tir_group\nabc,true,,\n")
