"""Held-out evaluation harness: product pipeline arm vs direct-LLM baseline arm.

Protocol (mirrored in docs/results.md, which states it before any numbers):

- Held-out holes are the four non-reference sources under ``data/sources``
  (``20LCDD003``, ``DDG007``, ``HD012``, ``NCB0003``). ``PDD446`` is the
  development reference and is excluded. Calibration bands were fitted on
  the ``data/calibration`` holes, never on these.
- Pipeline arm: run the real product CLI
  (``.venv/bin/ruloom run <hole> --max-attempts N``, stdin ``accept``) and
  record verdict, per-group check outcomes, attempts used, artifact sha256,
  model usage tokens, and wall time from the run directory.
- Baseline arm (direct-LLM adversary, same product model via
  ``models/meta_cli.py``): give the model the SAME statistics summary the
  planner sees plus an instruction to emit CSV rows directly; request
  exactly ``--baseline-rows`` rows in one structured call ``{csv: string}``;
  parse the rows; score them with the SAME check code
  (``profiles/hylogger/validation/checks.py``) against the same thresholds;
  record parse failures honestly as rows requested vs rows parsed.
- Asymmetry: the pipeline generates the plan's full ``n_rows`` while the
  baseline is capped at the requested emitted rows for output limits, so
  distributional metrics are compared and exact-count metrics are reported
  per arm, never pooled.
- Reproducibility: pipeline = regenerate-from-plan hash match, measured per
  hole by rebuilding the profile from the source CSV and re-running the
  seeded generator; baseline = no seed exists, recorded as not reproducible
  by construction (the baseline is never run twice to fake a comparison).

Grouping of check outcomes follows the manifest's own ``kind`` mapping
(constraint vs fidelity). Leakage has no dedicated check in the product;
the harness operationalises it as the vocabulary pair
(``swir_vocabulary`` + ``tir_vocabulary``): synthetic labels stay inside
the calibrated source vocabulary. That definition is stated here and in
``docs/results.md`` so the Leakage row stays a measured value, not prose.

Outputs: ``<out>/<hole>.json`` per hole plus ``<out>/summary.json``.
Standard library plus project imports only. No network except the model
calls the protocol requires.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from engine.artifacts.hashing import sha256_bytes  # noqa: E402
from models.base import StructuredModel  # noqa: E402
from models.meta_cli import MuseCLIModel  # noqa: E402
from profiles.hylogger.analysis.calibration import load_thresholds  # noqa: E402
from profiles.hylogger.analysis.statistics import Profile  # noqa: E402
from profiles.hylogger.generation.generate import generate as generate_artifact  # noqa: E402
from profiles.hylogger.generation.plan import build_prompt  # noqa: E402
from profiles.hylogger.intake.reader import Sample, read_scalar_csv  # noqa: E402
from profiles.hylogger.validation.checks import (  # noqa: E402
    CheckReport,
    CheckResult,
    run_checks,
)
from profiles.hylogger.validation.dataset import (  # noqa: E402
    HEADER,
    read_csv_bytes,
    to_csv_bytes,
)

#: Held-out hole ids (stems of data/sources/<id>-scalars.csv). PDD446 is
#: the development reference and is never a held-out hole.
HELD_OUT_HOLES = ("20LCDD003", "DDG007", "HD012", "NCB0003")

#: Checks operationalised as the leakage group: synthetic labels stay
#: inside the calibrated source vocabulary. Also counted in constraints
#: (their manifest kind); the Leakage row reports this pair on its own.
LEAKAGE_CHECKS = ("swir_vocabulary", "tir_vocabulary")

#: Structured-output schema for the baseline adversary call.
BASELINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"csv": {"type": "string"}},
    "required": ["csv"],
    "additionalProperties": False,
}


def usage_dict_tokens(usage: Any) -> int:
    """Count budget tokens from a Usage object or its stored-dict form.

    The engine's own counter reads live :class:`Usage` objects; run-log
    JSON carries plain dicts, which attribute lookup would read as zero,
    so this helper accepts both. Non-integer fields count as zero.
    """
    total = 0
    for field in ("input_tokens", "output_tokens", "reasoning_tokens"):
        if isinstance(usage, Mapping):
            value = usage.get(field, 0)
        else:
            value = getattr(usage, field, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        total += value
    return total


def load_check_kinds(manifest_path: Path | None = None) -> dict[str, str]:
    """Map check id to its manifest ``kind`` (constraint or fidelity)."""
    path = manifest_path or (REPO_ROOT / "profiles" / "hylogger" / "profile.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {
        entry["id"]: entry.get("kind", "constraint")
        for entry in manifest.get("checks", [])
        if isinstance(entry, dict) and "id" in entry
    }


def group_results(
    results: Sequence[Mapping[str, Any] | CheckResult],
    kinds: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate check outcomes into constraint/fidelity/leakage groups.

    A group passes when none of its checks fails (skips permitted, matching
    the product verdict semantics). Accepts :class:`CheckResult` objects or
    plain ``{"name": ..., "status": ...}`` mappings (e.g. from stored JSON).
    """

    def name_of(result: Any) -> str:
        return result.name if isinstance(result, CheckResult) else str(result["name"])

    def status_of(result: Any) -> str:
        return (
            result.status if isinstance(result, CheckResult) else str(result["status"])
        )

    kinds = dict(kinds or {})
    groups: dict[str, list[str]] = {"constraints": [], "fidelity": [], "leakage": []}
    for result in results:
        check = name_of(result)
        kind = kinds.get(check, "constraint")
        if kind == "fidelity":
            groups["fidelity"].append(check)
        else:
            groups["constraints"].append(check)
        if check in LEAKAGE_CHECKS:
            groups["leakage"].append(check)
    outcome: dict[str, dict[str, Any]] = {}
    for group, checks in groups.items():
        failed = [
            name_of(result)
            for result in results
            if name_of(result) in checks and status_of(result) == "fail"
        ]
        skipped = [
            name_of(result)
            for result in results
            if name_of(result) in checks and status_of(result) == "skip"
        ]
        outcome[group] = {
            "pass": not failed,
            "n_checks": len(checks),
            "n_failed": len(failed),
            "failed": failed,
            "n_skipped": len(skipped),
            "skipped": skipped,
        }
    return outcome


def parse_baseline_csv(
    text: str, requested_rows: int
) -> tuple[list[Sample] | None, int, str | None]:
    """Parse baseline-emitted CSV text into samples.

    Returns ``(samples_or_none, parsed_rows, error_or_none)``. A header or
    cell failure yields ``(None, 0, message)``; trailing truncated rows are
    not salvaged -- requested vs parsed is recorded honestly.
    """
    try:
        samples = read_csv_bytes(text)
    except ValueError as exc:
        return None, 0, f"{type(exc).__name__}: {exc}"
    return samples, len(samples), None


def score_samples(
    samples: Sequence[Sample],
    thresholds: Any,
    source_samples: Sequence[Sample],
    *,
    artifact_name: str = "",
    artifact_sha256: str = "",
) -> CheckReport:
    """Run the product checks over samples (mirrors the validate step)."""
    sha = artifact_sha256 or sha256_bytes(to_csv_bytes(list(samples)))
    report = run_checks(
        list(samples),
        thresholds,
        source_samples=list(source_samples),
        artifact_name=artifact_name,
        artifact_sha256=sha,
    )
    report.results.insert(
        0, CheckResult("csv_schema", "pass", f"{len(samples)} rows, header ok")
    )
    return report


def run_baseline(
    model: StructuredModel,
    profile: Profile,
    source_samples: Sequence[Sample],
    thresholds: Any,
    n_rows: int,
) -> dict[str, Any]:
    """Run the direct-LLM adversary: one structured call emitting CSV text."""
    prompt = (
        build_prompt(profile)
        + f"\nNow emit a synthetic hole directly as CSV text with exactly "
        f"{n_rows} data rows under the header "
        f"{','.join(HEADER)} (valid is true/false, missing labels are empty "
        f"cells). Reply with a JSON object with exactly one key \"csv\" "
        f"holding the full CSV text, header included."
    )
    started = time.monotonic()
    try:
        completion = model.complete(prompt, BASELINE_SCHEMA)
        wall_s = round(time.monotonic() - started, 1)
    except Exception as exc:  # noqa: BLE001 -- recorded, not raised
        return {
            "requested_rows": n_rows,
            "parsed_rows": 0,
            "parse_error": f"{type(exc).__name__}: {exc}",
            "model": getattr(model, "model", "?"),
            "usage_tokens": 0,
            "wall_s": round(time.monotonic() - started, 1),
            "verdict": "error",
            "report": None,
            "reproducible": False,
            "reproducibility_note": "no seed exists; not reproducible by construction",
        }
    csv_text = completion.data.get("csv", "")
    if not isinstance(csv_text, str):
        csv_text = str(csv_text)
    samples, parsed, error = parse_baseline_csv(csv_text, n_rows)
    usage = usage_dict_tokens(completion.usage)
    record: dict[str, Any] = {
        "requested_rows": n_rows,
        "parsed_rows": parsed,
        "parse_error": error,
        "model": completion.usage.model
        if hasattr(completion.usage, "model")
        else getattr(model, "model", "?"),
        "usage_tokens": usage,
        "wall_s": wall_s,
        "usage": {
            "input_tokens": getattr(completion.usage, "input_tokens", 0),
            "output_tokens": getattr(completion.usage, "output_tokens", 0),
            "reasoning_tokens": getattr(completion.usage, "reasoning_tokens", 0),
            "calls": getattr(completion.usage, "calls", 0),
        },
        "reproducible": False,
        "reproducibility_note": "no seed exists; not reproducible by construction",
    }
    if samples is None:
        record["verdict"] = "error"
        record["report"] = None
        return record
    report = score_samples(
        samples,
        thresholds,
        source_samples,
        artifact_name="baseline",
        artifact_sha256=sha256_bytes(csv_text.encode("utf-8")),
    )
    record["verdict"] = report.verdict
    record["report"] = report.to_dict()
    return record


def read_run_events(run_dir: Path) -> list[dict[str, Any]]:
    """Read the hash-chained run log of one product run directory."""
    events = []
    with (run_dir / "run.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def find_run_dir(out_dir: Path) -> Path:
    """Locate the single product run directory under ``<out>/runs/``."""
    candidates = sorted(
        (out_dir / "runs").glob("*/*/run.jsonl"), key=lambda p: p.stat().st_mtime
    )
    if not candidates:
        candidates = sorted(
            (out_dir / "runs").glob("*/run.jsonl"), key=lambda p: p.stat().st_mtime
        )
    if not candidates:
        raise FileNotFoundError(f"no run.jsonl under {out_dir / 'runs'}")
    return candidates[-1].parent


def run_pipeline(
    hole: str,
    source: Path,
    max_attempts: int,
    out_dir: Path,
    ruloom: str = ".venv/bin/ruloom",
) -> dict[str, Any]:
    """Run the real product CLI for one hole and record measured evidence."""
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        ruloom,
        "run",
        str(source),
        "--max-attempts",
        str(max_attempts),
        "--out",
        str(out_dir),
    ]
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        input="accept\nStep 14 held-out evaluation accept\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=3600,
    )
    wall_s = time.monotonic() - started
    record: dict[str, Any] = {
        "exit_code": proc.returncode,
        "wall_s": round(wall_s, 1),
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }
    try:
        run_dir = find_run_dir(out_dir)
    except FileNotFoundError as exc:
        record["error"] = str(exc)
        return record
    record["run_dir"] = str(run_dir.relative_to(REPO_ROOT))
    events = read_run_events(run_dir)
    attempts = [
        e for e in events if e.get("stage") == "generate_validate_repair"
    ]
    record["attempts_used"] = len(attempts)
    record["attempt_verdicts"] = [
        (e.get("details") or {}).get("verdict") for e in attempts
    ]
    call_usages = [
        e.get("usage")
        for e in events
        if e.get("stage") in ("plan", "repair") and e.get("usage")
    ]
    record["usage_tokens"] = sum(usage_dict_tokens(u) for u in call_usages)
    record["usage_calls"] = len(call_usages)
    record["usage_models"] = sorted(
        {u.get("model") for u in call_usages if isinstance(u, dict) and u.get("model")}
    )
    validation_path = run_dir / "validation-report.json"
    if validation_path.is_file():
        record["report"] = json.loads(validation_path.read_text(encoding="utf-8"))
        record["verdict"] = record["report"].get("verdict")
        record["artifact_sha256"] = record["report"].get("artifact_sha256")
    plan_path = run_dir / "plan.json"
    if plan_path.is_file():
        record["plan"] = json.loads(plan_path.read_text(encoding="utf-8"))
    data_path = run_dir / "data.csv"
    if data_path.is_file():
        record["artifact_sha256"] = sha256_bytes(data_path.read_bytes())
        record["artifact_rows"] = sum(1 for _ in data_path.open()) - 1
    record["reproducible"] = check_reproducibility(
        source, run_dir, record.get("plan")
    )
    return record


def check_reproducibility(
    source: Path, run_dir: Path, plan: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Regenerate from the recorded plan and compare artifact hashes."""
    data_path = run_dir / "data.csv"
    if plan is None or not data_path.is_file():
        return {"match": False, "note": "plan or artifact missing"}
    try:
        source_bytes = source.read_bytes()
        source_samples = read_scalar_csv(source)
        profile = Profile.from_samples(
            source_samples, source.stem, sha256_bytes(source_bytes)
        )
        regenerated = generate_artifact(profile, dict(plan))
    except Exception as exc:  # noqa: BLE001 -- recorded, not raised
        return {"match": False, "note": f"regeneration failed: {exc}"}
    expected = sha256_bytes(data_path.read_bytes())
    actual = sha256_bytes(regenerated)
    return {
        "match": expected == actual,
        "expected_sha256": expected,
        "regenerated_sha256": actual,
    }


def evaluate_hole(
    hole: str,
    *,
    max_attempts: int,
    baseline_rows: int,
    out_dir: Path,
    model: StructuredModel | None = None,
    run_pipeline_arm: bool = True,
) -> dict[str, Any]:
    """Evaluate one held-out hole with both arms; write the per-hole JSON."""
    source = REPO_ROOT / "data" / "sources" / f"{hole}-scalars.csv"
    source_samples = read_scalar_csv(source)
    thresholds = load_thresholds(
        REPO_ROOT / "profiles" / "hylogger" / "thresholds.json"
    )
    profile = Profile.from_samples(
        source_samples, source.stem, sha256_bytes(source.read_bytes())
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    hole_out = out_dir / hole
    record: dict[str, Any] = {
        "hole": hole,
        "source": str(source.relative_to(REPO_ROOT)),
        "source_rows": len(source_samples),
        "source_sha256": sha256_bytes(source.read_bytes()),
        "max_attempts": max_attempts,
        "baseline_rows": baseline_rows,
    }
    if run_pipeline_arm:
        record["pipeline"] = run_pipeline(hole, source, max_attempts, hole_out)
    else:
        record["pipeline"] = {"skipped": True}
    active_model = model if model is not None else MuseCLIModel()
    baseline = run_baseline(active_model, profile, source_samples, thresholds,
                            baseline_rows)
    record["baseline"] = baseline
    kinds = load_check_kinds()
    for arm in ("pipeline", "baseline"):
        report = (record.get(arm) or {}).get("report")
        if isinstance(report, dict):
            record[arm]["groups"] = group_results(report.get("results", []), kinds)
    with (out_dir / f"{hole}.json").open("w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
    return record


def summarize(hole_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate per-hole records into the summary JSON."""
    summary: dict[str, Any] = {"holes": [], "arms": {}}
    for arm in ("pipeline", "baseline"):
        agg: dict[str, Any] = {
            "holes": 0,
            "verdict_pass": 0,
            "constraints_pass": 0,
            "fidelity_pass": 0,
            "leakage_pass": 0,
            "reproducible": 0,
            "attempts_total": 0,
            "usage_tokens_total": 0,
            "wall_s_total": 0.0,
        }
        for record in hole_records:
            arm_rec = record.get(arm) or {}
            if arm_rec.get("skipped") or arm_rec.get("error"):
                continue
            agg["holes"] += 1
            if arm_rec.get("verdict") == "pass":
                agg["verdict_pass"] += 1
            groups = arm_rec.get("groups") or {}
            for key in ("constraints", "fidelity", "leakage"):
                if groups.get(key, {}).get("pass"):
                    agg[f"{key}_pass"] += 1
            repro = arm_rec.get("reproducible")
            if isinstance(repro, dict):
                if repro.get("match"):
                    agg["reproducible"] += 1
            elif repro is True:
                agg["reproducible"] += 1
            agg["attempts_total"] += int(arm_rec.get("attempts_used") or 0)
            agg["usage_tokens_total"] += int(arm_rec.get("usage_tokens") or 0)
            agg["wall_s_total"] += float(arm_rec.get("wall_s") or 0.0)
        agg["wall_s_total"] = round(agg["wall_s_total"], 1)
        summary["arms"][arm] = agg
    summary["holes"] = [r.get("hole") for r in hole_records]
    return summary


def render_results_table(summary: Mapping[str, Any]) -> str:
    """Render the aggregate table (07 section 11 shape) as Markdown."""
    arms = summary.get("arms", {})
    rows = [
        ("Constraints pass", "constraints_pass"),
        ("Fidelity pass", "fidelity_pass"),
        ("Leakage pass", "leakage_pass"),
        ("Reproducible", "reproducible"),
        ("Held-out holes pass", "verdict_pass"),
        ("Attempts used (total)", "attempts_total"),
        ("Model tokens (total)", "usage_tokens_total"),
        ("Wall time s (total)", "wall_s_total"),
    ]
    holes = summary.get("holes", [])
    header = "| Measure | " + " | ".join(
        f"{arm} (n={arms.get(arm, {}).get('holes', 0)})" for arm in ("pipeline", "baseline")
    ) + " |"
    lines = [
        f"Held-out holes: {', '.join(holes)}.",
        "",
        header,
        "| --- | " + " | ".join(["---"] * 2) + " |",
    ]
    for label, key in rows:
        lines.append(
            "| " + label + " | "
            + " | ".join(str(arms.get(arm, {}).get(key, "?")) for arm in ("pipeline", "baseline"))
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the held-out evaluation and write per-hole + summary JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holes", nargs="*", default=list(HELD_OUT_HOLES))
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--baseline-rows", type=int, default=1000)
    parser.add_argument("--out", default=None)
    parser.add_argument("--skip-pipeline", action="store_true")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.out:
        out_dir = Path(args.out)
        if not out_dir.is_absolute():
            out_dir = REPO_ROOT / out_dir
    else:
        out_dir = REPO_ROOT / "runs" / "evaluation" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for hole in args.holes:
        record = evaluate_hole(
            hole,
            max_attempts=args.max_attempts,
            baseline_rows=args.baseline_rows,
            out_dir=out_dir,
            run_pipeline_arm=not args.skip_pipeline,
        )
        records.append(record)
    summary = summarize(records)
    summary["out"] = str(out_dir.relative_to(REPO_ROOT))
    summary["holes_detail"] = args.holes
    with (out_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    print(render_results_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
