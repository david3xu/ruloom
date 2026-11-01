"""Tabular validation: deterministic checks over the artifact dataset."""

from .checks import CheckReport, CheckResult, run_checks
from .dataset import read_csv_bytes, to_csv_bytes

__all__ = [
    "CheckReport",
    "CheckResult",
    "run_checks",
    "read_csv_bytes",
    "to_csv_bytes",
]
