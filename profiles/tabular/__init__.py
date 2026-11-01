"""Tabular profile: plain CSV shape statistics and synthesis."""

from .analysis.calibration import Thresholds, load_thresholds
from .validation.checks import CheckReport, CheckResult, run_checks
from .validation.dataset import read_csv_bytes, to_csv_bytes
from .generation.generate import generate
from .generation.plan import (
    PLAN_SCHEMA,
    PlanError,
    propose,
    provider_schema,
    validate_plan,
)
from .intake.reader import read_tabular_csv
from .analysis.statistics import TabularProfile, build_stats

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
    "provider_schema",
    "validate_plan",
    "read_tabular_csv",
    "TabularProfile",
    "build_stats",
]
