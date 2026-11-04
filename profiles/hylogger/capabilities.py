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
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analysis.calibration import load_thresholds
from .analysis.statistics import Profile
from .generation.generate import generate as generate_artifact
from .generation.plan import propose, provider_schema, validate_plan
from .generation.repair import build_repair_prompt
from .imaging.checks import run_image_checks
from .imaging.critic import (
    EVIDENCE_NOT_DISPATCHED,
    STATUS_UNAVAILABLE,
    CriticError,
    build_failure_receipt,
    critique_synthetic,
    failure_receipt_for,
    validate_receipt,
)
from .imaging.generate import synthesize as synthesize_image
from .imaging.intake import inventory_images
from .imaging.scene import build_schematic_scene
from .imaging.statistics import (
    ImageStats,
    read_statistics,
    summarize_for_prompt,
)
from .intake.reader import read_scalar_csv
from .validation.cases import (
    assess_source_suitability,
    build_source_reference,
    check_source_joint_satisfiability,
    get_case,
    load_cases_catalogue,
    validate_plan_satisfiability,
    validate_source_reference,
)
from .validation.checks import (
    run_checks,
    source_fidelity_reference,
)
from .validation.critique import critique_report
from .validation.dataset import read as read_artifact
from .validation.workflow import (
    assess_case_source,
    build_case_members,
    case_check_results,
    evaluate_case_context,
    repair_case_plan,
    research_case,
    review_case_candidate,
    verify_case_members,
)
from .validation.workflow import (
    prepare_case_context as _workflow_prepare_case_context,
)

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
        candidate = source_path.with_name(name[: -len(data_suffix)] + manifest_suffix)
    else:
        candidate = source_path.with_name(source_path.stem + manifest_suffix)
    return candidate if candidate.is_file() else None


def _plan_max_rows(ctx: Any) -> int | None:
    """The manifest-declared row maximum, when the profile declares one."""
    bounds = ctx.plan_bounds()
    ceiling = bounds.get("max_rows") if isinstance(bounds, dict) else None
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
        return None
    return ceiling


def _spec_strategy(ctx: Any) -> str | None:
    """The run spec's plan strategy (the run default when plans omit it)."""
    spec = ctx.spec_record()
    strategy = spec.get("strategy") if isinstance(spec, dict) else None
    if isinstance(strategy, str) and strategy:
        return strategy
    return None


def _allowed_strategies(ctx: Any) -> list[str] | None:
    """The single-strategy bound the planner must stay inside, if any."""
    strategy = _spec_strategy(ctx)
    return [strategy] if strategy is not None else None


def _spec_max_rows(ctx: Any) -> int | None:
    """The run spec's row bound (already tightened against the manifest)."""
    spec = ctx.spec_record()
    limits = spec.get("limits") if isinstance(spec, dict) else None
    ceiling = limits.get("max_rows") if isinstance(limits, dict) else None
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
        return _plan_max_rows(ctx)
    return ceiling


def _supported_options(ctx: Any) -> dict[str, Any]:
    """The corrective options a repair proposal may choose within."""
    spec = ctx.spec_record() if isinstance(ctx.spec_record(), dict) else {}
    strategy = _spec_strategy(ctx)
    return {
        "strategies": [strategy] if strategy is not None else [],
        "max_rows": _spec_max_rows(ctx),
        "target_rows": spec.get("target_rows"),
        "outputs": list(spec.get("outputs", ["scalar"])),
    }


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


def _check_source_pin(ctx: Any, source_sha256: str) -> None:
    """Refuse a measured source that misses the spec's expected pin.

    The spec may pin the expected source SHA-256; when it does, intake
    verifies the measured bytes against that pin before anything
    downstream runs -- before any model call and before any release
    may treat the bytes as the pinned source. A missing pin (null)
    leaves the bytes unchecked here (the sibling-manifest registry
    check below still applies).
    """
    spec = ctx.spec_record()
    source = spec.get("source") if isinstance(spec, dict) else None
    pin = source.get("sha256") if isinstance(source, dict) else None
    if not isinstance(pin, str) or not pin:
        return
    if pin.lower() != source_sha256.lower():
        raise RuntimeError(
            f"source hash mismatch: the spec pins source sha256 "
            f"{pin}, but the source bytes hash to "
            f"{source_sha256}: refusing (no verified source)"
        )


def intake(ctx: Any) -> None:
    """Hash the source, verify it against the registry, parse samples."""
    payload = ctx.raw_bytes()
    source_sha256 = hashlib.sha256(payload).hexdigest()
    _check_source_pin(ctx, source_sha256)
    manifest = _sibling_manifest(ctx.source_path, ctx.source_config)
    manifest_name, manifest_sha256, hash_verified = _verify_sibling_manifest(
        manifest, source_sha256
    )
    config = ctx.source_config
    licence = config.get("licence") if isinstance(config, dict) else None
    attribution = config.get("attribution") if isinstance(config, dict) else None
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
    spec = ctx.spec_record()
    if isinstance(spec, dict) and spec.get("case") is not None:
        source_pin = (
            spec.get("source", {}).get("sha256")
            if isinstance(spec.get("source"), dict)
            else None
        )
        case_spec = {
            "case": spec["case"],
            "target_rows": spec.get("target_rows"),
            "source": {"sha256": source_pin},
            "outputs": spec.get("outputs"),
        }
        case_ctx = prepare_case_context(
            ctx.source_path,
            case_spec,
            licence_meta=licence,
            thresholds_path=_thresholds_path(),
        )
        ctx.set_requirements(case_ctx)
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
        f"[source_intake] {source_name}: {len(samples)} rows sha256={source_sha256}"
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
    """Ask the planner role for a generation plan (statistics only).

    The proposal must stay inside the spec-supported bounded schema:
    the row bound and the strategy set come from the run spec, and an
    out-of-spec proposal is refused before any generation runs. When
    the caller supplied a fixed plan for replay (``--plan``), that
    saved plan is validated against the same bounds and stored with
    no planner call; repair after a failed replay still uses the
    planner.
    """
    profile = ctx.profile()
    fixed = ctx.fixed_plan() if callable(getattr(ctx, "fixed_plan", None)) else None
    if fixed is not None:
        try:
            candidate = dict(fixed)
            spec_strategy = _spec_strategy(ctx)
            if "strategy" not in candidate and spec_strategy is not None:
                candidate["strategy"] = spec_strategy
            replayed = validate_plan(
                candidate,
                max_rows=_spec_max_rows(ctx),
                allowed_strategies=_allowed_strategies(ctx),
            )
        except Exception as exc:  # noqa: BLE001 -- logged, then reported
            ctx.event("plan", "error", {"error": f"{type(exc).__name__}: {exc}"})
            ctx.say(f"[plan] error: {exc}")
            raise RuntimeError(f"plan failed: {exc}") from exc
        ctx.set_plan(dict(replayed))
        ctx.event("plan", "ok", {"plan": dict(replayed), "replay": True})
        ctx.say(
            f"[plan] replay seed={replayed['seed']} "
            f"n_rows={replayed['n_rows']} strategy={replayed.get('strategy')} "
            f"(fixed plan, no planner call)"
        )
        return
    try:
        completion = propose(
            ctx.model("planner"),
            profile,
            max_rows=_spec_max_rows(ctx),
            allowed_strategies=_allowed_strategies(ctx),
            target_rows=ctx.spec_record().get("target_rows"),
        )
    except Exception as exc:  # noqa: BLE001 -- logged, then reported
        ctx.event("plan", "error", {"error": f"{type(exc).__name__}: {exc}"})
        ctx.say(f"[plan] error: {exc}")
        raise RuntimeError(f"plan failed: {exc}") from exc
    proposed = dict(completion.data)
    spec_strategy = _spec_strategy(ctx)
    if "strategy" not in proposed and spec_strategy is not None:
        proposed["strategy"] = spec_strategy
    ctx.set_plan(dict(proposed))
    ctx.event("plan", "ok", {"plan": dict(proposed)}, usage=completion.usage)
    ctx.say(
        f"[plan] seed={proposed['seed']} n_rows={proposed['n_rows']} "
        f"strategy={proposed.get('strategy')}"
    )


def generate(ctx: Any) -> None:
    """Generate one synthetic artifact from the current plan."""
    payload = generate_artifact(ctx.profile(), ctx.plan())
    ctx.record_artifact("data.csv", "text/csv", payload)


def _expected_geometry(ctx: Any) -> dict[str, Any] | None:
    """The requested plan geometry for the plan-binding checks.

    The plan carries the requested row count and the profile carries
    the fitted start depth and spacing step (the same step selection
    the seeded generator uses), so validation can refuse an artifact
    whose row count, start depth, or end extent does not match what
    was requested. ``None`` when the plan or profile cannot supply
    all three numbers; the checks then skip as unsupported.
    """
    plan = ctx.plan()
    stats = getattr(ctx.profile(), "stats", None)
    if not isinstance(plan, dict) or not isinstance(stats, dict):
        return None
    geometry = stats.get("geometry")
    spacing = geometry.get("spacing_m") if isinstance(geometry, dict) else None
    step = None
    if isinstance(spacing, dict):
        step = spacing.get("p50") or spacing.get("mean")
    n_rows = plan.get("n_rows")
    start = geometry.get("depth_start_m") if isinstance(geometry, dict) else None
    if (
        isinstance(n_rows, bool)
        or not isinstance(n_rows, int)
        or not isinstance(start, (int, float))
        or isinstance(start, bool)
        or not isinstance(step, (int, float))
        or isinstance(step, bool)
    ):
        return None
    return {"n_rows": n_rows, "start_m": float(start), "step_m": float(step)}


def validate(ctx: Any) -> None:
    """Read the artifact back and run the deterministic checks."""
    artifact = ctx.latest_artifact()
    payload = ctx.artifact_payload(artifact.sha256)
    try:
        samples = read_artifact(payload)
    except ValueError as exc:
        raise RuntimeError(f"synthetic artifact failed schema parse: {exc}") from exc
    thresholds = load_thresholds(_thresholds_path())
    meta = ctx.source_meta()
    plan = ctx.plan()
    strategy = plan.get("strategy") if isinstance(plan, dict) else None
    if strategy is None:
        strategy = _spec_strategy(ctx)
    expected_valid_count: int | None = None
    if strategy == "fitted_markov_balanced":
        stats = getattr(ctx.profile(), "stats", None)
        validity = stats.get("validity") if isinstance(stats, dict) else None
        vf = validity.get("valid_fraction") if isinstance(validity, dict) else None
        n_rows = plan.get("n_rows") if isinstance(plan, dict) else None
        if (
            isinstance(n_rows, int)
            and not isinstance(n_rows, bool)
            and isinstance(vf, (int, float))
            and not isinstance(vf, bool)
            and math.isfinite(float(vf))
        ):
            expected_valid_count = int(round(n_rows * float(vf)))
        else:
            expected_valid_count = -1
    report = run_checks(
        samples,
        thresholds,
        source_reference=ctx.reference_stats(),
        expected_geometry=_expected_geometry(ctx),
        expected_valid_count=expected_valid_count,
        artifact_name=(f"{meta['source_name']}:attempt-{ctx.attempt_number}"),
        artifact_sha256=artifact.sha256,
    )
    req = ctx.requirements()
    case_items = []
    if req is not None:
        case_utility = evaluate_case_context(payload, req)
        setattr(report, "case_utility", case_utility)
        case_items = case_check_results(case_utility)
    ctx.set_report(report)
    items = _items(ctx, report)
    if case_items:
        items.extend(case_items)
    ctx.record_checks(items)


def repair(ctx: Any) -> None:
    """Ask the planner role for a revised plan after failed checks.

    The repair prompt carries only the failed measured properties plus
    the supported corrective options, and the revised plan is validated
    against the same spec-supported bounds as the initial plan.
    """
    profile = ctx.profile()
    report = ctx.latest_report()
    failed_results = [r for r in report.results if r.status == "fail"]
    prompt = build_repair_prompt(
        profile, failed_results, ctx.plan(), _supported_options(ctx)
    )
    try:
        completion = ctx.model("planner").complete(
            prompt, provider_schema(_allowed_strategies(ctx))
        )
        candidate = dict(completion.data)
        spec_strategy = _spec_strategy(ctx)
        if "strategy" not in candidate and spec_strategy is not None:
            candidate["strategy"] = spec_strategy
        new_plan = validate_plan(
            candidate,
            max_rows=_spec_max_rows(ctx),
            allowed_strategies=_allowed_strategies(ctx),
        )
    except Exception as exc:  # noqa: BLE001 -- logged, then reported
        ctx.event("repair", "error", {"error": f"{type(exc).__name__}: {exc}"})
        ctx.say(f"[repair] error: {exc}")
        raise RuntimeError(f"repair failed: {exc}") from exc
    ctx.set_plan(dict(new_plan))
    ctx.event("repair", "ok", {"plan": dict(new_plan)}, usage=completion.usage)


def case_source_assess(ctx: Any) -> None:
    """Advisory source assessment by the source_assessor role."""
    req = ctx.requirements()
    if req is None:
        return
    if ctx.model_policy == "offline":
        ctx.record_advisory(
            "case_source_assess",
            {"status": "unavailable", "reason": "model policy is offline"},
        )
        ctx.event(
            "case_source_assess",
            "unavailable",
            {"reason": "model policy is offline"},
        )
        return
    model_client = ctx.model("source_assessor")
    advisory = assess_case_source(model_client, req)
    ctx.record_advisory("case_source_assess", advisory)
    ctx.event("case_source_assess", "ok", {"case_id": advisory.get("case_id")})


def case_research(ctx: Any) -> None:
    """Advisory case research by the case_researcher role."""
    req = ctx.requirements()
    if req is None:
        return
    spec = ctx.spec_record()
    purpose = None
    if isinstance(spec, dict):
        purpose = spec.get("purpose") or spec.get("request")
    if not isinstance(purpose, str) or not purpose.strip():
        purpose = "teaching lesson"
    if ctx.model_policy == "offline":
        ctx.record_advisory(
            "case_research",
            {"status": "unavailable", "reason": "model policy is offline"},
        )
        ctx.event(
            "case_research",
            "unavailable",
            {"reason": "model policy is offline"},
        )
        return
    model_client = ctx.model("case_researcher")
    advisory = research_case(model_client, purpose, req)
    unsupported = advisory.get("unsupported_requirements", [])
    if unsupported:
        raise RuntimeError(
            f"case research identified unsupported requirements: {unsupported}"
        )
    ctx.record_advisory("case_research", advisory)
    ctx.event("case_research", "ok", {"case_id": advisory.get("case_id")})


def case_repair(ctx: Any) -> None:
    """Causal, same-seed, same-target plan repair for case runs."""
    req = ctx.requirements()
    if req is None:
        return
    report = ctx.latest_report()
    plan = ctx.plan()
    failed_ids = set(report.failed)
    case_utility = getattr(report, "case_utility", None)
    if case_utility and isinstance(case_utility.get("failures"), (list, tuple, set)):
        for f in case_utility["failures"]:
            s = str(f)
            failed_ids.add(s)
            if not s.startswith("case."):
                failed_ids.add(f"case.{s}")
            else:
                failed_ids.add(s.removeprefix("case."))
    repaired = repair_case_plan(plan, sorted(failed_ids), req)
    ctx.set_plan(repaired)
    ctx.event("case_repair", "ok", {"strategy": repaired.get("strategy")})


def case_candidate_review(ctx: Any) -> None:
    """Advisory candidate review by the case_reviewer role."""
    req = ctx.requirements()
    if req is None:
        return
    sha = ctx.selected_artifact_sha()
    if sha is None:
        return
    payload = ctx.artifact_payload(sha)
    report = ctx.report_for(sha)
    case_utility = getattr(report, "case_utility", None)
    if case_utility is None:
        return
    if ctx.model_policy == "offline":
        ctx.record_advisory(
            "case_candidate_review",
            {"status": "unavailable", "reason": "model policy is offline"},
        )
        ctx.event(
            "case_candidate_review",
            "unavailable",
            {"reason": "model policy is offline"},
        )
        return
    model_client = ctx.model("case_reviewer")
    advisory = review_case_candidate(model_client, payload, req, case_utility)
    ctx.record_advisory("case_candidate_review", advisory)
    ctx.event("case_candidate_review", "ok", {"case_id": advisory.get("case_id")})


class ImageIntakeError(ValueError):
    """``--images`` was requested but cannot yield usable image statistics."""


def image_intake(ctx: Any) -> None:
    """Hash and attribute the optional image directory (no-op without it).

    With ``--images DIR`` every PNG/JPEG is hashed, typed, and
    attributed from its sibling manifest, its source hash is registered
    with the engine (so no copy of it can ever be attached to a model
    call), and code-only statistics are computed for each PNG. Without
    the flag this step returns silently so the default run is
    byte-identical to a run without image steps.

    The image extension is separate from the admitted scalar teaching
    case: a run selecting a scalar case together with ``--images`` is
    refused. An image request that yields no images, or no decodable
    PNG statistics, fails loudly instead of degrading to a scalar-only
    success.
    """
    images_dir = ctx.images_dir
    if images_dir is None:
        return
    spec = ctx.spec_record()
    if isinstance(spec, Mapping) and spec.get("case") is not None:
        raise ImageIntakeError(
            "image-enabled runs are a separate extension: a selected scalar "
            "teaching case cannot be combined with --images"
        )
    records = inventory_images(images_dir)
    if not records:
        raise ImageIntakeError(
            "--images was requested but the directory holds no PNG/JPEG images"
        )
    for record in records:
        ctx.register_source_artifact(record.sha256, record.name)
    stats, skipped = read_statistics(images_dir)
    if not stats:
        raise ImageIntakeError(
            "--images was requested but no image yielded decodable PNG statistics"
        )
    inventoried = {record.name: record.sha256 for record in records}
    for item in stats:
        ctx.register_source_artifact(item.sha256, item.name)
        if inventoried.get(item.name) != item.sha256:
            raise ImageIntakeError("source image changed during statistics derivation")
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
    """Render the synthetic PNG from reference statistics and plan seed.

    Also builds the shared schematic scene record (one entry per
    strip: sample id, schematic depth interval, image id, pixel band,
    orientation) that the table and image views both derive from;
    schematic-only, never a real core-photo claim.
    """
    bundle = ctx.companion()
    reference = bundle.get("reference")
    if reference is None:
        return
    plan = ctx.plan()
    seed = plan.get("seed", 0)
    payload = synthesize_image(reference, int(seed))
    artifact = ctx.record_artifact("synthetic.png", "image/png", payload)
    bundle["image_artifact_sha"] = artifact.sha256
    records = bundle.get("records") or []
    image_id = records[0]["name"] if records else "synthetic.png"
    bundle["scene"] = build_schematic_scene(reference, image_id).to_dict()
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
        scene=bundle.get("scene"),
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


def _record_vision_receipt(
    ctx: Any, bundle: dict[str, Any], receipt: Mapping[str, Any]
) -> None:
    """Store one sealed receipt in the companion bundle and advisory log."""
    bundle["vision_advisory"] = dict(receipt)
    ctx.set_companion(bundle)
    ctx.record_advisory("vision_note", dict(receipt))


def image_critique(ctx: Any) -> None:
    """Advisory vision note on the synthetic PNG (never a verdict).

    The vision role sees the rendered synthetic artifact only. Every
    outcome is recorded as a sealed version-1 receipt: ``ok`` for a
    strictly validated answer, ``unavailable`` / ``advisory-failed``
    otherwise. A failure receipt is recorded first and the original
    exception is then re-raised unchanged (``BudgetExceeded`` keeps its
    class), so the engine applies the manifest ``critic_policy``
    (``optional``: recorded and the run continues; ``required``: the run
    fails loudly). Offline policy is refused by the engine's model
    policy, never treated as a success. The run verdict is untouched
    either way: it never reads advisories.
    """
    import tempfile

    bundle = ctx.companion()
    sha = bundle.get("image_artifact_sha")
    reference = bundle.get("reference")
    if sha is None or reference is None:
        return
    report_data = bundle.get("report")
    if not isinstance(report_data, Mapping):
        raise CriticError("image validation report missing before critique")
    deterministic_checks = report_data.get("results")

    try:
        client = ctx.model("critic")
    except Exception as exc:
        receipt = failure_receipt_for(sha, deterministic_checks, exc, dispatched=False)
        _record_vision_receipt(ctx, bundle, receipt)
        ctx.event(
            "image_critique",
            receipt["status"],
            {"reason": type(exc).__name__, "receipt_sha256": receipt["receipt_sha256"]},
        )
        if type(exc).__name__ in {
            "BudgetExceeded",
            "PermissionDenied",
            "ModelPolicyError",
        }:
            raise
        raise CriticError(
            f"vision adapter unavailable ({type(exc).__name__})"
        ) from None

    payload = ctx.artifact_payload(sha)
    summary = summarize_for_prompt(ImageStats.from_dict(reference))
    with tempfile.NamedTemporaryFile(
        suffix=".png", prefix="ruloom-synthetic-", delete=False
    ) as handle:
        handle.write(payload)
        synthetic_path = Path(handle.name).resolve()
    try:
        try:
            receipt = critique_synthetic(
                client,
                synthetic_path,
                summary,
                artifact_sha256=sha,
                deterministic_checks=deterministic_checks,
            )
        except Exception as exc:
            failure = failure_receipt_for(
                sha, deterministic_checks, exc, dispatched=True
            )
            _record_vision_receipt(ctx, bundle, failure)
            ctx.event(
                "image_critique",
                failure["status"],
                {
                    "reason": type(exc).__name__,
                    "receipt_sha256": failure["receipt_sha256"],
                },
            )
            if type(exc).__name__ in {
                "BudgetExceeded",
                "PermissionDenied",
                "ModelPolicyError",
            }:
                raise
            raise CriticError(
                f"vision advisory failed ({type(exc).__name__})", dispatched=True
            ) from None
        _record_vision_receipt(ctx, bundle, receipt)
        ctx.say("[image_critique] advisory vision receipt recorded")
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
    rep_dict = report.to_dict()
    case_utility = getattr(report, "case_utility", None)
    if case_utility is not None:
        rep_dict["case_utility"] = case_utility
        case_results = case_check_results(case_utility)
        existing_names = {r.get("name") for r in rep_dict.get("results", [])}
        for cr in case_results:
            if cr.get("check_id") not in existing_names:
                rep_dict["results"].append(
                    {
                        "name": cr["check_id"],
                        "status": cr["status"],
                        "detail": str(cr.get("detail", "")),
                        "band_position": cr.get("band_position"),
                    }
                )
    (artifact_dir / "validation-report.json").write_text(
        json.dumps(rep_dict, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "seed.txt").write_text(f"{plan['seed']}\n", encoding="utf-8")
    (artifact_dir / "provenance.md").write_text(ctx.provenance_text(), encoding="utf-8")
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
                    "scene": bundle.get("scene"),
                    "schematic_only": True,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if bundle.get("report") is not None:
            (artifact_dir / "image-validation.json").write_text(
                json.dumps(bundle["report"], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        vision_note = bundle.get("vision_advisory") or ctx.advisory("vision_note")
        if vision_note is None:
            vision_note = build_failure_receipt(
                bundle["image_artifact_sha"],
                bundle["report"]["results"],
                status=STATUS_UNAVAILABLE,
                evidence_kind=EVIDENCE_NOT_DISPATCHED,
                note="Vision advisory unavailable: no review receipt was recorded.",
            )
        validate_receipt(vision_note)
        (artifact_dir / "vision-advisory.json").write_text(
            json.dumps(vision_note, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    note = ctx.advisory("local_critique_note")
    if note is not None:
        (artifact_dir / "critic-note.json").write_text(
            json.dumps(note, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    req = ctx.requirements()
    if req is not None:
        case_utility = getattr(report, "case_utility", None)
        if case_utility is not None and case_utility.get("passed") is True:
            case_members = build_case_members(
                payload, req, case_utility, ctx.spec_digest()
            )
            for fname, fbytes in case_members.items():
                (artifact_dir / fname).write_bytes(fbytes)
    ctx.event(
        "artifact",
        "ok",
        {"artifact_dir": str(artifact_dir)},
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


def prepare_case_context(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Prepare case context, ensuring source_fit_sha256 binding is present."""
    ctx = _workflow_prepare_case_context(*args, **kwargs)
    if isinstance(ctx, dict) and "binding" in ctx:
        binding = ctx["binding"]
        if isinstance(binding, dict) and "source_fit_sha256" not in binding:
            fit = ctx.get("source_fit_receipt")
            if fit is None:
                fit = ctx.get("source_fit")
            if isinstance(fit, Mapping):
                from .validation.workflow import _canonical_bytes

                binding["source_fit_sha256"] = hashlib.sha256(
                    _canonical_bytes(fit)
                ).hexdigest()
    return ctx


@dataclass(frozen=True)
class _CasePreflight:
    """What the preflight judged. Opaque to the engine and never serialised."""

    source_path: Path
    source_sha256: str
    source_config: dict[str, Any]
    sidecar_path: Path | None
    sidecar_sha256: str | None
    case_def: dict[str, Any]
    reference: dict[str, Any]


def _source_licence(
    sidecar: Path | None, source_config: Mapping[str, Any]
) -> str | None:
    """The licence string intake would use: the sibling record wins over config."""
    if sidecar is not None:
        licence = json.loads(sidecar.read_text(encoding="utf-8")).get("licence")
    else:
        licence = source_config.get("licence")
    return licence if isinstance(licence, str) else None


def prepare_case_preflight(
    source_path: Path | str,
    case: Mapping[str, Any],
    source_config: Mapping[str, Any] | None = None,
) -> tuple[_CasePreflight, dict[str, Any]]:
    """Judge a source for a selected case BEFORE any request model is called.

    Reuses the accepted catalogue and source checks (the same sequence
    ``prepare_case_context`` runs at run intake, which stays an independent
    recheck): the case and version must match the installed catalogue, the
    registry record must match the bytes, the source must be suitable, and the
    bounded reference must build from a single consistent read. Nothing from
    the source is returned beyond its hash: no rows, no label vocabulary and no
    free-form licence text. Raises ``ValueError`` to refuse.
    """
    if not isinstance(case, Mapping):
        raise ValueError("the selected case must be an {id, version} mapping")
    case_id, version = case.get("id"), case.get("version")
    config = dict(source_config) if isinstance(source_config, Mapping) else {}
    load_cases_catalogue()
    try:
        case_def = get_case(str(case_id))
    except KeyError as err:
        raise ValueError(f"unknown case {case_id!r}: {err}") from err
    if (
        case_id != case_def["id"]
        or type(version) is not int
        or case_def.get("version") != version
    ):
        raise ValueError(
            f"case {case_id!r} version {version!r} does not match the installed "
            f"catalogue ({case_def['id']!r} version {case_def.get('version')!r})"
        )
    source_p = Path(source_path)
    try:
        measured = hashlib.sha256(source_p.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"cannot read source {source_p}: {exc}") from exc
    sidecar = _sibling_manifest(source_p, config)
    sidecar_sha: str | None = None
    if sidecar is not None:
        try:
            _verify_sibling_manifest(sidecar, measured)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        sidecar_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    licence = _source_licence(sidecar, config)
    suitability = assess_source_suitability(
        source_p, case_def, licence_meta=licence, expected_source_sha256=measured
    )
    if suitability.get("suitable") is not True:
        raise ValueError(
            f"source is not suitable for case {case_id!r}: "
            f"{suitability.get('reasons', [])}"
        )
    reference = build_source_reference(
        source_p, case_def=case_def, licence_meta=licence
    )
    if reference.get("source_sha256") != measured:
        raise ValueError(
            "the source changed while its reference was being built: reference "
            f"hash {reference.get('source_sha256')} differs from measured {measured}"
        )
    validate_source_reference(reference, case_def)
    handle = _CasePreflight(
        source_path=source_p,
        source_sha256=measured,
        source_config=config,
        sidecar_path=sidecar,
        sidecar_sha256=sidecar_sha,
        case_def=case_def,
        reference=reference,
    )
    evidence = {
        "case_id": case_def["id"],
        "case_version": case_def["version"],
        "source_sha256": measured,
        "source_fit": "suitable",
    }
    return handle, evidence


def check_case_target_rows(handle: _CasePreflight, n_rows: int) -> dict[str, Any]:
    """Refuse an infeasible interpreted row count; never change it.

    Reuses the accepted feasibility checks against the reference the preflight
    built, so this needs no second read of the source. Run intake repeats the
    check independently.
    """
    if not isinstance(handle, _CasePreflight):
        raise TypeError(f"not a case preflight handle: {type(handle).__name__}")
    if type(n_rows) is not int or isinstance(n_rows, bool) or n_rows < 1:
        raise ValueError(f"the row count must be a positive integer, got {n_rows!r}")
    validate_plan_satisfiability(n_rows, handle.case_def, handle.reference)
    check_source_joint_satisfiability(n_rows, handle.case_def, handle.reference)
    return {"case_id": handle.case_def["id"]}


def confirm_case_source_unchanged(handle: _CasePreflight) -> None:
    """Refuse unless the source and its registry record are still as judged."""
    if not isinstance(handle, _CasePreflight):
        raise TypeError(f"not a case preflight handle: {type(handle).__name__}")
    try:
        now = hashlib.sha256(handle.source_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(
            f"source changed during preparation: cannot re-read it: {exc}"
        ) from exc
    if now != handle.source_sha256:
        raise ValueError(
            f"source changed during preparation: judged {handle.source_sha256}, "
            f"now {now}"
        )
    sidecar = _sibling_manifest(handle.source_path, handle.source_config)
    if (sidecar is None) != (handle.sidecar_path is None):
        raise ValueError(
            "source registry record appeared or vanished during preparation"
        )
    if sidecar is not None:
        sidecar_now = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        if sidecar_now != handle.sidecar_sha256:
            raise ValueError("source registry record changed during preparation")


#: Checks the generic request step sequences for a selected case: source fit
#: before the model, row feasibility after interpretation, and a changed-source
#: refusal. The engine names them but never learns what they mean.
CASE_PREPARATION_HOOKS: dict[str, Any] = {
    "preflight": prepare_case_preflight,
    "check_target": check_case_target_rows,
    "confirm_unchanged": confirm_case_source_unchanged,
}


#: Capability implementations keyed by manifest capability id.
CAPABILITIES: dict[str, Any] = {
    "intake": intake,
    "case_source_assess": case_source_assess,
    "profile_source": profile_source,
    "case_research": case_research,
    "plan": plan,
    "generate": generate,
    "validate": validate,
    "repair": repair,
    "case_repair": case_repair,
    "case_candidate_review": case_candidate_review,
    "image_intake": image_intake,
    "image_synthesize": image_synthesize,
    "image_validate": image_validate,
    "image_critique": image_critique,
    "local_critique": local_critique,
    "package_artifact": package_artifact,
    "package_release": package_release,
}

__all__ = ["CAPABILITIES", "CASE_PREPARATION_HOOKS", "verify_case_members"]
