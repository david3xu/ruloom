"""Tabular thresholds: the fidelity tolerance and its provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


@dataclass(frozen=True)
class Thresholds:
    """Fidelity tolerance for the tabular profile."""

    mean_relative_tolerance: float = 0.25

    def to_dict(self) -> dict:
        return {"mean_relative_tolerance": self.mean_relative_tolerance}

    @classmethod
    def from_dict(cls, payload: dict) -> Thresholds:
        tolerance = payload.get("mean_relative_tolerance", 0.25)
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
            raise ValueError(
                f"mean_relative_tolerance must be a number, got {tolerance!r}"
            )
        if tolerance < 0:
            raise ValueError(f"mean_relative_tolerance must be >= 0, got {tolerance!r}")
        return cls(mean_relative_tolerance=float(tolerance))


def load_thresholds(path: PathLike) -> Thresholds:
    """Load thresholds from a JSON file."""
    return Thresholds.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def write_thresholds(thresholds: Thresholds, path: PathLike) -> Path:
    """Write thresholds to a JSON file."""
    out = Path(path)
    out.write_text(
        json.dumps(thresholds.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out
