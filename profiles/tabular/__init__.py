"""Tabular profile: plain CSV shape statistics and synthesis."""

from .calibration import Thresholds, load_thresholds
from .checks import CheckReport, CheckResult, run_checks
from .dataset import read_csv_bytes, to_csv_bytes
from .generate import generate
from .plan import PLAN_SCHEMA, PlanError, propose, validate_plan
from .reader import read_tabular_csv
from .statistics import TabularProfile, build_stats

__all__ = [
    "Thresholds",
    "load_thresholds",
    "CheckReport",
    "CheckResult",
    "run_checks",
    "read_csv_bytes",
    "to_csv_bytes",
    "generate",
    "PLAN_SCHEMA",
    "PlanError",
    "propose",
    "validate_plan",
    "read_tabular_csv",
    "TabularProfile",
    "build_stats",
]
