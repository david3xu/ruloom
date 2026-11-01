"""Synthetic tabular dataset CSV format.

The synthetic artifact reuses the source header exactly; cells are written
as plain strings. This is the format later loops generate and consume; it
never carries source rows.
"""

from __future__ import annotations

import csv
import io
from typing import Mapping, Sequence, Union


def to_csv_bytes(
    rows: Sequence[Mapping[str, str]], columns: Sequence[str]
) -> bytes:
    """Serialise row mappings to CSV bytes under ``columns``."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow([row.get(col, "") for col in columns])
    return buffer.getvalue().encode("utf-8")


def read_csv_bytes(
    payload: Union[bytes, str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Parse artifact CSV bytes back into ``(columns, rows)``."""
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError("artifact has no header row")
    columns = list(reader.fieldnames)
    rows = [{col: (row.get(col) or "") for col in columns} for row in reader]
    return columns, rows


#: Alias: ``read`` is the counterpart of :func:`to_csv_bytes`.
read = read_csv_bytes
