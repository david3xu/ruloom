"""HyLogger capability implementations for the generic engine.

Each function receives a scoped run context from the engine and performs
one manifest step: intake, profiling, planning, generation, validation,
repair, imaging (intake, synthesis, validation, advisory critique), or
packaging. The model only ever sees profile statistics, never raw source
rows or raw pixels; the engine enforces that boundary from the manifest
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
from .validation.dataset import read as read_artifact
from .generation.generate import generate as generate_artifact
from .generation.plan import PLAN_SCHEMA, propose, validate_plan
from .generation.repair import build_repair_prompt
from .analysis.statistics import Profile
from .intake.reader import read_scalar_csv
from .imaging.checks import run_image_checks
from .imaging.critic import critique_synthetic
from .imaging.generate import synthesize as synthesize_image
from .imaging.intake import inventory_images
from .imaging.statistics import (
    ImageStats,
    read_statistics,
    summarize_for_prompt,
)
from .validation.critique import critique_report


def _thresholds_path() -> Path:
    """Committed thresholds, overridable for tests via RULOOM_THRESHOLDS."""
    override = os.environ.get("RULOOM_THRESHOLDS")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "thresholds.json"


def _sibling_manifest(source_path: Path, config: Any) -> Path | None:
    """Sibling ``*-source.json`` manifest for a source CSV."""
    data_suffix = (
        config.get("data_suffix", "-scalars.csv")
        if isinstance(config, dict)
        else "-scalars.csv"
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
    """Hash the source, record licence/attribution, parse samples."""
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
    samples = read_scalar_csv(ctx.source_path)
    source_name = ctx.source_path.stem
    ctx.state["samples"] = samples
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
            "n_samples": len(samples),
            "licence": licence,
            "attribution": attribution,
            "manifest": manifest_name,
        },
    )
    ctx.say(
        f"[source_intake] {source_name}: {len(samples)} rows "
        f"sha256={source_sha256}"
    )
    if licence or attribution:
        ctx.say(f"[source_intake] licence={licence} attribution={attribution}")


def profile_source(ctx: Any) -> None:
    """Build summary statistics over the parsed samples."""
    profile = Profile.from_samples(
        ctx.state["samples"],
        ctx.state["source_name"],
        ctx.state["source_sha256"],
    )
    ctx.state["profile"] = profile
    ctx.event(
        "profile",
        "ok",
        {
            "source_name": ctx.state["source_name"],
            "n_samples": len(ctx.state["samples"]),
        },
    )
    ctx.say(f"[profile] statistics built over {len(ctx.state['samples'])} samples")


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
    ctx.event("plan", "ok", {"plan": dict(completion.data)},
              usage=completion.usage)
    ctx.say(
        f"[plan] seed={completion.data['seed']} n_rows={completion.data['n_rows']}"
    )


def generate(ctx: Any) -> None:
    """Generate one synthetic artifact from the current plan."""
    payload = generate_artifact(ctx.state["profile"], ctx.state["plan"])
    artifact = ctx.record_artifact("data.csv", "text/csv", payload)
    ctx.state["artifact_sha"] = artifact.sha256


def validate(ctx: Any) -> None:
    """Read the artifact back and run the deterministic checks."""
    sha = ctx.state["artifact_sha"]
    payload = ctx.artifact_payload(sha)
    samples = read_artifact(payload)
    thresholds = load_thresholds(_thresholds_path())
    report = run_checks(
        samples,
        thresholds,
        source_samples=list(ctx.state["samples"]),
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
    failed_results = [r for r in report.results if r.status == "fail"]
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


def image_intake(ctx: Any) -> None:
    """Hash and attribute the optional image directory (no-op without it).

    With ``--images DIR`` every PNG/JPEG is hashed, typed, and
    attributed from its sibling manifest, and code-only statistics are
    computed for each PNG. Without the flag this step returns silently
    so the default run is byte-identical to a run without image steps.
    """
    images_dir = getattr(ctx, "images_dir", None)
    if images_dir is None:
        return
    records = inventory_images(images_dir)
    stats, skipped = read_statistics(images_dir)
    ctx.state["image_records"] = [record.to_dict() for record in records]
    ctx.state["image_stats"] = [item.to_dict() for item in stats]
    ctx.state["image_skipped"] = list(skipped)
    ctx.state["image_reference"] = stats[0].to_dict() if stats else None
    ctx.event(
        "image_intake",
        "ok",
        {
            "images_dir": str(images_dir),
            "n_images": len(records),
            "n_statistics": len(stats),
            "skipped": list(skipped),
        },
    )
    ctx.say(
        f"[image_intake] {len(records)} images "
        f"({len(stats)} with statistics, {len(skipped)} skipped)"
    )


def image_synthesize(ctx: Any) -> None:
    """Render the synthetic PNG from reference statistics and plan seed."""
    reference = ctx.state.get("image_reference")
    if reference is None:
        return
    plan = ctx.state.get("plan") or {}
    seed = plan.get("seed", 0)
    payload = synthesize_image(reference, int(seed))
    artifact = ctx.record_artifact("synthetic.png", "image/png", payload)
    ctx.state["image_artifact_sha"] = artifact.sha256
    ctx.event(
        "image_synthesize",
        "ok",
        {"artifact_sha256": artifact.sha256, "seed": int(seed)},
        artifact_sha256=artifact.sha256,
    )
    ctx.say(f"[image_synthesize] synthetic.png sha256={artifact.sha256}")


def image_validate(ctx: Any) -> None:
    """Run the deterministic image checks on the synthetic PNG."""
    sha = ctx.state.get("image_artifact_sha")
    reference = ctx.state.get("image_reference")
    if sha is None or reference is None:
        return
    payload = ctx.artifact_payload(sha)
    report = run_image_checks(
        ImageStats.from_dict(reference),
        payload,
        artifact_name="synthetic.png",
        artifact_sha256=sha,
    )
    ctx.state["image_report"] = report.to_dict()
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
    if report.verdict == "fail":
        # Deterministic image checks validate: fold failures into the run
        # verdict (the shape below matches EngineCheckResult serialised
        # entries so the gate reports them like any other failure).
        # Advisory critique output can never do this — only checks can.
        ctx.state["failed"] = sorted(
            set(ctx.state.get("failed", [])) | set(report.failed)
        )
        ctx.state["verdict"] = "fail"
        best = ctx.state.get("best_attempt")
        if isinstance(best, dict):
            entries = best.setdefault("check_results", [])
            for result in report.results:
                if result.status == "fail":
                    entries.append(
                        {
                            "check_id": result.name,
                            "kind": catalog.get(result.name, {}).get(
                                "kind", "constraint"
                            ),
                            "passed": False,
                            "measured": result.detail,
                            "threshold": {"source": "literal"},
                            "threshold_source": "literal",
                            "artifact_sha256": sha,
                            "verifier": "image_validate",
                            "detail": result.detail,
                        }
                    )
    ctx.event(
        "image_validate",
        "ok" if report.verdict == "pass" else "fail",
        {"verdict": report.verdict, "failed": report.failed},
        artifact_sha256=sha,
    )
    ctx.say(f"[image_validate] verdict={report.verdict}")


def image_critique(ctx: Any) -> None:
    """Advisory vision note on the synthetic PNG (never a verdict).

    The vision role sees the rendered synthetic artifact only. Any
    failure here is recorded as an advisory error payload; the run
    verdict is untouched either way.
    """
    import tempfile

    sha = ctx.state.get("image_artifact_sha")
    reference = ctx.state.get("image_reference")
    if sha is None or reference is None:
        return
    payload = ctx.artifact_payload(sha)
    summary = summarize_for_prompt(ImageStats.from_dict(reference))
    with tempfile.NamedTemporaryFile(
        suffix=".png", prefix="ruloom-synthetic-", delete=False
    ) as handle:
        handle.write(payload)
        synthetic_path = Path(handle.name)
    try:
        try:
            client = ctx.model("critic")
            note = critique_synthetic(client, synthetic_path, summary)
            ctx.record_advisory("vision_note", note)
            ctx.say("[image_critique] advisory vision note recorded")
        except Exception as exc:  # noqa: BLE001 -- advisory, never fatal
            ctx.record_advisory(
                "vision_note",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            ctx.say(f"[image_critique] advisory critique failed: {exc}")
    finally:
        synthetic_path.unlink(missing_ok=True)


def local_critique(ctx: Any) -> None:
    """Advisory text note on the validation report (never a verdict).

    Off by default: without the ``--local-critic`` flag this step returns
    silently, so the default run is byte-identical to a run without it.
    With the flag on, the ``local_critic`` role reads the validation
    report plus the profile statistics summary only -- never raw rows,
    never images. Any failure propagates, so an unreachable local server
    fails the run loudly instead of silently skipping the critique.
    """
    if not getattr(ctx, "local_critic_enabled", False):
        return
    report = ctx.state["report"]
    profile = ctx.state["profile"]
    note = critique_report(
        ctx.model("local_critic"), report.to_dict(), profile.to_json()
    )
    ctx.state["local_critique_note"] = note
    ctx.record_advisory("local_critique_note", note)
    ctx.event("local_critique", "ok", {"model": note.get("model")})
    ctx.say("[local_critique] advisory text note recorded")


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
    if ctx.state.get("image_artifact_sha") is not None:
        (artifact_dir / "synthetic.png").write_bytes(
            ctx.artifact_payload(ctx.state["image_artifact_sha"])
        )
        (artifact_dir / "image-statistics.json").write_text(
            json.dumps(
                {
                    "reference": ctx.state.get("image_reference"),
                    "skipped": ctx.state.get("image_skipped", []),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if ctx.state.get("image_report") is not None:
            (artifact_dir / "image-validation.json").write_text(
                json.dumps(
                    ctx.state["image_report"], indent=2, sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )
    if ctx.state.get("local_critique_note") is not None:
        (artifact_dir / "critic-note.json").write_text(
            json.dumps(
                ctx.state["local_critique_note"], indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
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
    "image_intake": image_intake,
    "image_synthesize": image_synthesize,
    "image_validate": image_validate,
    "image_critique": image_critique,
    "local_critique": local_critique,
    "package_artifact": package_artifact,
    "package_release": package_release,
}
