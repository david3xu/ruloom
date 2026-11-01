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

Scoring integrity (R09-harness, RP-08):

- Fresh-invocation binding: only a product run directory created by the
  harness's own CLI invocation is ever scored. Older run directories
  are counted as stale-ignored and never read for verdicts; a CLI
  failure over old or missing artifacts records the arm unscorable
  with the error, never a pass.
- Empty check groups fail coverage, and an arm that executed zero
  required checks is unscorable. Required coverage
  (executed/required per hole) is recorded on every scored arm.
- Every baseline invocation saves its raw output bytes, prompt digest,
  model identity, invocation id, usage, and error state next to the
  per-hole record. A failed invocation is unscorable and never reuses
  a previous invocation's output.
- Aggregates recompute independently: ``--recompute`` (and the end of
  every run) rebuilds headline counts purely from the saved per-hole
  artifacts and fails loudly on mismatch with the stored summary.
  Unscorable arms stay in the holes_total denominator, never in pass
  counts.

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
import uuid
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


def required_check_ids(
    manifest_path: Path | None = None, *, images_present: bool = False
) -> list[str]:
    """List manifest check ids that are required for a run of this shape.

    Checks gated by ``applicable_when: images_present`` count only when
    the run produced images. ``required: false`` checks (e.g. the
    vacuous ``swir_answer_rate`` band) never gate.
    """
    path = manifest_path or (REPO_ROOT / "profiles" / "hylogger" / "profile.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    ids = []
    for entry in manifest.get("checks", []):
        if not isinstance(entry, dict) or not entry.get("required"):
            continue
        if entry.get("applicable_when") == "images_present" and not images_present:
            continue
        ids.append(entry["id"])
    return ids


def coverage_report(
    executed: Sequence[str],
    required: Sequence[str],
) -> dict[str, Any]:
    """Describe required-check coverage for one arm.

    ``missing`` names required checks with no executed result. An arm
    with zero executed required checks is a coverage failure and must
    be recorded unscorable, never passing.
    """
    executed_required = [c for c in required if c in executed]
    missing = [c for c in required if c not in executed]
    return {
        "n_required": len(required),
        "n_executed": len(executed_required),
        "missing": missing,
        "complete": not missing,
    }


def benchmark_version(name: str) -> dict[str, Any]:
    """Identify one versioned benchmark rerun (code + spec + data pins)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        commit = (proc.stdout or "").strip() or "unknown"
    except Exception:  # noqa: BLE001 -- recorded, not raised
        commit = "unknown"
    from engine.core.spec import SPEC_VERSION  # noqa: E402

    manifests = {}
    for profile_id in ("hylogger", "tabular"):
        try:
            manifest = json.loads(
                (
                    REPO_ROOT / "profiles" / profile_id / "profile.json"
                ).read_text(encoding="utf-8")
            )
            manifests[profile_id] = manifest.get("version")
        except OSError:
            manifests[profile_id] = None
    try:
        thresholds_sha256 = sha256_bytes(
            (REPO_ROOT / "profiles" / "hylogger" / "thresholds.json").read_bytes()
        )
    except OSError:
        thresholds_sha256 = "unknown"
    return {
        "name": name,
        "code_commit": commit,
        "spec_version": SPEC_VERSION,
        "manifest_versions": manifests,
        "thresholds_sha256": thresholds_sha256,
    }


def group_results(
    results: Sequence[Mapping[str, Any] | CheckResult],
    kinds: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate check outcomes into constraint/fidelity/leakage groups.

    A group passes when it executed at least one check and none of its
    checks fails (skips permitted, matching the product verdict
    semantics). An EMPTY group fails coverage (R09-harness): zero
    executed checks can never pass. Accepts :class:`CheckResult`
    objects or plain ``{"name": ..., "status": ...}`` mappings (e.g.
    from stored JSON).
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
            "pass": not failed and bool(checks),
            "coverage": "ok" if checks else "empty",
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


def _save_baseline_invocation(
    save_dir: Path,
    *,
    invocation_id: str,
    model_name: str,
    prompt_sha256: str,
    csv_text: str | None,
    requested_rows: int,
    parsed_rows: int,
    parse_error: str | None,
    usage: Mapping[str, Any] | None,
    wall_s: float,
) -> dict[str, Any]:
    """Persist one baseline invocation's raw bytes and identity.

    ``baseline.csv`` holds exactly the bytes this invocation returned
    (absent when the call itself failed, in which case any older file
    is removed so a later reader can never mistake stale bytes for
    this invocation's). ``invocation.json`` carries the invocation id,
    model identity, prompt digest, output digest, usage, and error
    state. Returns the saved-artifact descriptor for the record.
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    csv_path = save_dir / "baseline.csv"
    if csv_text is None:
        if csv_path.is_file():
            csv_path.unlink()
        output_sha: str | None = None
        csv_saved = False
    else:
        csv_path.write_bytes(csv_text.encode("utf-8"))
        output_sha = sha256_bytes(csv_text.encode("utf-8"))
        csv_saved = True
    invocation = {
        "invocation_id": invocation_id,
        "model": model_name,
        "prompt_sha256": prompt_sha256,
        "output_sha256": output_sha,
        "csv_saved": csv_saved,
        "requested_rows": requested_rows,
        "parsed_rows": parsed_rows,
        "parse_error": parse_error,
        "usage": dict(usage or {}),
        "wall_s": wall_s,
    }
    (save_dir / "invocation.json").write_text(
        json.dumps(invocation, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {
        "dir": _relative_or_str(save_dir),
        "csv_saved": csv_saved,
    }


def run_baseline(
    model: StructuredModel,
    profile: Profile,
    source_samples: Sequence[Sample],
    thresholds: Any,
    n_rows: int,
    *,
    save_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the direct-LLM adversary: one structured call emitting CSV text.

    Every invocation carries a fresh ``invocation_id`` plus the prompt
    digest, and -- when ``save_dir`` is given -- its raw output bytes
    land on disk next to an invocation record. A failed invocation
    (refusal, timeout, unparsable bytes) is unscorable and never reuses
    a previous invocation's output.
    """
    invocation_id = uuid.uuid4().hex
    prompt = (
        build_prompt(profile)
        + f"\nNow emit a synthetic hole directly as CSV text with exactly "
        f"{n_rows} data rows under the header "
        f"{','.join(HEADER)} (valid is true/false, missing labels are empty "
        f"cells). Reply with a JSON object with exactly one key \"csv\" "
        f"holding the full CSV text, header included."
    )
    prompt_sha256 = sha256_bytes(prompt.encode("utf-8"))
    model_name = getattr(model, "model", "?")
    started = time.monotonic()
    try:
        completion = model.complete(prompt, BASELINE_SCHEMA)
        wall_s = round(time.monotonic() - started, 1)
    except Exception as exc:  # noqa: BLE001 -- recorded, not raised
        wall_s = round(time.monotonic() - started, 1)
        record: dict[str, Any] = {
            "invocation_id": invocation_id,
            "prompt_sha256": prompt_sha256,
            "requested_rows": n_rows,
            "parsed_rows": 0,
            "parse_error": f"{type(exc).__name__}: {exc}",
            "model": model_name,
            "usage_tokens": 0,
            "wall_s": wall_s,
            "verdict": "error",
            "unscorable": True,
            "reason": f"model invocation failed: {type(exc).__name__}",
            "report": None,
            "reproducible": False,
            "reproducibility_note": "no seed exists; not reproducible by construction",
        }
        if save_dir is not None:
            record["artifacts"] = _save_baseline_invocation(
                save_dir,
                invocation_id=invocation_id,
                model_name=model_name,
                prompt_sha256=prompt_sha256,
                csv_text=None,
                requested_rows=n_rows,
                parsed_rows=0,
                parse_error=record["parse_error"],
                usage=None,
                wall_s=wall_s,
            )
        return record
    csv_text = completion.data.get("csv", "")
    if not isinstance(csv_text, str):
        csv_text = str(csv_text)
    samples, parsed, error = parse_baseline_csv(csv_text, n_rows)
    usage = usage_dict_tokens(completion.usage)
    record = {
        "invocation_id": invocation_id,
        "prompt_sha256": prompt_sha256,
        "output_bytes_sha256": sha256_bytes(csv_text.encode("utf-8")),
        "requested_rows": n_rows,
        "parsed_rows": parsed,
        "parse_error": error,
        "model": completion.usage.model
        if hasattr(completion.usage, "model")
        else model_name,
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
    if save_dir is not None:
        record["artifacts"] = _save_baseline_invocation(
            save_dir,
            invocation_id=invocation_id,
            model_name=record["model"],
            prompt_sha256=prompt_sha256,
            csv_text=csv_text,
            requested_rows=n_rows,
            parsed_rows=parsed,
            parse_error=error,
            usage=record["usage"],
            wall_s=wall_s,
        )
    if samples is None:
        record["verdict"] = "error"
        record["unscorable"] = True
        record["reason"] = "baseline bytes unparsable; no score computed"
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


def _relative_or_str(path: Path) -> str:
    """Render ``path`` relative to the repo root, or absolute outside it."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _known_run_logs(out_dir: Path) -> set[Path]:
    """Every ``run.jsonl`` already on disk (evidence of older invocations)."""
    runs = out_dir / "runs"
    if not runs.is_dir():
        return set()
    return set(runs.glob("*/*/run.jsonl")) | set(runs.glob("*/run.jsonl"))


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


def _unscorable(
    record: dict[str, Any], reason: str, *, error: str | None = None
) -> dict[str, Any]:
    """Mark an arm record unscorable: never counted as pass or fail."""
    record["unscorable"] = True
    record["reason"] = reason
    if error is not None:
        record["error"] = error
    record.pop("verdict", None)
    record.pop("groups", None)
    record.pop("report", None)
    return record


def run_pipeline(
    hole: str,
    source: Path,
    max_attempts: int,
    out_dir: Path,
    ruloom: str = ".venv/bin/ruloom",
) -> dict[str, Any]:
    """Run the real product CLI for one hole and record measured evidence.

    Fresh-invocation binding (R09-harness): only a run directory
    created by THIS invocation is ever scored. Run directories that
    predate the invocation are counted as ``stale_ignored`` and never
    read for verdicts -- a CLI failure (nonzero exit, timeout) over old
    or missing artifacts records ``unscorable`` with the error instead
    of re-scoring stale bytes as a fresh result.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    invocation_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    argv = [
        ruloom,
        "run",
        str(source),
        "--max-attempts",
        str(max_attempts),
        "--out",
        str(out_dir),
    ]
    record: dict[str, Any] = {
        "invocation_id": invocation_id,
        "started_at": started_at,
        "argv": argv,
    }
    known_before = _known_run_logs(out_dir)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            input="accept\nStep 14 held-out evaluation accept\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=3600,
        )
    except subprocess.TimeoutExpired as exc:
        record["wall_s"] = round(time.monotonic() - started, 1)
        record["exit_code"] = None
        return _unscorable(
            record,
            f"invocation {invocation_id} timed out; "
            f"{len(known_before)} pre-existing run(s) ignored, never scored",
            error=f"TimeoutExpired: {exc}",
        )
    wall_s = time.monotonic() - started
    record["exit_code"] = proc.returncode
    record["wall_s"] = round(wall_s, 1)
    record["stdout_tail"] = (proc.stdout or "")[-2000:]
    record["stderr_tail"] = (proc.stderr or "")[-2000:]
    fresh = sorted(set(_known_run_logs(out_dir)) - known_before)
    record["stale_ignored"] = len(known_before)
    if not fresh:
        return _unscorable(
            record,
            f"no fresh run dir for invocation {invocation_id} "
            f"(exit {proc.returncode}); {len(known_before)} pre-existing "
            "run(s) ignored, never scored",
            error=(proc.stderr or "")[-500:] or f"exit {proc.returncode}",
        )
    run_dir = fresh[-1].parent
    record["run_dir"] = _relative_or_str(run_dir)
    record["fresh_invocation"] = True
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
    else:
        run_json = run_dir / "run.json"
        status = None
        if run_json.is_file():
            try:
                status = json.loads(run_json.read_text(encoding="utf-8")).get("status")
            except ValueError:
                status = None
        return _unscorable(
            record,
            f"fresh run {record['run_dir']} has no validation report "
            f"(run status {status!r}, exit {proc.returncode}); "
            "nothing scored",
            error=(proc.stderr or "")[-500:] or f"exit {proc.returncode}",
        )
    executed = [
        r.get("name")
        for r in record["report"].get("results", [])
        if isinstance(r, dict) and r.get("name")
    ]
    images_present = (run_dir / "image-statistics.json").is_file()
    required = required_check_ids(images_present=images_present)
    record["coverage"] = coverage_report(executed, required)
    if record["coverage"]["n_executed"] == 0:
        return _unscorable(
            record,
            f"fresh run {record['run_dir']} executed zero required checks; "
            "coverage failure, nothing scored",
        )
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
    benchmark_name: str = "benchmark-v2-post-rp07",
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
        "benchmark": benchmark_version(benchmark_name),
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
                            baseline_rows,
                            save_dir=hole_out / "baseline")
    record["baseline"] = baseline
    kinds = load_check_kinds()
    required = required_check_ids()
    for arm in ("pipeline", "baseline"):
        arm_rec = record.get(arm) or {}
        if arm_rec.get("skipped") or arm_rec.get("unscorable"):
            continue
        report = arm_rec.get("report")
        if isinstance(report, dict):
            arm_rec["groups"] = group_results(report.get("results", []), kinds)
            if "coverage" not in arm_rec:
                executed = [
                    r.get("name")
                    for r in report.get("results", [])
                    if isinstance(r, dict) and r.get("name")
                ]
                arm_rec["coverage"] = coverage_report(executed, required)
            if arm_rec["coverage"]["n_executed"] == 0:
                _unscorable(
                    arm_rec,
                    f"{arm} arm executed zero required checks; "
                    "coverage failure, nothing scored",
                )
    with (out_dir / f"{hole}.json").open("w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
    return record


#: Headline aggregate keys covered by the independent recompute check.
HEADLINE_KEYS = (
    "holes",
    "holes_total",
    "unscorable",
    "verdict_pass",
    "constraints_pass",
    "fidelity_pass",
    "leakage_pass",
    "reproducible",
    "attempts_total",
    "usage_tokens_total",
    "wall_s_total",
)


def summarize(hole_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate per-hole records into the summary JSON.

    Unscorable arms (invocation failure, coverage failure) never add to
    pass counts but stay in the ``holes_total`` denominator with an
    ``unscorable`` count, so a partial run cannot look complete.
    Deliberately skipped arms (``--skip-pipeline``) stay out of the
    denominator entirely.
    """
    summary: dict[str, Any] = {"holes": [], "arms": {}}
    for arm in ("pipeline", "baseline"):
        agg: dict[str, Any] = {
            "holes": 0,
            "holes_total": 0,
            "unscorable": 0,
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
            if arm_rec.get("skipped"):
                continue
            agg["holes_total"] += 1
            if arm_rec.get("unscorable") or arm_rec.get("error"):
                agg["unscorable"] += 1
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


def _load_hole_records(out_dir: Path) -> list[dict[str, Any]]:
    """Read saved per-hole artifacts from disk (recompute path)."""
    records = []
    for path in sorted(out_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(record, dict) and "hole" in record:
            records.append(record)
    return records


def recompute_summary(out_dir: Path) -> dict[str, Any]:
    """Rebuild headline counts purely from saved per-hole artifacts.

    This is the independent recompute path: it reads the ``<hole>.json``
    files from disk and re-derives every headline key with its own
    aggregation, so a stored ``summary.json`` that disagrees with its
    per-hole evidence is caught instead of trusted.
    """
    hole_records = _load_hole_records(out_dir)
    recomputed: dict[str, Any] = {"holes": [], "arms": {}}
    for arm in ("pipeline", "baseline"):
        agg: dict[str, Any] = {key: 0 for key in HEADLINE_KEYS}
        agg["wall_s_total"] = 0.0
        holes_seen = 0
        for record in hole_records:
            arm_rec = record.get(arm) or {}
            if not isinstance(arm_rec, dict) or arm_rec.get("skipped"):
                continue
            holes_seen += 1
            if arm_rec.get("unscorable") or arm_rec.get("error"):
                agg["unscorable"] += 1
                continue
            agg["holes"] += 1
            if arm_rec.get("verdict") == "pass":
                agg["verdict_pass"] += 1
            groups = arm_rec.get("groups")
            if isinstance(groups, dict):
                for key in ("constraints", "fidelity", "leakage"):
                    cell = groups.get(key)
                    if isinstance(cell, dict) and cell.get("pass") is True:
                        agg[f"{key}_pass"] += 1
            repro = arm_rec.get("reproducible")
            if isinstance(repro, dict):
                matched = repro.get("match") is True
            else:
                matched = repro is True
            if matched:
                agg["reproducible"] += 1
            attempts = arm_rec.get("attempts_used") or 0
            tokens = arm_rec.get("usage_tokens") or 0
            wall = arm_rec.get("wall_s") or 0.0
            agg["attempts_total"] += int(attempts)
            agg["usage_tokens_total"] += int(tokens)
            agg["wall_s_total"] += float(wall)
        agg["holes_total"] = holes_seen
        agg["wall_s_total"] = round(agg["wall_s_total"], 1)
        recomputed["arms"][arm] = agg
    recomputed["holes"] = [r.get("hole") for r in hole_records]
    return recomputed


def summary_mismatch(out_dir: Path) -> bool:
    """True when the stored summary disagrees with its per-hole evidence."""
    summary_path = out_dir / "summary.json"
    try:
        stored = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    recomputed = recompute_summary(out_dir)
    if stored.get("holes") != recomputed.get("holes"):
        return True
    for arm in ("pipeline", "baseline"):
        stored_arm = (stored.get("arms") or {}).get(arm) or {}
        fresh_arm = recomputed["arms"][arm]
        for key in HEADLINE_KEYS:
            if stored_arm.get(key) != fresh_arm.get(key):
                return True
    return False


def render_results_table(summary: Mapping[str, Any]) -> str:
    """Render the aggregate table (07 section 11 shape) as Markdown."""
    arms = summary.get("arms", {})
    rows = [
        ("Constraints pass", "constraints_pass"),
        ("Fidelity pass", "fidelity_pass"),
        ("Leakage pass", "leakage_pass"),
        ("Reproducible", "reproducible"),
        ("Held-out holes pass", "verdict_pass"),
        ("Unscorable holes", "unscorable"),
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
    parser.add_argument("--benchmark-name", default="benchmark-v2-post-rp07")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="verify a stored out dir: recompute headlines from per-hole "
        "artifacts and fail loudly on mismatch instead of running",
    )
    args = parser.parse_args(argv)
    if args.out:
        out_dir = Path(args.out)
        if not out_dir.is_absolute():
            out_dir = REPO_ROOT / out_dir
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = REPO_ROOT / "runs" / "evaluation" / stamp
    if args.recompute:
        if summary_mismatch(out_dir):
            print(f"RECOMPUTE MISMATCH in {out_dir}: stored summary "
                  "disagrees with per-hole artifacts")
            return 1
        print(f"recompute OK: stored summary matches per-hole artifacts "
              f"in {out_dir}")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for hole in args.holes:
        record = evaluate_hole(
            hole,
            max_attempts=args.max_attempts,
            baseline_rows=args.baseline_rows,
            out_dir=out_dir,
            run_pipeline_arm=not args.skip_pipeline,
            benchmark_name=args.benchmark_name,
        )
        records.append(record)
    summary = summarize(records)
    summary["benchmark"] = benchmark_version(args.benchmark_name)
    summary["out"] = _relative_or_str(out_dir)
    summary["holes_detail"] = args.holes
    with (out_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    print(render_results_table(summary))
    if summary_mismatch(out_dir):
        print("RECOMPUTE MISMATCH: stored summary disagrees with per-hole "
              "artifacts (this is a harness bug, not a result)")
        return 1
    print("recompute OK: stored summary matches per-hole artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
