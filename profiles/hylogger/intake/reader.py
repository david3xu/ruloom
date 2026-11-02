"""HyLogger scalar CSV reader.

Reads the frozen NVCL scalar CSVs under ``data/sources`` and
``data/calibration`` and normalises each row to a :class:`Sample`.

Source column mapping (column order varies between files, names do not):

- ``StartDepth`` -> depth in metres
- ``Final_Mask == 1`` -> valid
- ``Grp1 sTSAS`` -> SWIR group label
- ``Grp1 sjCLST`` -> TIR group label

Depth convention: ``StartDepth`` metres are never negative. Measured
over all 43 real holes (5 sources plus 38 calibration) the minimum
depth is 0.0001 m; the ``depth_nonnegative`` validation check refuses
any artifact with a negative depth, and the ``depth_finite`` check
refuses non-finite depths (the parser already drops unusable source
depth cells, including NaN and infinities).

Cells that carry no answer -- empty, ``null``, ``INVALID``, ``ASPECTRAL``,
``NOTAROK`` (any letter case, surrounding whitespace ignored) -- become
``None``. Rows without a usable depth are dropped.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]

DEPTH_COLUMN = "StartDepth"
VALID_COLUMN = "Final_Mask"
SWIR_COLUMN = "Grp1_sTSAS"
TIR_COLUMN = "Grp1_sjCLST"

#: Upper-cased spellings that mean "no answer" for a group label.
NO_ANSWER = frozenset({"", "NULL", "INVALID", "ASPECTRAL", "NOTAROK"})


@dataclass(frozen=True)
class Sample:
    """One normalised HyLogger sample."""

    depth_m: float
    valid: bool
    swir_group: str | None
    tir_group: str | None


def normalize_group(value: object) -> str | None:
    """Map a raw group cell to a label or ``None`` for no-answer spellings."""
    if value is None:
        return None
    text = str(value).strip()
    if text.upper() in NO_ANSWER:
        return None
    return text


def parse_depth(value: object) -> float | None:
    """Parse a raw depth cell; return ``None`` when unusable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        depth = float(text)
    except ValueError:
        return None
    if math.isnan(depth) or math.isinf(depth):
        return None
    return depth


def parse_valid(value: object) -> bool:
    """Map a raw ``Final_Mask`` cell to a validity flag (``== 1`` is valid)."""
    if value is None:
        return False
    return str(value).strip() == "1"


def read_scalar_csv(path: PathLike) -> list[Sample]:
    """Read one NVCL scalar CSV into normalised :class:`Sample` records.

    Rows without a usable depth are dropped. All other rows are kept,
    including invalid (``Final_Mask != 1``) rows and rows whose group
    labels normalise to ``None``.
    """
    samples: list[Sample] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        missing = [
            name
            for name in (DEPTH_COLUMN, VALID_COLUMN, SWIR_COLUMN, TIR_COLUMN)
            if name not in fields
        ]
        if missing:
            raise ValueError(f"{path}: missing required columns: {', '.join(missing)}")
        for row in reader:
            depth = parse_depth(row.get(DEPTH_COLUMN))
            if depth is None:
                continue
            samples.append(
                Sample(
                    depth_m=depth,
                    valid=parse_valid(row.get(VALID_COLUMN)),
                    swir_group=normalize_group(row.get(SWIR_COLUMN)),
                    tir_group=normalize_group(row.get(TIR_COLUMN)),
                )
            )
    return samples


def read_scalar_csvs(paths: Sequence[PathLike]) -> dict[str, list[Sample]]:
    """Read several scalar CSVs, keyed by file stem."""
    return {str(Path(p).stem): read_scalar_csv(p) for p in paths}
