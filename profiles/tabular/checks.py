"""Deterministic checks of tabular artifacts against calibrated thresholds.

Every check returns a :class:`CheckResult` with status ``pass``, ``fail``
or ``skip``. A skip is recorded as a skip, never as a pass. The report
verdict is ``pass`` only when no check fails.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .calibration import Thresholds
from .dataset import read_csv_bytes, to_csv_bytes

PASS, FAIL, SKIP = "pass", "fail", "skip"


@dataclass(frozen=True)
class CheckResult:
    """One named check outcome; ``status`` is pass, fail or skip."""

    name: str
    status: str
    detail: str


@dataclass
class CheckReport:
    """All check outcomes for one artifact, bound to its sha256."""

    artifact_name: str
    artifact_sha256: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """``pass`` only when no check fails (skips permitted, shown)."""
        return FAIL if any(r.status == FAIL for r in self.results) else PASS

    @property
    def failed(self) -> list[str]:
        return [r.name for r in self.results if r.status == FAIL]

    @property
    def skipped(self) -> list[str]:
        return [r.name for r in self.results if r.status == SKIP]

    def to_dict(self) -> dict:
        return {
            "artifact_name": self.artifact_name,
            "artifact_sha256": self.artifact_sha256,
            "verdict": self.verdict,
            "results": [
                {"name": r.name, "status": r.status, "detail": r.detail}
                for r in self.results
            ],
        }


def _mean(values: Sequence[str]) -> float | None:
    numbers: list[float] = []
    for value in values:
        if value == "":
            continue
        try:
            numbers.append(float(value))
        except ValueError:
            return None
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def run_checks(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    thresholds: Thresholds,
    *,
    plan_n_rows: int,
    profile_columns: Sequence[str] | None = None,
    profile_mean: float | None = None,
    mean_column: str | None = None,
    artifact_name: str = "",
    artifact_sha256: str = "",
) -> CheckReport:
    """Run schema, shape, and one fidelity check on a tabular artifact."""
    rows = list(rows)
    columns = list(columns)
    payload = to_csv_bytes(rows, columns)
    sha = artifact_sha256 or hashlib.sha256(payload).hexdigest()
    report = CheckReport(artifact_name=artifact_name, artifact_sha256=sha)
    add = report.results.append

    if not rows:
        add(CheckResult("has_rows", FAIL, "empty artifact"))
        return report
    add(CheckResult("has_rows", PASS, f"{len(rows)} rows"))

    expected = list(profile_columns) if profile_columns is not None else columns
    if columns == expected:
        add(CheckResult("schema_columns", PASS, f"columns {columns}"))
    else:
        add(
            CheckResult(
                "schema_columns",
                FAIL,
                f"columns {columns} != profile {expected}",
            )
        )

    if len(rows) == plan_n_rows:
        add(CheckResult("row_count", PASS, f"{len(rows)} rows as planned"))
    else:
        add(
            CheckResult(
                "row_count",
                FAIL,
                f"{len(rows)} rows != planned {plan_n_rows}",
            )
        )

    target = mean_column or (columns[0] if columns else "")
    observed = _mean([row.get(target, "") for row in rows])
    if observed is None or profile_mean is None:
        add(CheckResult("mean_value", SKIP, "no numeric column to compare"))
    else:
        tolerance = thresholds.mean_relative_tolerance
        reference = abs(profile_mean) or 1.0
        distance = abs(observed - profile_mean) / reference
        if distance <= tolerance:
            add(
                CheckResult(
                    "mean_value",
                    PASS,
                    f"relative drift {distance:.6f} <= {tolerance}",
                )
            )
        else:
            add(
                CheckResult(
                    "mean_value",
                    FAIL,
                    f"relative drift {distance:.6f} > {tolerance}",
                )
            )
    return report


def check_artifact_csv(
    payload: bytes | str,
    thresholds: Thresholds,
    *,
    plan_n_rows: int,
    profile_columns: Sequence[str] | None = None,
    profile_mean: float | None = None,
    mean_column: str | None = None,
    artifact_name: str = "",
) -> CheckReport:
    """Parse an artifact CSV (schema check) then run all checks."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    sha = hashlib.sha256(raw).hexdigest()
    try:
        columns, rows = read_csv_bytes(raw)
    except ValueError as exc:
        return CheckReport(
            artifact_name=artifact_name,
            artifact_sha256=sha,
            results=[CheckResult("csv_schema", FAIL, str(exc))],
        )
    report = run_checks(
        columns,
        rows,
        thresholds,
        plan_n_rows=plan_n_rows,
        profile_columns=profile_columns,
        profile_mean=profile_mean,
        mean_column=mean_column,
        artifact_name=artifact_name,
        artifact_sha256=sha,
    )
    report.results.insert(
        0, CheckResult("csv_schema", PASS, f"{len(rows)} rows, header ok")
    )
    return report
