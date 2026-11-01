"""HyLogger profile intake: reader, summary statistics, synthetic dataset format."""

from .calibration import (
    Band,
    HoleScalars,
    Thresholds,
    channel_marginals,
    derive_thresholds,
    hole_scalars,
    load_thresholds,
    marginal_tvd,
    scalar_summary,
    split_halves,
    write_thresholds,
)
from .checks import CheckReport, CheckResult, check_artifact_csv, run_checks
from .dataset import read, read_csv_bytes, to_csv_bytes
from .generate import generate, generate_samples
from .reader import Sample, read_scalar_csv, read_scalar_csvs
from .statistics import Profile, build_stats

__all__ = [
    "Sample",
    "read_scalar_csv",
    "read_scalar_csvs",
    "Profile",
    "build_stats",
    "to_csv_bytes",
    "read_csv_bytes",
    "read",
    "generate",
    "generate_samples",
    "Band",
    "HoleScalars",
    "Thresholds",
    "channel_marginals",
    "derive_thresholds",
    "hole_scalars",
    "load_thresholds",
    "marginal_tvd",
    "scalar_summary",
    "split_halves",
    "write_thresholds",
    "CheckReport",
    "CheckResult",
    "check_artifact_csv",
    "run_checks",
]
