"""Evidence-report tests: every number traces to a stored record.

The report renderer reads one evidence bundle assembled from stored
records and recomputes nothing: a fabricated bundle with distinctive
values must render those values verbatim, and a released run must
ship a report whose digests match the run directory files. A failing
run still writes its report with the failing checks listed and no
decision. All model contact is faked; no test here touches the
network.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.artifacts.consumer import verify_release
from engine.core.manifest import load_manifest
from engine.core.report import render_evidence_report
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute
from models.base import Completion, Usage
from profiles.tabular import capabilities as tab_capabilities

REPO_ROOT = Path(__file__).resolve().parents[2]
TABULAR_MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")

COLUMNS = ["id", "value", "label"]
ROWS = [
    {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
    for i in range(20)
]


def _evidence() -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "profile_id": "tabular",
        "profile_version": 1,
        "spec": {
            "spec_version": 1,
            "spec_id": "tabular-default",
            "profile": "tabular",
            "source": {"path": "orders-tabular.csv", "sha256": None},
            "outputs": ["scalar"],
            "strategy": "fitted_gaussian_disclosed",
            "limits": {"max_rows": 200000, "max_attempts": 2},
            "rights": {"output_terms_intent": None},
        },
        "spec_digest": "spec-digest-7",
        "source": {
            "source_name": "orders-tabular",
            "source_sha256": "source-sha-9",
            "licence": "CC-BY-4.0",
            "attribution": "Demo fixture.",
            "hash_verified": False,
            "manifest_name": None,
        },
        "attempt_no": 2,
        "plan": {"seed": 7, "n_rows": 20},
        "check_results": [
            {
                "check_id": "mean_value",
                "kind": "fidelity",
                "status": "pass",
                "measured": "custom-measured-7",
                "threshold": "custom-threshold-9",
                "threshold_source": "relative_tolerance",
                "artifact_sha256": "artifact-sha-1",
                "verifier": "validate",
                "attempt": 2,
                "detail": "custom-detail-5",
            },
            {
                "check_id": "row_count",
                "kind": "constraint",
                "status": "fail",
                "measured": "custom-measured-3",
                "threshold": "custom-threshold-4",
                "threshold_source": "literal",
                "artifact_sha256": "artifact-sha-1",
                "verifier": "validate",
                "attempt": 2,
                "detail": "custom-detail-8",
            },
        ],
        "manifest_checks": [
            {"id": "mean_value", "kind": "fidelity", "required": True},
            {"id": "row_count", "kind": "constraint", "required": False},
        ],
        "conditions": {},
        "decision": {
            "verdict": "accept",
            "note": "human note 42",
            "decided_by": "human",
            "at": "2026-11-01T00:00:00+00:00",
        },
        "frozen_digest": "frozen-digest-1",
        "frozen_members": [
            {"path": "data.csv", "size": 11, "sha256": "data-sha-2", "attempt": 2}
        ],
        "rights": {
            "source_sha256": "source-sha-9",
            "source_hash_verified": False,
            "output_terms": None,
        },
        "images_present": False,
        "refs": {
            "run": "run.json",
            "spec": "run.jsonl",
            "attempt": "attempts/attempt-2/receipt.json",
            "checks": "attempts/attempt-2/receipt.json",
            "frozen": "frozen-manifest.json",
            "decision": "decision.json",
            "release": "manifest.json",
        },
    }


def test_report_renders_stored_values_verbatim() -> None:
    text = render_evidence_report(_evidence())
    for needle in (
        "run-1",
        "tabular-default",
        "spec-digest-7",
        "orders-tabular",
        "source-sha-9",
        "custom-measured-7",
        "custom-threshold-9",
        "custom-measured-3",
        "human note 42",
        "frozen-digest-1",
        "data-sha-2",
        "attempts/attempt-2/receipt.json",
        "fitted_gaussian_disclosed",
    ):
        assert needle in text


def test_report_marks_required_and_optional_status() -> None:
    text = render_evidence_report(_evidence())
    assert "| mean_value | yes | pass" in text
    assert "| row_count | no" in text


def test_report_without_decision_or_frozen() -> None:
    bundle = _evidence()
    bundle["decision"] = None
    bundle["frozen_digest"] = None
    bundle["frozen_members"] = []
    text = render_evidence_report(bundle)
    assert "Decision: none" in text
    assert "no frozen snapshot" in text


def test_runtime_contribution_does_not_mistake_replay_for_inference() -> None:
    bundle = _evidence()
    bundle.update(
        model_policy="offline", plan_origin="saved-plan replay", model_invocations=[]
    )
    text = render_evidence_report(bundle)
    assert "Model policy: offline" in text
    assert "Initial plan origin: saved-plan replay" in text
    assert "No successful model invocations recorded" in text
    assert "Planned rows (not a new measurement)" in text


def test_runtime_contribution_shows_actual_model_and_unavailable_review() -> None:
    bundle = _evidence()
    bundle.update(
        model_policy="local-only",
        plan_origin="model proposal",
        model_invocations=[
            {
                "role": "planner",
                "adapter": "glimmer_local",
                "model": "requested",
                "returned_model": "reported",
            }
        ],
        model_denials=[{"role": "critic", "error": "hosted call refused"}],
        advisories=[{"status": "advisory-failed", "error": "critic unavailable"}],
    )
    text = render_evidence_report(bundle)
    assert "| planner | glimmer_local | requested | reported |" in text
    assert "hosted call refused" in text
    assert "critic unavailable" in text
    assert "Runtime identities are server reports" in text


def test_report_needs_no_model_or_check_modules() -> None:
    """The renderer imports neither model adapters nor check code."""
    import engine.core.report as report_module

    source = Path(report_module.__file__).read_text(encoding="utf-8")
    for fragment in ("models.", "meta_cli", "glimmer", "run_checks"):
        assert fragment not in source


class FakeModel:
    """A StructuredModel double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=5, output_tokens=5, calls=1),
            meta={"model": "fake"},
        )


def _write_source(tmp_path: Path) -> Path:
    csv_path = tmp_path / "orders-tabular.csv"
    lines = [",".join(COLUMNS)]
    lines += [",".join(row[col] for col in COLUMNS) for row in ROWS]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "orders-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "orders-demo",
                "licence": "CC-BY-4.0",
                "attribution": "Demo fixture for tests.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _write_thresholds(tmp_path: Path) -> Path:
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"mean_relative_tolerance": 0.25}) + "\n")
    return path


def _run(
    tmp_path: Path,
    monkeypatch: Any,
    plans: Sequence[Mapping[str, Any]],
    answers: Sequence[str],
    *,
    out_name: str = "out",
) -> tuple[int, dict[str, Any]]:
    source = _write_source(tmp_path)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    out = tmp_path / out_name
    queue = list(answers)
    return execute(
        TABULAR_MANIFEST,
        tab_capabilities.CAPABILITIES,
        source=source,
        max_attempts=2,
        model_factory=lambda role, config: FakeModel(list(plans)),
        out=out,
        say=lambda message: None,
        ask=lambda prompt: queue.pop(0),
    )


def _run_dir(out: Path) -> Path:
    run_dirs = [child for child in out.rglob("run.json") if child.parent.name != "out"]
    assert len(run_dirs) == 1
    return run_dirs[0].parent


def test_released_run_ships_traceable_report(tmp_path: Path, monkeypatch: Any) -> None:
    code, outcome = _run(
        tmp_path, monkeypatch, [{"seed": 7, "n_rows": 20}], ["accept", "Fine."]
    )
    assert code == EXIT_OK
    artifact = _run_dir(tmp_path / "out")
    report_path = artifact / "evidence-report.md"
    assert report_path.is_file()
    text = report_path.read_text(encoding="utf-8")
    record = json.loads((artifact / "run.json").read_text(encoding="utf-8"))
    assert record["run"]["request"]["spec_digest"] in text
    assert outcome["artifact_sha256"] in text
    assert "Fine." in text
    frozen = json.loads((artifact / "frozen-manifest.json").read_text(encoding="utf-8"))
    assert frozen["evidence_digest"] in text
    with zipfile.ZipFile(artifact / "release.zip") as archive:
        assert "evidence-report.md" in archive.namelist()
        assert archive.read("evidence-report.md").decode("utf-8") == text
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert "evidence-report.md" in [entry["path"] for entry in manifest["members"]]
    outcome_check = verify_release(artifact / "release.zip", manifest=manifest)
    assert outcome_check["artifact_sha256"] == outcome["artifact_sha256"]


def test_failed_run_writes_report_without_release(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from profiles.tabular.analysis.calibration import Thresholds

    monkeypatch.setattr(
        tab_capabilities,
        "load_thresholds",
        lambda path: Thresholds(mean_relative_tolerance=-1.0),
    )
    code, outcome = _run(
        tmp_path,
        monkeypatch,
        [{"seed": 7, "n_rows": 20}, {"seed": 8, "n_rows": 20}],
        [],
    )
    assert code == EXIT_FAILED
    assert outcome["status"] == "failed"
    artifact = _run_dir(tmp_path / "out")
    text = (artifact / "evidence-report.md").read_text(encoding="utf-8")
    assert "Decision: none" in text
    assert "mean_value" in text
    assert not (artifact / "release.zip").exists()


def test_report_renders_attached_image_metadata() -> None:
    bundle = _evidence()
    bundle["model_invocations"] = [
        {
            "role": "critic",
            "adapter": "meta_cli",
            "model": "muse-spark-1.3",
            "returned_model": "muse-spark-1.3",
            "attached_image": {
                "artifact_sha256": "img-sha-abc-123",
                "produced_by": "image_synthesize",
                "media_type": "image/png",
                "bytes": 4096,
                "width": 64,
                "height": 32,
            },
        }
    ]
    bundle["images_present"] = True
    text = render_evidence_report(bundle)
    assert "Attached image artifact for critic:" in text
    assert "img-sha-abc-123" in text
    assert "4096 bytes" in text
    assert "64x32" in text
    assert "image/png" in text
    assert "image_synthesize" in text


def test_image_unavailability_is_visible_without_truncated_payload():
    evidence = _evidence()
    evidence["advisories"] = [
        {
            "name": "vision_note",
            "payload": {
                "version": 1,
                "receipt_sha256": "a" * 64,
                "status": "unavailable",
                "evidence_kind": "not_dispatched",
                "provider": None,
                "model": None,
                "cost": {"status": "unavailable", "reason": "offline policy"},
            },
        }
    ]
    rendered = render_evidence_report(evidence)
    assert "Image advisory status: unavailable" in rendered
    assert "evidence: not_dispatched" in rendered
