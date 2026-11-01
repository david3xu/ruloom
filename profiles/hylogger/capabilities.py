"""HyLogger capability implementations for the generic engine.

Each function receives a scoped run context from the engine and performs
one manifest step: intake, profiling, planning, generation, validation,
repair, imaging (intake, synthesis, validation, advisory critique), or
packaging. The model only ever sees profile statistics, never raw source
rows or raw pixels; the engine enforces that boundary from the manifest
permissions, and these functions never reach past their scoped context.

The context is duck-typed on purpose: this module imports no engine and
no model code, so the layer boundary stays intact.

Declared inputs per capability (frozen snapshots; validated writes only):

* ``intake``/``profile_source`` (raw-granted): source bytes/path and the
  parsed frame; intake verifies the source hash against the sibling
  manifest record (mismatch refuses loudly) and stores the source
  identity, the verification evidence, the frame, the statistics profile,
  and the source-derived fidelity reference the checks need.
* ``plan``/``repair`` (model-granted): profile and latest report; store
  generation plans.
* ``generate``: profile and plan; stores the artifact bytes.
* ``validate`` (verdict-granted): latest attempt artifact, profile, and
  reference statistics (never raw rows); stores the validation report
  and the engine-bound check results.
* ``image_*``: the companion bundle via the engine (``image_intake`` is
  raw-granted); image validation records engine-bound check results, and
  the image verdict folds into final acceptance through those results.
* ``image_critique``/``local_critique`` (advisory): synthetic evidence
  only; advisory output never decides.
* ``package_artifact``/``package_release``: the engine-selected artifact
  and evidence; write the artifact directory and the release ZIP.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .analysis.calibration import load_thresholds
from .validation.checks import (
    run_checks,
    source_fidelity_reference,
)
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


#: Profile check statuses translated to the engine's recognised verdict
#: statuses. A profile ``skip`` is recorded as engine ``skipped`` so it
#: can never read as a pass at acceptance.
_ENGINE_STATUS = {"pass": "pass", "fail": "fail", "skip": "skipped"}


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


def _plan_max_rows(ctx: Any) -> int | None:
    """The manifest-declared row maximum, when the profile declares one."""
    bounds = ctx.plan_bounds()
    ceiling = bounds.get("max_rows") if isinstance(bounds, dict) else None
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, int)
        or ceiling < 1
    ):
        return None
    return ceiling


def _catalog(ctx: Any) -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in ctx.declared_checks}


def _items(ctx: Any, report: Any) -> list[dict[str, Any]]:
    """Translate a profile check report to engine-bound check items."""
    catalog = _catalog(ctx)
    items = []
    for result in report.results:
        entry = catalog.get(result.name, {})
        threshold = entry.get("threshold", {"source": "literal"})
        items.append(
            {
                "check_id": result.name,
                "kind": entry.get("kind", "constraint"),
                "status": _ENGINE_STATUS[result.status],
                "measured": result.detail,
                "threshold": threshold,
                "threshold_source": threshold.get("source", "literal")
                if isinstance(threshold, dict)
                else "literal",
                "detail": result.detail,
                "band_position": getattr(result, "band_position", None),
            }
        )
    return items


def _verify_sibling_manifest(
    manifest: Path | None, source_sha256: str
) -> tuple[str | None, str | None, bool]:
    """Check the source bytes against the sibling manifest record.

    Returns ``(manifest_name, manifest_sha256, hash_verified)``. A recorded
    ``sha256`` that differs from the measured source hash fails loudly:
    the source is not the bytes the registry describes, so nothing
    downstream -- no model call, no release -- may treat its licence or
    attribution as verified. A missing manifest (or one recording no hash)
    is not fatal: the run proceeds, but the source stays unverified and
    the release carries no output-rights claim.
    """
    if manifest is None:
        return None, None, False
    manifest_bytes = manifest.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    record = json.loads(manifest_bytes.decode("utf-8"))
    recorded = record.get("sha256")
    if not isinstance(recorded, str) or not recorded:
        return manifest.name, manifest_sha256, False
    if recorded.lower() != source_sha256.lower():
        raise RuntimeError(
            f"source hash mismatch: {manifest.name} records "
            f"sha256 {recorded}, but the source bytes hash to "
            f"{source_sha256}: refusing (no verified source)"
        )
    return manifest.name, manifest_sha256, True


def intake(ctx: Any) -> None:
    """Hash the source, verify it against the registry, parse samples."""
    payload = ctx.raw_bytes()
    source_sha256 = hashlib.sha256(payload).hexdigest()
    manifest = _sibling_manifest(ctx.source_path, ctx.source_config)
    manifest_name, manifest_sha256, hash_verified = _verify_sibling_manifest(
        manifest, source_sha256
    )
    config = ctx.source_config
    licence = config.get("licence") if isinstance(config, dict) else None
    attribution = (
        config.get("attribution") if isinstance(config, dict) else None
    )
    output_terms_grant = None
    if manifest is not None:
        record = json.loads(manifest.read_text(encoding="utf-8"))
        licence = record.get("licence")
        attribution = record.get("attribution")
        output_terms_grant = record.get("output_terms")
    samples = read_scalar_csv(ctx.source_path)
    source_name = ctx.source_path.stem
    ctx.set_source_meta(
        name=source_name,
        sha256=source_sha256,
        licence=licence,
        attribution=attribution,
        manifest_name=manifest_name,
        manifest_sha256=manifest_sha256,
        hash_verified=hash_verified,
        output_terms_grant=output_terms_grant,
    )
    ctx.set_source_frame(list(samples))
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
            "manifest_sha256": manifest_sha256,
            "hash_verified": hash_verified,
            "output_terms_grant": output_terms_grant,
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
    samples = ctx.source_frame()
    meta = ctx.source_meta()
    profile = Profile.from_samples(
        samples,
        meta["source_name"],
        meta["source_sha256"],
    )
    ctx.set_profile(profile)
    # Source-derived fidelity references the checks need, computed once
    # here where raw access is granted; validation reads this statistics
    # view and never the rows.
    ctx.set_reference_stats(source_fidelity_reference(samples))
    ctx.event(
        "profile",
        "ok",
        {
            "source_name": meta["source_name"],
            "n_samples": len(samples),
        },
    )
    ctx.say(f"[profile] statistics built over {len(samples)} samples")


def plan(ctx: Any) -> None:
    """Ask the planner role for a generation plan (statistics only)."""
    profile = ctx.profile()
    try:
        completion = propose(
            ctx.model("planner"), profile, max_rows=_plan_max_rows(ctx)
        )
    except Exception as exc:  # noqa: BLE001 -- logged, then reported
        ctx.event("plan", "error", {"error": f"{type(exc).__name__}: {exc}"})
        ctx.say(f"[plan] error: {exc}")
        raise RuntimeError(f"plan failed: {exc}") from exc
    ctx.set_plan(dict(completion.data))
    ctx.event("plan", "ok", {"plan": dict(completion.data)},
              usage=completion.usage)
    ctx.say(
        f"[plan] seed={completion.data['seed']} n_rows={completion.data['n_rows']}"
    )


def generate(ctx: Any) -> None:
    """Generate one synthetic artifact from the current plan."""
    payload = generate_artifact(ctx.profile(), ctx.plan())
    ctx.record_artifact("data.csv", "text/csv", payload)


def validate(ctx: Any) -> None:
    """Read the artifact back and run the deterministic checks."""
    artifact = ctx.latest_artifact()
    payload = ctx.artifact_payload(artifact.sha256)
    try:
        samples = read_artifact(payload)
    except ValueError as exc:
        raise RuntimeError(
            f"synthetic artifact failed schema parse: {exc}"
        ) from exc
    thresholds = load_thresholds(_thresholds_path())
    meta = ctx.source_meta()
    report = run_checks(
        samples,
        thresholds,
        source_reference=ctx.reference_stats(),
        artifact_name=(
            f"{meta['source_name']}:attempt-{ctx.attempt_number}"
        ),
        artifact_sha256=artifact.sha256,
    )
    ctx.set_report(report)
    catalog = _catalog(ctx)
    schema_entry = catalog.get("csv_schema", {})
    schema_threshold = schema_entry.get("threshold", {"source": "literal"})
    items = [
        {
            "check_id": "csv_schema",
            "kind": schema_entry.get("kind", "constraint"),
            "status": "pass",
            "measured": f"{len(samples)} rows, header ok",
            "threshold": schema_threshold,
            "threshold_source": schema_threshold.get("source", "literal")
            if isinstance(schema_threshold, dict)
            else "literal",
            "detail": f"{len(samples)} rows, header ok",
        }
    ]
    items.extend(_items(ctx, report))
    ctx.record_checks(items)


def repair(ctx: Any) -> None:
    """Ask the planner role for a revised plan after failed checks."""
    profile = ctx.profile()
    report = ctx.latest_report()
    failed_results = [r for r in report.results if r.status == "fail"]
    prompt = build_repair_prompt(
        profile, failed_results, ctx.plan()
    )
    try:
        completion = ctx.model("planner").complete(
            prompt, PLAN_SCHEMA
        )
        new_plan = validate_plan(
            completion.data, max_rows=_plan_max_rows(ctx)
        )
    except Exception as exc:  # noqa: BLE001 -- logged, then reported
        ctx.event("repair", "error", {"error": f"{type(exc).__name__}: {exc}"})
        ctx.say(f"[repair] error: {exc}")
        raise RuntimeError(f"repair failed: {exc}") from exc
    ctx.set_plan(dict(new_plan))
    ctx.event("repair", "ok", {"plan": dict(new_plan)},
              usage=completion.usage)


def image_intake(ctx: Any) -> None:
    """Hash and attribute the optional image directory (no-op without it).

    With ``--images DIR`` every PNG/JPEG is hashed, typed, and
    attributed from its sibling manifest, and code-only statistics are
    computed for each PNG. Without the flag this step returns silently
    so the default run is byte-identical to a run without image steps.
    """
    images_dir = ctx.images_dir
    if images_dir is None:
        return
    records = inventory_images(images_dir)
    stats, skipped = read_statistics(images_dir)
    ctx.set_companion(
        {
            "records": [record.to_dict() for record in records],
            "stats": [item.to_dict() for item in stats],
            "skipped": list(skipped),
            "reference": stats[0].to_dict() if stats else None,
        }
    )
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
    bundle = ctx.companion()
    reference = bundle.get("reference")
    if reference is None:
        return
    plan = ctx.plan()
    seed = plan.get("seed", 0)
    payload = synthesize_image(reference, int(seed))
    artifact = ctx.record_artifact("synthetic.png", "image/png", payload)
    bundle["image_artifact_sha"] = artifact.sha256
    ctx.set_companion(bundle)
    ctx.event(
        "image_synthesize",
        "ok",
        {"artifact_sha256": artifact.sha256, "seed": int(seed)},
        artifact_sha256=artifact.sha256,
    )
    ctx.say(f"[image_synthesize] synthetic.png sha256={artifact.sha256}")


def image_validate(ctx: Any) -> None:
    """Run the deterministic image checks on the synthetic PNG.

    Image failures reach the run verdict only through the engine-bound
    check results recorded here; advisory critique output can never do
    this. The image report itself rides in the companion bundle, never
    in the validation-report slot owned by the loop checks.
    """
    bundle = ctx.companion()
    sha = bundle.get("image_artifact_sha")
    reference = bundle.get("reference")
    if sha is None or reference is None:
        return
    payload = ctx.artifact_payload(sha)
    report = run_image_checks(
        ImageStats.from_dict(reference),
        payload,
        artifact_name="synthetic.png",
        artifact_sha256=sha,
    )
    bundle["report"] = report.to_dict()
    ctx.set_companion(bundle)
    ctx.record_checks(_items(ctx, report))
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
    failure here -- including budget exhaustion -- propagates to the
    engine, which applies the manifest ``critic_policy`` for this
    capability (``optional``: recorded visibly as an advisory failure
    and the run continues; ``required``: the run fails loudly). The
    run verdict is untouched either way: it never reads advisories.
    """
    import tempfile

    bundle = ctx.companion()
    sha = bundle.get("image_artifact_sha")
    reference = bundle.get("reference")
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
        client = ctx.model("critic")
        note = critique_synthetic(client, synthetic_path, summary)
        ctx.record_advisory("vision_note", note)
        ctx.say("[image_critique] advisory vision note recorded")
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
    if not ctx.local_critic_enabled:
        return
    report = ctx.latest_report()
    profile = ctx.profile()
    note = critique_report(
        ctx.model("local_critic"), report.to_dict(), profile.to_disclosed_json()
    )
    ctx.record_advisory("local_critique_note", note)
    ctx.event("local_critique", "ok", {"model": note.get("model")})
    ctx.say("[local_critique] advisory text note recorded")


def package_artifact(ctx: Any) -> None:
    """Write the artifact directory with data, plan, report, and log.

    The plan and seed written here are the engine-selected attempt's
    own: the current plan may belong to a later, worse attempt, while
    only the selected record replays the packaged artifact.
    """
    sha = ctx.selected_artifact_sha()
    payload = ctx.artifact_payload(sha)
    plan = ctx.selected_plan()
    report = ctx.report_for(sha)
    artifact_dir = ctx.prepare_artifact_dir()
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
    bundle = ctx.companion()
    if bundle.get("image_artifact_sha") is not None:
        (artifact_dir / "synthetic.png").write_bytes(
            ctx.artifact_payload(bundle["image_artifact_sha"])
        )
        (artifact_dir / "image-statistics.json").write_text(
            json.dumps(
                {
                    "reference": bundle.get("reference"),
                    "skipped": bundle.get("skipped", []),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if bundle.get("report") is not None:
            (artifact_dir / "image-validation.json").write_text(
                json.dumps(
                    bundle["report"], indent=2, sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )
    note = ctx.advisory("local_critique_note")
    if note is not None:
        (artifact_dir / "critic-note.json").write_text(
            json.dumps(
                note, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
    ctx.event(
        "artifact", "ok", {"artifact_dir": str(artifact_dir)},
        artifact_sha256=sha,
    )
    ctx.flush_log(artifact_dir / "run.jsonl")
    ctx.say(f"[artifact] wrote {artifact_dir}")


def package_release(ctx: Any) -> None:
    """On human accept, write the reproducible release ZIP."""
    artifact_dir = Path(ctx.artifact_dir())
    sha = ctx.selected_artifact_sha()
    zip_path, zip_sha = ctx.pack_release()
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
