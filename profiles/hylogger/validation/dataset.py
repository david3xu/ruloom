"""Synthetic dataset CSV format.

Columns: ``depth_m,valid,swir_group,tir_group``. ``valid`` is written as
``true``/``false`` and null labels as empty cells. This is the format
later loops generate and consume; it never carries source rows.
"""

from __future__ import annotations

import csv
import io
from typing import Sequence, Union

from ..intake.reader import Sample

HEADER = ("depth_m", "valid", "swir_group", "tir_group")


def to_csv_bytes(samples: Sequence[Sample]) -> bytes:
    """Serialise normalised samples to the synthetic CSV format."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for sample in samples:
        writer.writerow(
            [
                repr(float(sample.depth_m)),
                "true" if sample.valid else "false",
                sample.swir_group if sample.swir_group is not None else "",
                sample.tir_group if sample.tir_group is not None else "",
            ]
        )
    return buffer.getvalue().encode("utf-8")


def read_csv_bytes(payload: Union[bytes, str]) -> list[Sample]:
    """Parse synthetic CSV bytes (or text) back into :class:`Sample` records."""
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != list(HEADER):
        raise ValueError(f"unexpected header: {reader.fieldnames}")
    samples: list[Sample] = []
    for row in reader:
        valid_cell = (row["valid"] or "").strip().lower()
        if valid_cell not in ("true", "false"):
            raise ValueError(f"bad valid cell: {row['valid']!r}")
        try:
            depth = float((row["depth_m"] or "").strip())
        except ValueError as exc:
            raise ValueError(f"bad depth cell: {row['depth_m']!r}") from exc
        samples.append(
            Sample(
                depth_m=depth,
                valid=valid_cell == "true",
                swir_group=row["swir_group"] or None,
                tir_group=row["tir_group"] or None,
            )
        )
    return samples


#: Alias: ``read`` is the counterpart of :func:`to_csv_bytes`.
read = read_csv_bytes
