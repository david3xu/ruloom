"""Audit regression: R08 sanitised model views and verified rights (RP-04).

The tabular probe in the audit showed literal text cells preserved in
``Profile.to_json`` with the synthetic output recreating the single source
row exactly, and ``build_provenance`` dedicated every release to CC0
without any rights review gate. This suite proves the corrected behaviour:

* rare text literals never reach the model-visible view;
* the generator cannot recreate a single-row source from the disclosed
  vocabulary;
* a sibling ``*-source.json`` hash mismatch fails intake loudly before
  any model call;
* unverified rights produce no CC0/public-dedication claim; a verified
  record with no explicit output-terms grant -- even a permissive or a
  restrictive source licence -- likewise yields NOT CLEARED, while a
  verified explicit grant is stated verbatim with output terms
  recorded as separate release evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine.artifacts.package import (
    OUTPUT_TERMS_GRANT_FIELD,
    build_provenance,
    resolve_output_terms,
    rights_block,
)
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_FAILED, EXIT_OK, execute
from models.base import Completion, Usage
from profiles.hylogger.analysis.statistics import Profile
from profiles.hylogger.generation.plan import build_prompt as hy_build_prompt
from profiles.hylogger.intake.reader import Sample
from profiles.hylogger import capabilities as hy_capabilities
from profiles.tabular import capabilities as tab_capabilities
from profiles.tabular.analysis.statistics import TabularProfile
from profiles.tabular.generation.generate import generate as tab_generate
from profiles.tabular.generation.generate import generate_rows

REPO_ROOT = Path(__file__).resolve().parents[2]
TABULAR_MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")
HYLOGGER_MANIFEST = load_manifest(
    REPO_ROOT / "profiles" / "hylogger" / "profile.json"
)

LITERAL_COLUMNS = ["name", "phone", "diagnosis"]
LITERAL_ROWS = [
    {"name": "Ava Reyes", "phone": "555-0100", "diagnosis": "DX-4417"}
]


class FakePlanner:
    """A planner double replaying queued plans and counting calls."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        self.prompts.append(prompt)
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=1, output_tokens=1, calls=1),
            meta={"model": "fake"},
        )


class RefusingModel:
    """Fails the run if any model call is attempted."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        self.calls += 1
        raise AssertionError("model must not be called before verified intake")


def _write_thresholds(path: Path) -> Path:
    thresholds = path / "thresholds.json"
    thresholds.write_text(
        json.dumps({"mean_relative_tolerance": 0.25}, indent=2) + "\n",
        encoding="utf-8",
    )
    return thresholds


def _write_tabular_source(
    path: Path,
    *,
    stem: str = "r08-tabular",
    columns: Sequence[str] | None = None,
    rows: Sequence[Mapping[str, str]] | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> Path:
    resolved = list(columns) if columns is not None else ["id", "value", "label"]
    body = rows if rows is not None else [
        {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
        for i in range(20)
    ]
    csv_path = path / f"{stem}-tabular.csv"
    lines = [",".join(resolved)]
    lines += [",".join(row[col] for col in resolved) for row in body]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if manifest is not None:
        (path / f"{stem}-source.json").write_text(
            json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return csv_path


def test_single_row_literals_absent_from_model_view() -> None:
    profile = TabularProfile.from_rows(
        LITERAL_COLUMNS, list(LITERAL_ROWS), "solo-tabular", "abc123"
    )
    view = profile.to_disclosed_json()
    for literal in ("Ava Reyes", "555-0100", "DX-4417"):
        assert literal not in view, f"literal leaks to model view: {literal!r}"
    disclosed = profile.disclosed_stats()
    for column in LITERAL_COLUMNS:
        summary = disclosed["text"][column]
        assert summary["values"] == {}
        assert summary["suppressed_values"] == 1
    # The full code-side record is untouched; only the view is sanitised.
    assert profile.stats["text"]["name"]["values"] == {"Ava Reyes": 1}


def test_generator_does_not_recreate_single_source_row() -> None:
    profile = TabularProfile.from_rows(
        LITERAL_COLUMNS, list(LITERAL_ROWS), "solo-tabular", "abc123"
    )
    source = dict(LITERAL_ROWS[0])
    for seed in (1, 2, 3):
        for row in generate_rows(profile, seed=seed, n_rows=5):
            assert row != source
    payload = tab_generate(profile, {"seed": 1, "n_rows": 5})
    text = payload.decode("utf-8")
    for literal in ("Ava Reyes", "555-0100", "DX-4417"):
        assert literal not in text


def test_source_manifest_hash_mismatch_fails_intake_before_model_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_tabular_source(
        tmp_path,
        stem="r08probe",
        manifest={
            "dataset_name": "r08probe",
            "sha256": "0" * 64,
            "licence": "CC-BY-4.0",
            "attribution": "Tampered fixture.",
        },
    )
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    refusing = RefusingModel()
    code, result = execute(
        TABULAR_MANIFEST,
        tab_capabilities.CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=lambda role, config: refusing,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    assert refusing.calls == 0
    assert "mismatch" in result.get("error", "")


def test_hylogger_manifest_mismatch_fails_intake(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "r08hole-scalars.csv"
    csv_path.write_text(
        "StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST\n"
        "10.0,1,A,X\n10.5,1,A,Y\n11.0,0,,\n",
        encoding="utf-8",
    )
    (tmp_path / "r08hole-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "r08hole",
                "sha256": "f" * 64,
                "licence": "CC-BY-4.0",
                "attribution": "Tampered fixture.",
            }
        ),
        encoding="utf-8",
    )
    refusing = RefusingModel()
    code, result = execute(
        HYLOGGER_MANIFEST,
        hy_capabilities.CAPABILITIES,
        source=csv_path,
        max_attempts=1,
        model_factory=lambda role, config: refusing,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: "accept",
    )
    assert code == EXIT_FAILED
    assert refusing.calls == 0
    assert "mismatch" in result.get("error", "")


def _accepted_tabular_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stem: str,
    manifest: Mapping[str, Any] | None,
) -> tuple[FakePlanner, Path]:
    source = _write_tabular_source(tmp_path, stem=stem, manifest=manifest)
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    planner = FakePlanner([{"seed": 7, "n_rows": 20}])
    queue = ["accept", "ok"]
    code, result = execute(
        TABULAR_MANIFEST,
        tab_capabilities.CAPABILITIES,
        source=source,
        max_attempts=2,
        model_factory=lambda role, config: planner,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: queue.pop(0),
    )
    assert code == EXIT_OK
    return planner, Path(str(result["artifact_dir"]))


def test_unverified_rights_yield_no_cc0_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, artifact = _accepted_tabular_run(
        tmp_path, monkeypatch, stem="r08unverified", manifest=None
    )
    provenance = (artifact / "provenance.md").read_text(encoding="utf-8")
    # No dedication is granted: the only rights sentence is the denial.
    assert "CC0" not in provenance
    assert "released under" not in provenance
    assert "NOT CLEARED" in provenance
    assert "carries no\npublic-dedication claim" in provenance
    assert "UNVERIFIED" in provenance
    rights = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))[
        "rights"
    ]
    assert rights["source_hash_verified"] is False
    assert rights["output_terms"] is None


def _write_verified_tabular_source(
    path: Path,
    *,
    stem: str,
    manifest_extra: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    columns = ["id", "value", "label"]
    rows = [
        {"id": str(i), "value": f"{10.0 + (i % 5)}", "label": "a" if i % 2 else "b"}
        for i in range(20)
    ]
    csv_path = path / f"{stem}-tabular.csv"
    csv_path.write_text(
        "\n".join(
            [",".join(columns)]
            + [",".join(row[col] for col in columns) for row in rows]
        )
        + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "dataset_name": stem,
        "sha256": digest,
        "licence": "CC-BY-4.0",
        "attribution": "Verification Attribution 4417.",
        "rows": len(rows),
    }
    if manifest_extra:
        manifest.update(dict(manifest_extra))
    (path / f"{stem}-source.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return csv_path, manifest


def _accepted_verified_tabular_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stem: str,
    manifest_extra: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    csv_path, manifest = _write_verified_tabular_source(
        tmp_path, stem=stem, manifest_extra=manifest_extra
    )
    monkeypatch.setenv("RULOOM_THRESHOLDS", str(_write_thresholds(tmp_path)))
    planner = FakePlanner([{"seed": 7, "n_rows": 20}])
    queue = ["accept", "ok"]
    code, result = execute(
        TABULAR_MANIFEST,
        tab_capabilities.CAPABILITIES,
        source=csv_path,
        max_attempts=2,
        model_factory=lambda role, config: planner,
        out=tmp_path / "out",
        say=lambda message: None,
        ask=lambda prompt: queue.pop(0),
    )
    assert code == EXIT_OK
    artifact = Path(str(result["artifact_dir"]))
    provenance = (artifact / "provenance.md").read_text(encoding="utf-8")
    rights = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))[
        "rights"
    ]
    return provenance, rights, manifest


def test_restrictive_licence_verified_yields_no_cc0_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified restrictive licence is not an output-terms grant."""
    provenance, rights, _ = _accepted_verified_tabular_run(
        tmp_path,
        monkeypatch,
        stem="r08restrictive",
        manifest_extra={
            "licence": "Proprietary - all rights reserved",
            "attribution": "Restricted fixture.",
        },
    )
    assert "CC0" not in provenance
    assert "released under" not in provenance
    assert "NOT CLEARED" in provenance
    assert "verified against sibling manifest" in provenance
    assert rights["source_hash_verified"] is True
    assert rights["source_licence"] == "Proprietary - all rights reserved"
    assert rights["output_terms"] is None
    assert rights["output_terms_grant"] is None
    assert rights["output_terms_grant_field"] is None


def test_verified_licence_without_grant_yields_not_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified permissive licence without an explicit grant clears nothing."""
    provenance, rights, manifest = _accepted_verified_tabular_run(
        tmp_path, monkeypatch, stem="r08nogrant"
    )
    assert "CC0" not in provenance
    assert "released under" not in provenance
    assert "NOT CLEARED" in provenance
    assert "verified against sibling manifest" in provenance
    assert rights["source_hash_verified"] is True
    assert rights["source_sha256"] == manifest["sha256"]
    assert rights["source_licence"] == "CC-BY-4.0"
    assert rights["output_terms"] is None
    assert rights["output_terms_grant"] is None
    assert rights["output_terms_grant_field"] is None


def test_verified_rights_carry_attribution_and_clear_output_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified explicit output-terms grant states the granted term."""
    attribution = "Verification Attribution 4417."
    provenance, rights, manifest = _accepted_verified_tabular_run(
        tmp_path,
        monkeypatch,
        stem="r08verified",
        manifest_extra={"output_terms": "CC0-1.0"},
    )
    assert "CC0-1.0" in provenance
    assert attribution in provenance
    assert "verified against sibling manifest" in provenance
    assert "NOT CLEARED" not in provenance
    assert rights["source_hash_verified"] is True
    assert rights["source_sha256"] == manifest["sha256"]
    assert rights["source_licence"] == "CC-BY-4.0"
    assert rights["source_attribution"] == attribution
    assert rights["output_terms"] == "CC0-1.0"
    assert rights["output_terms_grant"] == "CC0-1.0"
    assert rights["output_terms_grant_field"] == OUTPUT_TERMS_GRANT_FIELD
    assert OUTPUT_TERMS_GRANT_FIELD == "output_terms"
    assert rights["manifest_name"] == "r08verified-source.json"


def test_build_provenance_needs_explicit_output_terms() -> None:
    base = dict(
        source_name="hole",
        source_sha256="abc123",
        licence="CC-BY-4.0",
        attribution="Attr.",
        artifact_sha256="def456",
        plan={"seed": 1, "n_rows": 2},
        verdict="pass",
        run_id="run-1",
    )
    assert (
        resolve_output_terms(
            hash_verified=False, output_terms_grant="CC0-1.0"
        )
        is None
    )
    assert (
        resolve_output_terms(hash_verified=True, output_terms_grant=None)
        is None
    )
    assert (
        resolve_output_terms(hash_verified=True, output_terms_grant="")
        is None
    )
    assert (
        resolve_output_terms(hash_verified=True, output_terms_grant="  ")
        is None
    )
    # A verified explicit grant resolves to the granted term itself.
    assert (
        resolve_output_terms(
            hash_verified=True, output_terms_grant="CC0-1.0"
        )
        == "CC0-1.0"
    )
    # Licence presence is never a grant: no CC0 is ever synthesised.
    granted = resolve_output_terms(
        hash_verified=True,
        output_terms_grant="Proprietary - all rights reserved",
    )
    assert granted is not None and "CC0" not in granted
    # An unverified grant clears nothing but stays recorded as evidence.
    blocked = rights_block(
        source_sha256="abc123",
        licence="CC-BY-4.0",
        attribution="Attr.",
        manifest_name="hole-source.json",
        manifest_sha256="deadbeef",
        hash_verified=False,
        output_terms="CC0-1.0",
        output_terms_grant="CC0-1.0",
    )
    assert blocked["output_terms"] is None
    assert blocked["output_terms_grant"] == "CC0-1.0"
    assert blocked["output_terms_grant_field"] == OUTPUT_TERMS_GRANT_FIELD
    # No dedication is ever implied: the default renders no-clearance.
    assert "CC0" not in build_provenance(**base)
    assert "NOT CLEARED" in build_provenance(**base)
    cleared = build_provenance(
        **base,
        output_terms="CC0-1.0",
        hash_verified=True,
        manifest_name="hole-source.json",
    )
    assert "CC0-1.0" in cleared
    assert "NOT CLEARED" not in cleared


def test_hylogger_singleton_labels_absent_from_model_view() -> None:
    samples = [
        Sample(depth_m=10.0, valid=True, swir_group="Common", tir_group="X"),
        Sample(depth_m=10.5, valid=True, swir_group="Common", tir_group="X"),
        Sample(depth_m=11.0, valid=True, swir_group="Singleton", tir_group="X"),
    ]
    profile = Profile.from_samples(samples, "HOLE", "0" * 64)
    view = profile.to_disclosed_json()
    assert "Singleton" not in view
    assert "Common" in view
    disclosed = profile.disclosed_stats()
    assert disclosed["swir"]["suppressed_label_count"] == 1
    assert disclosed["swir"]["marginals"] == {"Common": 2}
    prompt = hy_build_prompt(profile)
    assert "Singleton" not in prompt
    assert profile.to_disclosed_json() in prompt
    # The full code-side record still carries the complete aggregates.
    assert profile.stats["swir"]["marginals"] == {"Common": 2, "Singleton": 1}
