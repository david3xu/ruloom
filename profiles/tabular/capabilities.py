"""Tabular capability implementations for the generic engine.

Each function receives a scoped run context from the engine and performs
one manifest step. The model only ever sees profile statistics, never raw
source rows; the engine enforces that boundary from the manifest
permissions, and these functions never reach past their scoped context.

The context is duck-typed on purpose: this module imports no engine and
no model code, so the layer boundary stays intact.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .analysis.calibration import load_thresholds
from .validation.checks import run_checks
from .validation.dataset import read as read_payload
from .generation.generate import generate as generate_artifact
from .generation.plan import (
    PLAN_SCHEMA,
    build_repair_prompt,
    propose,
    validate_plan,
)
from .intake.reader import read_tabular_csv
from .analysis.statistics import TabularProfile


def _thresholds_path() -> Path:
    """Committed thresholds, overridable for tests via RULOOM_THRESHOLDS."""
    override = os.environ.get("RULOOM_THRESHOLDS")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "thresholds.json"


def _sibling_manifest(source_path: Path, config: Any) -> Path | None:
    """Sibling ``*-source.json`` manifest for a source CSV."""
    data_suffix = (
        config.get("data_suffix", "-tabular.csv")
        if isinstance(config, dict)
        else "-tabular.csv"
    )
    manifest_suffix = (
        config.get("manifest_suffix", "-source.json")
        if isinstance(config, dict)
        else "-source.json"
    )
    name = source_path.name
    if name.endswith(data_suffix):
        candidate = source_path.with_name(
            name[: -len(data_suffix)] + manifest_suffix
        )
    else:
        candidate = source_path.with_name(source_path.stem + manifest_suffix)
    return candidate if candidate.is_file() else None


def _catalog(ctx: Any) -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in ctx.manifest.checks}


def intake(ctx: Any) -> None:
    """Hash the source, record licence/attribution, parse rows."""
    payload = ctx.raw_bytes()
    source_sha256 = hashlib.sha256(payload).hexdigest()
    manifest = _sibling_manifest(ctx.source_path, ctx.manifest.source)
    config = ctx.manifest.source
    licence = config.get("licence") if isinstance(config, dict) else None
    attribution = (
        config.get("attribution") if isinstance(config, dict) else None
    )
    manifest_name = None
    if manifest is not None:
        record = json.loads(manifest.read_text(encoding="utf-8"))
        licence = record.get("licence")
        attribution = record.get("attribution")
        manifest_name = manifest.name
    columns, rows = read_tabular_csv(ctx.source_path)
    source_name = ctx.source_path.stem
    ctx.state["columns"] = columns
    ctx.state["source_rows"] = rows
    ctx.state["source_name"] = source_name
    ctx.state["source_sha256"] = source_sha256
    ctx.state["licence"] = licence
    ctx.state["attribution"] = attribution
    ctx.event(
        "source_intake",
        "ok",
        {
            "source_path": str(ctx.source_path),
            "source_name": source_name,
            "source_sha256": source_sha256,
            "n_samples": len(rows),
            "licence": licence,
            "attribution": attribution,
            "manifest": manifest_name,
        },
    )
    ctx.say(
        f"[source_intake] {source_name}: {len(rows)} rows "
        f"sha256={source_sha256}"
    )
    if licence or attribution:
        ctx.say(f"[source_intake] licence={licence} attribution={attribution}")


def profile_source(ctx: Any) -> None:
    """Build summary statistics over the parsed rows."""
    profile = TabularProfile.from_rows(
        ctx.state["columns"],
        ctx.state["source_rows"],
        ctx.state["source_name"],
        ctx.state["source_sha256"],
    )
    ctx.state["profile"] = profile
    ctx.event(
        "profile",
        "ok",
        {
            "source_name": ctx.state["source_name"],
            "n_samples": len(ctx.state["source_rows"]),
        },
    )
    ctx.say(
        f"[profile] statistics built over {len(ctx.state['source_rows'])} rows"
    )


def plan(ctx: Any) -> None:
    """Ask the planner role for a generation plan (statistics only)."""
    profile = ctx.state["profile"]
    try:
        completion = propose(ctx.model("planner"), profile)
    except Exception as exc:  # noqa: BLE001 -- logged, then reported
        ctx.event("plan", "error", {"error": f"{type(exc).__name__}: {exc}"})
        ctx.say(f"[plan] error: {exc}")
        raise RuntimeError(f"plan failed: {exc}") from exc
    ctx.state["plan"] = dict(completion.data)
    ctx.state["attempt_usages"] = [completion.usage]
    ctx.event(
        "plan", "ok", {"plan": dict(completion.data)}, usage=completion.usage
    )
    ctx.say(
        f"[plan] seed={completion.data['seed']} n_rows={completion.data['n_rows']}"
    )


def generate(ctx: Any) -> None:
    """Generate one synthetic artifact from the current plan."""
    payload = generate_artifact(ctx.state["profile"], ctx.state["plan"])
    artifact = ctx.record_artifact("data.csv", "text/csv", payload)
    ctx.state["artifact_sha"] = artifact.sha256


def _profile_mean_column(ctx: Any) -> tuple[str | None, float | None]:
    numeric = ctx.state["profile"].stats.get("numeric", {})
    if not numeric:
        return None, None
    column = next(iter(numeric))
    return column, numeric[column]["mean"]


def validate(ctx: Any) -> None:
    """Read the artifact back and run the deterministic checks."""
    sha = ctx.state["artifact_sha"]
    payload = ctx.artifact_payload(sha)
    columns, rows = read_payload(payload)
    thresholds = load_thresholds(_thresholds_path())
    mean_column, profile_mean = _profile_mean_column(ctx)
    report = run_checks(
        columns,
        rows,
        thresholds,
        plan_n_rows=ctx.state["plan"]["n_rows"],
        profile_columns=ctx.state["profile"].stats.get("columns"),
        profile_mean=profile_mean,
        mean_column=mean_column,
        artifact_name=(
            f"{ctx.state['source_name']}:attempt-{ctx.attempt_number}"
        ),
        artifact_sha256=sha,
    )
    ctx.state["report"] = report
    ctx.state.setdefault("reports", {})[sha] = report
    catalog = _catalog(ctx)
    items = []
    for result in report.results:
        entry = catalog.get(result.name, {})
        threshold = entry.get("threshold", {"source": "literal"})
        items.append(
            {
                "check_id": result.name,
                "kind": entry.get("kind", "constraint"),
                "passed": result.status != "fail",
                "measured": result.detail,
                "threshold": threshold,
                "threshold_source": threshold.get("source", "literal")
                if isinstance(threshold, dict)
                else "literal",
                "detail": result.detail,
            }
        )
    ctx.record_checks(items)


def repair(ctx: Any) -> None:
    """Ask the planner role for a revised plan after failed checks."""
    profile = ctx.state["profile"]
    report = ctx.state["report"]
    failed_results = [
        {"name": r.name, "status": r.status, "detail": r.detail}
        for r in report.results
        if r.status == "fail"
    ]
    prompt = build_repair_prompt(
        profile, failed_results, ctx.state["plan"]
    )
    try:
        completion = ctx.model("planner").complete(
            prompt, PLAN_SCHEMA
        )
        new_plan = validate_plan(completion.data)
    except Exception as exc:  # noqa: BLE001 -- logged, then reported
        ctx.event("repair", "error", {"error": f"{type(exc).__name__}: {exc}"})
        ctx.say(f"[repair] error: {exc}")
        raise RuntimeError(f"repair failed: {exc}") from exc
    ctx.state["plan"] = dict(new_plan)
    ctx.state.setdefault("attempt_usages", []).append(completion.usage)
    ctx.event("repair", "ok", {"plan": dict(new_plan)},
              usage=completion.usage)


def describe(ctx: Any) -> None:
    """Record an advisory summary; it is logged but never affects pass/fail."""
    profile = ctx.state["profile"]
    payload = {
        "n_rows": profile.stats.get("n_rows"),
        "columns": list(profile.stats.get("columns", [])),
        "verdict": ctx.state.get("verdict"),
    }
    ctx.record_advisory("table_summary", payload)
    ctx.say(f"[describe] advisory summary recorded for {profile.source_name}")


def package_artifact(ctx: Any) -> None:
    """Write the artifact directory with data, plan, report, and log."""
    sha = ctx.state["artifact_sha256"]
    payload = ctx.artifact_payload(sha)
    plan = ctx.state["plan"]
    report = ctx.state["reports"][sha]
    artifact_dir = ctx.out_dir / "runs" / sha
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "data.csv").write_bytes(payload)
    (artifact_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (artifact_dir / "validation-report.json").write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "seed.txt").write_text(
        f"{plan['seed']}\n", encoding="utf-8"
    )
    (artifact_dir / "provenance.md").write_text(
        ctx.provenance_text(), encoding="utf-8"
    )
    ctx.state["artifact_dir"] = str(artifact_dir)
    ctx.event(
        "artifact", "ok", {"artifact_dir": str(artifact_dir)},
        artifact_sha256=sha,
    )
    ctx.flush_log(artifact_dir / "run.jsonl")
    ctx.say(f"[artifact] wrote {artifact_dir}")


def package_release(ctx: Any) -> None:
    """On human accept, write the reproducible release ZIP."""
    artifact_dir = Path(ctx.state["artifact_dir"])
    sha = ctx.state["artifact_sha256"]
    zip_path, zip_sha = ctx.pack_release(artifact_dir)
    ctx.event(
        "release",
        "ok",
        {"zip": str(zip_path), "zip_sha256": zip_sha},
        artifact_sha256=sha,
    )
    ctx.flush_log(artifact_dir / "run.jsonl")
    ctx.say(f"[release] wrote {zip_path}")
    ctx.say(f"[release] sha256={zip_sha}")


#: Capability implementations keyed by manifest capability id.
CAPABILITIES: dict[str, Any] = {
    "intake": intake,
    "profile_source": profile_source,
    "plan": plan,
    "generate": generate,
    "validate": validate,
    "repair": repair,
    "describe": describe,
    "package_artifact": package_artifact,
    "package_release": package_release,
}
