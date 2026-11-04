"""End-to-end case integration controls: spec, runner, repair, CLI verify, and offline mode.

Covers:
- Case-aware spec validation and strict refusal on undeclared cases or invalid bounds.
- Runner invariant: seed-reroll refusal (seed search forbidden in case runs).
- Runner invariant: target_rows bound enforcement.
- Runner invariant: write-once requirements.
- Condition-driven loop required checks and loop repair routing.
- ``ruloom verify`` CLI subcommand: positive control, external anchors, strip-attack detection,
  and tamper refusal.
- Offline model policy (zero model calls, advisories recorded as unavailable).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from engine import cli
from engine.core.manifest import load_manifest
from engine.core.runner import (
    RunnerError,
    _Runner,
    execute,
)
from engine.core.spec import SpecError, normalize_spec
from engine.core.verdict import CONDITION_CASE_SELECTED, loop_required_ids
from profiles.hylogger import capabilities as hy_capabilities

REPO_ROOT = Path(__file__).resolve().parents[2]
HY_MANIFEST = load_manifest(REPO_ROOT / "profiles" / "hylogger" / "profile.json")

VALID_CASE_ID = "hylogger_scalar_teaching_v1"
VALID_CASE_VERSION = 1


def _make_dummy_source(tmp_path: Path, n_rows: int = 120) -> tuple[Path, str]:
    """Create a minimal synthetic source CSV and companion manifest."""
    src = tmp_path / "TEST_HOLE_01-scalars.csv"
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST"]
    for i in range(n_rows):
        depth = round(10.0 + i * 0.0075, 4)
        is_valid = (i % 4) != 0
        mask = "1" if is_valid else "0"
        swir = "CHLORITE" if (is_valid and (i % 5 != 0)) else ""
        tir = "AMPHIBOLE" if (is_valid and i not in (1, 5)) else ""
        lines.append(f"{depth},{mask},{swir},{tir}")
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raw_bytes = src.read_bytes()
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()

    manifest_p = tmp_path / "TEST_HOLE_01-source.json"
    manifest_p.write_text(
        json.dumps(
            {
                "sha256": raw_sha,
                "dataset_name": "TEST_HOLE_01",
                "licence": "CC-BY-4.0",
                "attribution": "Geological Survey of Western Australia (GSWA)",
                "output_terms": "teaching_only",
            }
        ),
        encoding="utf-8",
    )
    return src, raw_sha


def _make_candidate_csv(
    n_rows: int = 200, depth_start: float = 10.0, spacing: float = 0.0075
) -> bytes:
    output = ["depth_m,valid,swir_group,tir_group\n"]
    depth = depth_start
    for i in range(n_rows):
        is_val = (i % 4) != 0
        sw = "CHLORITE" if (is_val and (i % 5 != 0)) else ""
        ti = "AMPHIBOLE" if (is_val and i not in (1, 5)) else ""
        flag = "true" if is_val else "false"
        output.append(f"{depth:.4f},{flag},{sw},{ti}\n")
        depth += spacing
    return "".join(output).encode("utf-8")


def _valid_case_overlay(source_path: Path, source_sha: str) -> dict[str, Any]:
    return {
        "profile": "hylogger",
        "case": {"id": VALID_CASE_ID, "version": VALID_CASE_VERSION},
        "target_rows": 200,
        "outputs": ["scalar"],
        "source": {"path": str(source_path), "sha256": source_sha},
        "purpose": "teaching lesson for synthetic scalar inspection",
    }


# ==============================================================================
# 1. Spec Validation Controls
# ==============================================================================


def test_normalize_spec_with_case_positive_control(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    overlay = _valid_case_overlay(src, sha)
    spec, digest = normalize_spec(overlay, HY_MANIFEST, source=src, max_attempts=1)

    assert spec["case"] == {"id": VALID_CASE_ID, "version": VALID_CASE_VERSION}
    assert spec["target_rows"] == 200
    assert spec["outputs"] == ["scalar"]
    assert spec["source"]["sha256"] == sha
    assert len(digest) == 64


def test_normalize_spec_refuses_invalid_case_schema(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    base = _valid_case_overlay(src, sha)

    # 1. Case is not an object
    spec = dict(base, case="hylogger_scalar_teaching_v1@1")
    with pytest.raises(SpecError, match="spec 'case' must be an object"):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    # 2. Missing or empty case.id
    spec = dict(base, case={"version": 1})
    with pytest.raises(SpecError, match="spec 'case.id' must be a non-empty string"):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    spec = dict(base, case={"id": "  ", "version": 1})
    with pytest.raises(SpecError, match="spec 'case.id' must be a non-empty string"):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    # 3. Missing or invalid case.version
    spec = dict(base, case={"id": VALID_CASE_ID})
    with pytest.raises(
        SpecError, match="spec 'case.version' must be a positive integer"
    ):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    spec = dict(base, case={"id": VALID_CASE_ID, "version": 0})
    with pytest.raises(
        SpecError, match="spec 'case.version' must be a positive integer"
    ):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    spec = dict(base, case={"id": VALID_CASE_ID, "version": 1.0})
    with pytest.raises(
        SpecError, match="spec 'case.version' must be a positive integer"
    ):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    spec = dict(base, case={"id": VALID_CASE_ID, "version": True})
    with pytest.raises(
        SpecError, match="spec 'case.version' must be a positive integer"
    ):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    # 4. Extra unknown keys in case object
    spec = dict(base, case={"id": VALID_CASE_ID, "version": 1, "extra": "unexpected"})
    with pytest.raises(SpecError, match="spec 'case' carries unknown keys"):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)


def test_normalize_spec_refuses_undeclared_case(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    base = _valid_case_overlay(src, sha)

    # Undeclared case id
    spec = dict(base, case={"id": "undeclared_teaching_case", "version": 1})
    with pytest.raises(SpecError, match="is not declared by profile"):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)

    # Undeclared version of declared case
    spec = dict(base, case={"id": VALID_CASE_ID, "version": 99})
    with pytest.raises(SpecError, match="is not declared by profile"):
        normalize_spec(spec, HY_MANIFEST, source=src, max_attempts=1)


def test_normalize_spec_requires_explicit_target_rows_for_case(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    base = _valid_case_overlay(src, sha)
    del base["target_rows"]

    with pytest.raises(
        SpecError, match="spec 'case' requires an explicit 'target_rows'"
    ):
        normalize_spec(base, HY_MANIFEST, source=src, max_attempts=1)


def test_normalize_spec_requires_scalar_only_for_case(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    base = _valid_case_overlay(src, sha)
    base["outputs"] = ["scalar", "imaging"]

    with pytest.raises(
        SpecError, match="spec 'case' requires outputs == \\['scalar'\\]"
    ):
        normalize_spec(base, HY_MANIFEST, source=src, max_attempts=1)


def test_normalize_spec_requires_64_hex_source_pin_for_case(tmp_path: Path) -> None:
    src, _ = _make_dummy_source(tmp_path)
    base = _valid_case_overlay(src, "0" * 64)

    # Null pin
    base["source"]["sha256"] = None
    with pytest.raises(
        SpecError, match="requires a non-null 64-character hexadecimal source pin"
    ):
        normalize_spec(base, HY_MANIFEST, source=src, max_attempts=1)

    # 63 characters (too short)
    base["source"]["sha256"] = "a" * 63
    with pytest.raises(
        SpecError, match="requires a non-null 64-character hexadecimal source pin"
    ):
        normalize_spec(base, HY_MANIFEST, source=src, max_attempts=1)

    # Non-hex character
    base["source"]["sha256"] = ("a" * 63) + "z"
    with pytest.raises(
        SpecError, match="requires a non-null 64-character hexadecimal source pin"
    ):
        normalize_spec(base, HY_MANIFEST, source=src, max_attempts=1)


def _make_runner(
    tmp_path: Path,
    spec: dict[str, Any],
    spec_digest: str,
    source: Path,
    max_attempts: int = 1,
) -> _Runner:
    return _Runner(
        manifest=HY_MANIFEST,
        registry=hy_capabilities.CAPABILITIES,
        source=source,
        max_attempts=max_attempts,
        model_factory=lambda role, config: None,
        out=tmp_path / "out",
        say=lambda msg: None,
        ask=lambda prompt: "accept",
        spec=spec,
        spec_digest=spec_digest,
    )


# ==============================================================================
# 2. Runner Invariants (Target Rows, Seed Rerolls, Write-Once Requirements)
# ==============================================================================


def test_runner_store_plan_enforces_target_rows_in_case_run(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    spec, spec_digest = normalize_spec(
        _valid_case_overlay(src, sha), HY_MANIFEST, source=src, max_attempts=1
    )

    runner = _make_runner(tmp_path, spec, spec_digest, source=src, max_attempts=1)

    # Plan matching target_rows succeeds
    runner.store_plan({"n_rows": 200, "seed": 42, "strategy": "fitted_markov_joint"})
    assert runner.read_plan()["n_rows"] == 200

    # Plan with differing n_rows raises RunnerError
    with pytest.raises(RunnerError, match="differ from requested target_rows 200"):
        runner.store_plan(
            {"n_rows": 150, "seed": 42, "strategy": "fitted_markov_joint"}
        )


def test_runner_store_plan_refuses_seed_reroll_in_case_run(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    spec, spec_digest = normalize_spec(
        _valid_case_overlay(src, sha), HY_MANIFEST, source=src, max_attempts=3
    )

    runner = _make_runner(tmp_path, spec, spec_digest, source=src, max_attempts=3)

    # Attempt 1: store plan with initial seed 42
    runner.store_plan({"n_rows": 200, "seed": 42, "strategy": "fitted_markov_joint"})
    assert runner.read_plan()["seed"] == 42

    # Attempt 2: storing same seed 42 is allowed
    runner.store_plan({"n_rows": 200, "seed": 42, "strategy": "fitted_markov_balanced"})
    assert runner.read_plan()["strategy"] == "fitted_markov_balanced"

    # Attempt 3: attempting to change seed to 99 is loudly refused
    with pytest.raises(
        RunnerError,
        match="differs from initial plan seed 42; seed rerolls are forbidden for case-selected runs",
    ):
        runner.store_plan(
            {"n_rows": 200, "seed": 99, "strategy": "fitted_markov_balanced"}
        )


def test_runner_store_requirements_write_once(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path)
    spec, spec_digest = normalize_spec(
        _valid_case_overlay(src, sha), HY_MANIFEST, source=src, max_attempts=1
    )

    runner = _make_runner(tmp_path, spec, spec_digest, source=src, max_attempts=1)

    req1 = {"binding": {"case_id": VALID_CASE_ID, "case_version": 1}}
    runner.store_requirements(req1)
    assert runner.requirements_record() == req1

    # Second write is strictly forbidden
    with pytest.raises(RunnerError, match="write-once"):
        runner.store_requirements({"binding": {"different": True}})


# ==============================================================================
# 3. Condition-driven Loop Required Checks and Repair Routing
# ==============================================================================


def test_runner_case_selected_condition_and_loop_required_checks(
    tmp_path: Path,
) -> None:
    src, sha = _make_dummy_source(tmp_path)
    spec, spec_digest = normalize_spec(
        _valid_case_overlay(src, sha), HY_MANIFEST, source=src, max_attempts=1
    )

    runner = _make_runner(tmp_path, spec, spec_digest, source=src, max_attempts=1)

    # Condition case_selected is active
    conditions = runner.run_conditions()
    assert conditions.get(CONDITION_CASE_SELECTED) is True

    # Check loop_required_ids under case_selected
    req_ids = loop_required_ids(HY_MANIFEST.checks, conditions=conditions)
    # Must include all 13 case.* checks
    case_req_ids = [cid for cid in req_ids if cid.startswith("case.")]
    assert len(case_req_ids) == 13
    assert "case.row_count" in case_req_ids
    assert "case.ordered_depth" in case_req_ids
    assert "case.downstream_structure_both_states_present" in case_req_ids


def test_manifest_declares_on_fail_by_condition_case_repair() -> None:
    loop_node = next(node for node in HY_MANIFEST.graph if "loop" in node)
    assert loop_node["on_fail_by_condition"] == {"case_selected": "case_repair"}
    assert "case_repair" in HY_MANIFEST.capabilities


# ==============================================================================
# 4. Verify Subcommand CLI (`ruloom verify`) & Consumer Release Verification
# ==============================================================================


def _build_mock_release_zip(
    zip_path: Path,
    members: Mapping[str, bytes],
    manifest_payload: Mapping[str, Any],
) -> None:
    """Build a zip archive carrying manifest.json, checksums.sha256, and declared members."""
    manifest_bytes = json.dumps(manifest_payload, indent=2).encode("utf-8")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", manifest_bytes)
        checksum_lines = [
            f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json\n"
        ]
        for name in sorted(members):
            data = members[name]
            zf.writestr(name, data)
            sha = hashlib.sha256(data).hexdigest()
            checksum_lines.append(f"{sha}  {name}\n")
        zf.writestr("checksums.sha256", "".join(checksum_lines).encode("utf-8"))


def test_ruloom_verify_cli_positive_and_anchor_controls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, sha = _make_dummy_source(tmp_path)
    spec = _valid_case_overlay(src, sha)

    # Prepare case context using profile workflow helper
    case_spec = {
        "case": spec["case"],
        "target_rows": spec["target_rows"],
        "source": {"sha256": spec["source"]["sha256"]},
        "outputs": spec["outputs"],
    }
    case_ctx = hy_capabilities.prepare_case_context(src, case_spec)
    cand_bytes = _make_candidate_csv(200)

    report = hy_capabilities.evaluate_case_context(cand_bytes, case_ctx)
    case_members = hy_capabilities.build_case_members(
        cand_bytes, case_ctx, report, "f" * 64
    )

    all_members = dict(case_members)
    all_members["data.csv"] = cand_bytes
    all_members["evidence.json"] = json.dumps({"audit": "dummy"}).encode("utf-8")
    case_results = hy_capabilities.case_check_results(report)
    all_members["validation-report.json"] = json.dumps(
        {
            "artifact_name": "TEST_HOLE_01:attempt-1",
            "artifact_sha256": hashlib.sha256(cand_bytes).hexdigest(),
            "verdict": "pass",
            "case_utility": report,
            "results": [
                {
                    "name": cr["check_id"],
                    "status": cr["status"],
                    "detail": str(cr.get("detail", "")),
                }
                for cr in case_results
            ],
        }
    ).encode("utf-8")

    manifest_payload = {
        "profile_id": "hylogger",
        "acceptance": {"request_identity": {"profile": "hylogger"}},
        "members": [
            {
                "name": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
            for name, data in all_members.items()
        ]
        + [{"name": "manifest.json"}],
    }

    archive = tmp_path / "release.zip"
    _build_mock_release_zip(archive, all_members, manifest_payload)

    # 1. Positive control: ruloom verify
    ret = cli.main(["verify", str(archive)])
    out, err = capsys.readouterr()
    assert ret == 0, f"verify failed: {err}"
    result = json.loads(out)
    assert "case" in result
    assert result["case"]["verified"] is True
    assert result["case"]["binding"]["case_id"] == VALID_CASE_ID

    # 2. Positive control: --require-case
    ret = cli.main(
        [
            "verify",
            str(archive),
            "--require-case",
            f"{VALID_CASE_ID}@{VALID_CASE_VERSION}",
        ]
    )
    out, _ = capsys.readouterr()
    assert ret == 0

    # 3. Anchor controls: --expect-contract-sha256 and --expect-catalogue-sha256
    contract_sha = result["case"]["binding"]["contract_sha256"]
    cat_sha = result["case"]["binding"]["catalogue_sha256"]

    # Matching anchors pass
    ret = cli.main(
        [
            "verify",
            str(archive),
            "--expect-contract-sha256",
            contract_sha,
            "--expect-catalogue-sha256",
            cat_sha,
        ]
    )
    assert ret == 0

    # Mismatched contract anchor fails
    ret = cli.main(
        [
            "verify",
            str(archive),
            "--expect-contract-sha256",
            "0" * 64,
        ]
    )
    _, err = capsys.readouterr()
    assert ret == 1
    assert "expected contract sha256" in err

    # Mismatched catalogue anchor fails
    ret = cli.main(
        [
            "verify",
            str(archive),
            "--expect-catalogue-sha256",
            "0" * 64,
        ]
    )
    _, err = capsys.readouterr()
    assert ret == 1
    assert "expected catalogue sha256" in err


def test_ruloom_verify_cli_strip_attack_and_tamper_detection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, sha = _make_dummy_source(tmp_path)
    spec = _valid_case_overlay(src, sha)

    case_spec = {
        "case": spec["case"],
        "target_rows": spec["target_rows"],
        "source": {"sha256": spec["source"]["sha256"]},
        "outputs": spec["outputs"],
    }
    case_ctx = hy_capabilities.prepare_case_context(src, case_spec)
    cand_bytes = _make_candidate_csv(200)
    report = hy_capabilities.evaluate_case_context(cand_bytes, case_ctx)
    case_members = hy_capabilities.build_case_members(
        cand_bytes, case_ctx, report, "f" * 64
    )

    val_report_bytes = json.dumps(
        {
            "artifact_name": "TEST_HOLE_01:attempt-1",
            "artifact_sha256": hashlib.sha256(cand_bytes).hexdigest(),
            "verdict": "pass",
            "results": [{"id": "r1", "status": "pass"}],
        }
    ).encode("utf-8")

    # 1. Strip attack: release archive stripped of all case-* members
    stripped_members = {
        "data.csv": cand_bytes,
        "evidence.json": json.dumps({"audit": "dummy"}).encode("utf-8"),
        "validation-report.json": val_report_bytes,
    }
    stripped_manifest = {
        "profile_id": "hylogger",
        "members": [
            {
                "name": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
            for name, data in stripped_members.items()
        ]
        + [{"name": "manifest.json"}],
    }
    stripped_archive = tmp_path / "stripped_release.zip"
    _build_mock_release_zip(stripped_archive, stripped_members, stripped_manifest)

    # Verifying with --require-case detects strip attack and refuses
    ret = cli.main(
        [
            "verify",
            str(stripped_archive),
            "--require-case",
            f"{VALID_CASE_ID}@{VALID_CASE_VERSION}",
        ]
    )
    _, err = capsys.readouterr()
    assert ret == 1
    assert "strip attack detected" in err or "carries no case-binding.json" in err

    # 2. Tampered case member: tampered case-contract.json
    tampered_members = dict(case_members)
    tampered_members["data.csv"] = cand_bytes
    tampered_members["evidence.json"] = json.dumps({"audit": "dummy"}).encode("utf-8")
    tampered_members["validation-report.json"] = val_report_bytes

    # Modify content of case-contract.json without updating manifest checksum
    tampered_members["case-contract.json"] = json.dumps({"tampered": True}).encode(
        "utf-8"
    )
    tampered_manifest = {
        "profile_id": "hylogger",
        "members": [
            {
                "name": name,
                "sha256": hashlib.sha256(
                    case_members[name]
                    if name in case_members
                    else tampered_members[name]
                ).hexdigest(),
                "size": len(
                    case_members[name]
                    if name in case_members
                    else tampered_members[name]
                ),
            }
            for name in tampered_members
        ]
        + [{"name": "manifest.json"}],
    }
    tampered_archive = tmp_path / "tampered_release.zip"
    _build_mock_release_zip(tampered_archive, tampered_members, tampered_manifest)

    ret = cli.main(["verify", str(tampered_archive)])
    _, err = capsys.readouterr()
    assert ret == 1
    assert "does not match" in err or "hash" in err or "discrepancies" in err


# ==============================================================================
# 5. Offline Model Policy Controls
# ==============================================================================


def test_offline_model_policy_records_unavailable_and_makes_no_model_calls(
    tmp_path: Path,
) -> None:
    src, sha = _make_dummy_source(tmp_path)
    spec = _valid_case_overlay(src, sha)

    called_roles: list[str] = []

    def mock_model_factory(role: str, config: Mapping[str, Any]) -> Any:
        called_roles.append(role)
        raise AssertionError(
            f"model factory should never be called under offline policy (role {role})"
        )

    out_dir = tmp_path / "out"
    fixed_plan = {"seed": 42, "n_rows": 200, "strategy": "fitted_markov_joint"}
    code, result = execute(
        HY_MANIFEST,
        hy_capabilities.CAPABILITIES,
        source=src,
        max_attempts=1,
        model_factory=mock_model_factory,
        out=out_dir,
        spec=spec,
        fixed_plan=fixed_plan,
        model_policy="offline",
        ask=lambda prompt: "accept",
    )

    # Offline run must not invoke model factory
    assert called_roles == []

    # Check that advisories are recorded as unavailable in outcome and run.jsonl
    offline_advisories = [
        adv
        for adv in result.get("advisories", [])
        if isinstance(adv.get("payload"), dict)
        and adv["payload"].get("status") == "unavailable"
    ]
    assert len(offline_advisories) >= 1

    run_records = [
        c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()
    ]
    assert len(run_records) == 1

    # Advisory events in run.jsonl must show unavailable
    run_jsonl_path = run_records[0] / "run.jsonl"
    events = [
        json.loads(line)
        for line in run_jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    offline_events = [
        e
        for e in events
        if e.get("status") == "unavailable"
        or (
            isinstance(e.get("detail"), dict)
            and e["detail"].get("reason") == "model policy is offline"
        )
    ]
    assert len(offline_events) >= 1


# ==============================================================================
# 6. Real CLI Run, Real ZIP with Adjacent Manifest & Recipient Verify
# ==============================================================================


def test_real_cli_run_and_verify_with_adjacent_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src, sha = _make_dummy_source(tmp_path, n_rows=120)
    spec_data = _valid_case_overlay(src, sha)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec_data, indent=2), encoding="utf-8")

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            {"seed": 1, "n_rows": 200, "strategy": "fitted_markov_joint"}, indent=2
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "cli_run_out"
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nVerified and approved.\n"))

    ret = cli.main(
        [
            "run",
            str(src),
            "--profile",
            "hylogger",
            "--spec",
            str(spec_file),
            "--plan",
            str(plan_file),
            "--max-attempts",
            "1",
            "--out",
            str(out_dir),
            "--model-policy",
            "offline",
        ]
    )
    assert ret == 0
    _ = capsys.readouterr()  # Drain run output

    run_dirs = [c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    real_zip = run_dir / "release.zip"
    adj_manifest = run_dir / "manifest.json"
    assert real_zip.is_file()
    assert adj_manifest.is_file()

    # Confirm manifest.json is NOT inside release.zip
    with zipfile.ZipFile(real_zip) as zf:
        assert "manifest.json" not in zf.namelist()
        assert "case-binding.json" in zf.namelist()
        assert "data.csv" in zf.namelist()

    # 1. Positive recipient verify with adjacent manifest.json (auto profile resolution)
    ret = cli.main(["verify", str(real_zip)])
    out, err = capsys.readouterr()
    assert ret == 0, f"verify failed: {err}"
    res = json.loads(out)
    assert res.get("case", {}).get("verified") is True
    assert res["case"]["binding"]["case_id"] == VALID_CASE_ID

    # 2. Positive recipient verify with explicit --profile
    ret = cli.main(["verify", str(real_zip), "--profile", "hylogger"])
    out, err = capsys.readouterr()
    assert ret == 0, f"verify --profile failed: {err}"

    # 3. Positive recipient verify with explicit --require-case
    ret = cli.main(
        [
            "verify",
            str(real_zip),
            "--require-case",
            f"{VALID_CASE_ID}@{VALID_CASE_VERSION}",
        ]
    )
    assert ret == 0

    # 4. Anchor controls on real bundle
    contract_sha = res["case"]["binding"]["contract_sha256"]
    cat_sha = res["case"]["binding"]["catalogue_sha256"]
    ret = cli.main(
        [
            "verify",
            str(real_zip),
            "--expect-contract-sha256",
            contract_sha,
            "--expect-catalogue-sha256",
            cat_sha,
        ]
    )
    assert ret == 0
    _ = capsys.readouterr()

    # 5. Clean recipient in isolated directory without adjacent manifest.json
    isolated_dir = tmp_path / "isolated_recipient"
    isolated_dir.mkdir()
    isolated_zip = isolated_dir / "release.zip"
    isolated_zip.write_bytes(real_zip.read_bytes())
    assert not (isolated_dir / "manifest.json").exists()

    ret = cli.main(["verify", str(isolated_zip)])
    out, err = capsys.readouterr()
    assert ret == 0, f"isolated verify without adjacent manifest failed: {err}"
    iso_res = json.loads(out)
    assert iso_res.get("case", {}).get("verified") is True


# ==============================================================================
# 7. Publication Path Positive & Negative Controls
# ==============================================================================


def test_publication_path_positive_and_tamper_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from engine.artifacts.publication import PublicationError, Publisher, download
    from engine.core.dispatch import DispatchStore

    src, sha = _make_dummy_source(tmp_path, n_rows=120)
    spec_data = _valid_case_overlay(src, sha)
    spec_file = tmp_path / "spec_pub.json"
    spec_file.write_text(json.dumps(spec_data, indent=2), encoding="utf-8")

    plan_file = tmp_path / "plan_pub.json"
    plan_file.write_text(
        json.dumps(
            {"seed": 1, "n_rows": 200, "strategy": "fitted_markov_joint"}, indent=2
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "pub_run_out"
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nPublish this run.\n"))

    ret = cli.main(
        [
            "run",
            str(src),
            "--profile",
            "hylogger",
            "--spec",
            str(spec_file),
            "--plan",
            str(plan_file),
            "--max-attempts",
            "1",
            "--out",
            str(out_dir),
            "--model-policy",
            "offline",
        ]
    )
    assert ret == 0
    _ = capsys.readouterr()  # Drain stdout from run
    run_dir = next(
        c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()
    )

    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    spec_digest = run_record["run"]["request"]["spec_digest"]

    dispatch = DispatchStore(out_dir)
    item = dispatch.submit(
        authority="test-curator",
        request={
            "profile": "hylogger",
            "source": str(src),
            "source_sha256": sha,
            "spec_digest": spec_digest,
        },
        idempotency_key="case-pub-k1",
        now=1000.0,
    )
    lease = dispatch.claim(
        item["item_id"], worker="w1", lease_seconds=600.0, now=1000.0
    )["lease"]
    dispatch.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": run_dir.name,
            "artifact_sha256": manifest["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=1000.0,
    )

    published_area = tmp_path / "published_area"
    publisher = Publisher(out_dir, published_area)

    # 1. Positive control: publish case release
    item_id = item["item_id"]
    identity = publisher.publish(item_id, run_dir, authority="test-curator", now=1000.0)
    published_zip = Path(identity["dest"])
    assert published_zip.is_file()
    assert identity["item_id"] == item_id

    # 2. Positive control: download published release and verify
    from engine.artifacts.consumer import verify_release

    dl_zip = tmp_path / "downloaded.zip"
    _ = download(published_zip, dl_zip, manifest=manifest)
    assert dl_zip.is_file()
    dl_identity = verify_release(dl_zip, manifest=manifest)
    assert dl_identity.get("case", {}).get("verified") is True

    # 3. Negative control: publish tampered case member refuses with PublicationError
    tamper_run_dir = tmp_path / "tamper_run"
    import shutil

    shutil.copytree(run_dir, tamper_run_dir)
    tamper_zip = tamper_run_dir / "release.zip"

    # Tamper case-contract.json inside the ZIP
    members = {}
    with zipfile.ZipFile(tamper_zip) as zf:
        for name in zf.namelist():
            members[name] = zf.read(name)
    members["case-contract.json"] = json.dumps({"tampered": True}).encode("utf-8")

    # Rebuild zip with tampered member (checksums will mismatch or case verifier will refuse)
    with zipfile.ZipFile(tamper_zip, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)

    with pytest.raises(PublicationError, match="fails consumer verification"):
        publisher.publish(item_id, tamper_run_dir, authority="test-curator", now=1000.0)


# ==============================================================================
# 7B. Recut Release Report Tamper Regression Controls (F1)
# ==============================================================================


def _recut_zip(
    source_zip: Path,
    target_zip: Path,
    mutate_fn: Any,
) -> Path:
    """Rewrite zip applying mutate_fn and regenerate checksums.sha256."""
    with zipfile.ZipFile(source_zip) as z:
        members = {n: z.read(n) for n in z.namelist() if n != "checksums.sha256"}
    mutated = mutate_fn(members)
    checksums_lines = "".join(
        f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in sorted(mutated.items())
    )
    mutated["checksums.sha256"] = checksums_lines.encode("utf-8")
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in sorted(mutated.items()):
            z.writestr(n, b)
    return target_zip


def test_recut_release_report_tamper_regression_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from engine.artifacts.consumer import ReleaseVerificationError, verify_release
    from engine.artifacts.publication import PublicationError, Publisher
    from engine.core.dispatch import DispatchStore

    src, sha = _make_dummy_source(tmp_path, n_rows=120)
    spec_data = _valid_case_overlay(src, sha)
    spec_file = tmp_path / "spec_recut.json"
    spec_file.write_text(json.dumps(spec_data, indent=2), encoding="utf-8")

    plan_file = tmp_path / "plan_recut.json"
    plan_file.write_text(
        json.dumps(
            {"seed": 1, "n_rows": 200, "strategy": "fitted_markov_joint"}, indent=2
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "recut_run_out"
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nRecut test approved.\n"))

    ret = cli.main(
        [
            "run",
            str(src),
            "--profile",
            "hylogger",
            "--spec",
            str(spec_file),
            "--plan",
            str(plan_file),
            "--max-attempts",
            "1",
            "--out",
            str(out_dir),
            "--model-policy",
            "offline",
        ]
    )
    assert ret == 0
    _ = capsys.readouterr()
    run_dir = next(
        c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()
    )
    real_zip = run_dir / "release.zip"
    assert real_zip.is_file()

    # 1. Positive intact ZIP passes without adjacent manifest.json
    isolated_dir = tmp_path / "isolated_intact"
    isolated_dir.mkdir()
    isolated_zip = isolated_dir / "release.zip"
    isolated_zip.write_bytes(real_zip.read_bytes())
    assert not (isolated_dir / "manifest.json").exists()

    ret = cli.main(["verify", str(isolated_zip), "--profile", "hylogger"])
    out, err = capsys.readouterr()
    assert ret == 0, f"isolated verify failed: {err}"
    iso_res = json.loads(out)
    assert iso_res.get("case", {}).get("verified") is True

    direct_res = verify_release(isolated_zip)
    assert direct_res.get("case", {}).get("verified") is True

    # Helper to test a recut zip mutation in an isolated directory
    recut_base = tmp_path / "recut_cases"
    recut_base.mkdir()

    def run_recut_check(name: str, mut_fn: Any) -> tuple[int, str]:
        case_dir = recut_base / name
        case_dir.mkdir()
        target_zip = case_dir / "release.zip"
        _recut_zip(real_zip, target_zip, mut_fn)
        assert not (case_dir / "manifest.json").exists()

        # Direct verify_release must raise ReleaseVerificationError
        with pytest.raises(ReleaseVerificationError):
            verify_release(target_zip)

        # CLI verify must exit non-zero
        exit_code = cli.main(["verify", str(target_zip), "--profile", "hylogger"])
        _, stderr_text = capsys.readouterr()
        assert exit_code == 1
        return exit_code, stderr_text

    # 2. Recut - Removed case results
    def mut_remove_all_case_results(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        rep["results"] = [
            r for r in rep["results"] if not r.get("name", "").startswith("case.")
        ]
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_rem_all = run_recut_check("rem_all", mut_remove_all_case_results)
    assert "missing required case check" in err_rem_all

    def mut_remove_one_case_result(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        rep["results"] = [
            r for r in rep["results"] if r.get("name") != "case.row_count"
        ]
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_rem_one = run_recut_check("rem_one", mut_remove_one_case_result)
    assert "missing required case check 'case.row_count'" in err_rem_one

    # 3. Recut - Duplicate case result
    def mut_duplicate_case_result(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        matching = [r for r in rep["results"] if r.get("name") == "case.row_count"]
        rep["results"].append(dict(matching[0]))
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_dup = run_recut_check("dup_one", mut_duplicate_case_result)
    assert "duplicate case check 'case.row_count'" in err_dup

    # 4. Recut - Flipped to fail
    def mut_flip_one_fail(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        for r in rep["results"]:
            if r.get("name") == "case.row_count":
                r["status"] = "fail"
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_flip_one = run_recut_check("flip_one", mut_flip_one_fail)
    assert "expected 'pass'" in err_flip_one

    def mut_flip_all_fail(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        for r in rep["results"]:
            if r.get("name", "").startswith("case."):
                r["status"] = "fail"
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_flip_all = run_recut_check("flip_all", mut_flip_all_fail)
    assert "expected 'pass'" in err_flip_all

    # 5. Recut - Changed details
    def mut_changed_detail(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        for r in rep["results"]:
            if r.get("name") == "case.row_count":
                r["detail"] = "tampered detail string 99999"
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_detail = run_recut_check("changed_detail", mut_changed_detail)
    assert "detail does not match" in err_detail

    # 6. Recut - Changed utility
    def mut_utility_passed_false(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        rep["case_utility"]["passed"] = False
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_util_pass = run_recut_check("util_pass_false", mut_utility_passed_false)
    assert "passed is not True" in err_util_pass or "does not match" in err_util_pass

    def mut_utility_removed(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        rep.pop("case_utility", None)
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_util_rem = run_recut_check("util_rem", mut_utility_removed)
    assert "missing required 'case_utility'" in err_util_rem

    def mut_utility_measurement_changed(m: dict[str, bytes]) -> dict[str, bytes]:
        rep = json.loads(m["validation-report.json"].decode("utf-8"))
        rep["case_utility"]["measurements"]["row_count"] = 999
        m["validation-report.json"] = json.dumps(rep).encode("utf-8")
        return m

    _, err_util_meas = run_recut_check("util_meas", mut_utility_measurement_changed)
    assert "does not match shipped case-utility.json" in err_util_meas

    # 7. Publisher inherits the refusal on tampered report release
    dispatch = DispatchStore(out_dir)
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    spec_digest = run_record["run"]["request"]["spec_digest"]

    item = dispatch.submit(
        authority="test-curator",
        request={
            "profile": "hylogger",
            "source": str(src),
            "source_sha256": sha,
            "spec_digest": spec_digest,
        },
        idempotency_key="case-pub-recut-k1",
        now=1000.0,
    )
    lease = dispatch.claim(
        item["item_id"], worker="w1", lease_seconds=600.0, now=1000.0
    )["lease"]
    dispatch.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": run_dir.name,
            "artifact_sha256": manifest["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=1000.0,
    )

    publisher = Publisher(out_dir, tmp_path / "pub_area_recut")

    # Tamper the run directory's release.zip report (flipping case checks to fail)
    tamper_pub_run = tmp_path / "tamper_pub_run"
    import shutil

    shutil.copytree(run_dir, tamper_pub_run)
    _recut_zip(
        tamper_pub_run / "release.zip",
        tamper_pub_run / "release.zip",
        mut_flip_all_fail,
    )
    new_sha = hashlib.sha256((tamper_pub_run / "release.zip").read_bytes()).hexdigest()
    (tamper_pub_run / "release.sha256").write_text(
        f"{new_sha}  release.zip\n", encoding="utf-8"
    )

    with pytest.raises(PublicationError, match="fails consumer verification"):
        publisher.publish(
            item["item_id"], tamper_pub_run, authority="test-curator", now=1000.0
        )


# ==============================================================================
# 8. Legacy Bundle Verification & Strip-Attack Refusal
# ==============================================================================


def test_legacy_bundle_verification_and_strip_attack_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src, _ = _make_dummy_source(tmp_path, n_rows=120)
    out_dir = tmp_path / "legacy_run_out"
    monkeypatch.setattr("sys.stdin", io.StringIO("accept\nLegacy run approved.\n"))

    plan_file = tmp_path / "legacy_plan.json"
    plan_file.write_text(
        json.dumps(
            {"seed": 42, "n_rows": 20, "strategy": "fitted_markov_joint"}, indent=2
        ),
        encoding="utf-8",
    )

    # Legacy profile run (no --spec with case)
    ret = cli.main(
        [
            "run",
            str(src),
            "--profile",
            "hylogger",
            "--plan",
            str(plan_file),
            "--max-attempts",
            "1",
            "--out",
            str(out_dir),
            "--model-policy",
            "offline",
        ]
    )
    assert ret == 0
    _ = capsys.readouterr()  # Drain stdout from run
    run_dir = next(
        c for c in (out_dir / "runs").iterdir() if (c / "run.json").is_file()
    )
    legacy_zip = run_dir / "release.zip"
    assert legacy_zip.is_file()

    # 1. Positive legacy verify: passes cleanly
    ret = cli.main(["verify", str(legacy_zip)])
    out, err = capsys.readouterr()
    assert ret == 0, f"legacy verify failed: {err}"
    res = json.loads(out)
    assert "data.csv" in res["members"]
    assert res.get("case", {}).get("verified") is False
    assert res["case"]["status"] == "not case-checked"

    # 2. Strip attack refusal: --require-case refuses legacy bundle lacking case members
    ret = cli.main(
        [
            "verify",
            str(legacy_zip),
            "--require-case",
            f"{VALID_CASE_ID}@{VALID_CASE_VERSION}",
        ]
    )
    _, err = capsys.readouterr()
    assert ret == 1
    assert "strip attack detected" in err

    # 3. Unexpected case-* member refusal
    unexpected_zip = tmp_path / "unexpected_member.zip"
    members = {}
    with zipfile.ZipFile(legacy_zip) as zf:
        for name in zf.namelist():
            members[name] = zf.read(name)
    members["case-unexpected.json"] = b'{"rogue": true}'

    # Rebuild checksums
    checksum_lines = [
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(members.items())
        if name != "checksums.sha256"
    ]
    members["checksums.sha256"] = "".join(checksum_lines).encode("utf-8")
    with zipfile.ZipFile(unexpected_zip, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)

    from engine.artifacts.consumer import ReleaseVerificationError, verify_release

    with pytest.raises(ReleaseVerificationError, match="unexpected case member"):
        verify_release(unexpected_zip)


# ==============================================================================
# 9. prepare --case Preflight Refusals Before Model Call
# ==============================================================================


def test_prepare_case_preflight_refusals_zero_model_calls(tmp_path: Path) -> None:
    src, _ = _make_dummy_source(tmp_path, n_rows=60)

    class CountingModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str, schema: Any) -> Any:
            self.calls += 1
            raise AssertionError("Model should never be called on preflight failure")

    # 1. Unknown case ID refuses before model call
    model = CountingModel()
    ret = cli.main(
        [
            "prepare",
            str(src),
            "--profile",
            "hylogger",
            "--request",
            "Create practice data",
            "--max-attempts",
            "1",
            "--out",
            str(tmp_path / "draft_unk"),
            "--case",
            "unknown_nonexistent_case@1",
        ],
        model=model,
    )
    assert ret == 1
    assert model.calls == 0

    # 2. Non-positive or invalid version refuses before model call
    model = CountingModel()
    ret = cli.main(
        [
            "prepare",
            str(src),
            "--profile",
            "hylogger",
            "--request",
            "Create practice data",
            "--max-attempts",
            "1",
            "--out",
            str(tmp_path / "draft_badver"),
            "--case",
            f"{VALID_CASE_ID}@0",
        ],
        model=model,
    )
    assert ret == 1
    assert model.calls == 0

    # 3. Missing source file refuses before model call
    model = CountingModel()
    ret = cli.main(
        [
            "prepare",
            str(tmp_path / "nonexistent.csv"),
            "--profile",
            "hylogger",
            "--request",
            "Create practice data",
            "--max-attempts",
            "1",
            "--out",
            str(tmp_path / "draft_nosrc"),
            "--case",
            f"{VALID_CASE_ID}@{VALID_CASE_VERSION}",
        ],
        model=model,
    )
    assert ret == 1
    assert model.calls == 0


# ==============================================================================
# 10. Mandatory AI Handoff Failure Blocks Before Planner / Gate
# ==============================================================================


class RoleMockModel:
    """Mock model returning scripted completions per role."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: dict[str, int] = {}

    def complete(self, prompt: str, schema: Any) -> Any:
        # Determine role from prompt or use default
        role = "unknown"
        if "source assessment" in prompt:
            role = "source_assessor"
        elif "case research" in prompt:
            role = "case_researcher"
        elif "candidate review" in prompt or "case reviewer" in prompt:
            role = "case_reviewer"
        elif "plan" in prompt:
            role = "planner"
        self.calls[role] = self.calls.get(role, 0) + 1

        from models.base import Completion, Usage

        data = self.responses.get(role, {})
        if isinstance(data, Exception):
            raise data
        return Completion(
            data=data,
            usage=Usage(
                model="MockRoleModel",
                calls=1,
                input_tokens=10,
                output_tokens=10,
                cached_tokens=0,
                reasoning_tokens=0,
            ),
            meta={"provider": "mock", "model": "MockRoleModel"},
        )


def test_case_source_assess_bad_citation_blocks_before_plan(tmp_path: Path) -> None:
    src, sha = _make_dummy_source(tmp_path, n_rows=120)
    spec_data = _valid_case_overlay(src, sha)

    # Source assessor returns citation not in admitted primary evidence IDs
    bad_assessor_resp = {
        "case_id": VALID_CASE_ID,
        "source_sha256": sha,
        "cited_evidence_ids": ["EVID-NONEXISTENT-HALLUCINATION"],
        "fit_assessment": "Source is well formatted.",
        "source_limitations": [],
        "teaching_suitability": "high",
    }

    mock = RoleMockModel(
        {
            "source_assessor": bad_assessor_resp,
            "case_researcher": {
                "case_id": VALID_CASE_ID,
                "case_version": VALID_CASE_VERSION,
                "cited_evidence_ids": ["EVID-HYLOG-001"],
                "supported_requirements": ["scalar lesson"],
                "unsupported_requirements": [],
                "research_summary": "Solid evidence.",
                "limitations": [],
            },
            "planner": {"seed": 42, "n_rows": 200, "strategy": "fitted_markov_joint"},
        }
    )

    code, outcome = execute(
        HY_MANIFEST,
        hy_capabilities.CAPABILITIES,
        source=src,
        max_attempts=1,
        model_factory=lambda role, config: mock,
        out=tmp_path / "out_bad_cite",
        spec=spec_data,
        model_policy="hosted",
    )

    # Must fail loudly due to citation validation
    assert code == 1
    assert outcome["status"] == "error"
    assert (
        "unknown or fabricated evidence id" in outcome["error"]
        or "citation" in outcome["error"].lower()
    )
    # Planner must never have been called
    assert "planner" not in mock.calls


def test_case_research_unsupported_requirements_blocks_before_plan(
    tmp_path: Path,
) -> None:
    src, sha = _make_dummy_source(tmp_path, n_rows=120)
    spec_data = _valid_case_overlay(src, sha)

    # Research role identifies unsupported domain requirements
    unsupported_research_resp = {
        "case_id": VALID_CASE_ID,
        "case_version": VALID_CASE_VERSION,
        "cited_evidence_ids": ["auscope_nvcl"],
        "supported_requirements": [],
        "unsupported_requirements": ["Live web search is not supported"],
        "research_summary": "Requirements unsupported.",
        "limitations": [],
    }

    mock = RoleMockModel(
        {
            "source_assessor": {
                "case_id": VALID_CASE_ID,
                "source_sha256": sha,
                "cited_evidence_ids": ["auscope_nvcl"],
                "fit_assessment": "Clean source.",
                "source_limitations": [],
                "teaching_suitability": "high",
            },
            "case_researcher": unsupported_research_resp,
            "planner": {"seed": 42, "n_rows": 200, "strategy": "fitted_markov_joint"},
        }
    )

    code, outcome = execute(
        HY_MANIFEST,
        hy_capabilities.CAPABILITIES,
        source=src,
        max_attempts=1,
        model_factory=lambda role, config: mock,
        out=tmp_path / "out_unsupported",
        spec=spec_data,
        model_policy="hosted",
    )

    assert code == 1
    assert outcome["status"] == "error"
    assert "case research identified unsupported requirements" in outcome["error"]
    assert "planner" not in mock.calls

    # Planner must never have been called
    assert "planner" not in mock.calls


# ==============================================================================
# 11. case_repair Strategy Transition Control
# ==============================================================================


def test_case_repair_transitions_strategy_on_inclusion_fraction_failure(
    tmp_path: Path,
) -> None:
    src, sha = _make_dummy_source(tmp_path, n_rows=120)
    case_spec = {
        "case": {"id": VALID_CASE_ID, "version": VALID_CASE_VERSION},
        "target_rows": 200,
        "source": {"sha256": sha},
        "outputs": ["scalar"],
    }
    case_ctx = hy_capabilities.prepare_case_context(src, case_spec)

    class DummyReport:
        def __init__(self) -> None:
            self.failed = ["swir_fidelity"]
            self.case_utility = {
                "passed": False,
                "failures": ["inclusion_fraction"],  # Un-namespaced utility failure key
            }

    class DummyRunContext:
        def __init__(self) -> None:
            self._plan = {
                "seed": 42,
                "n_rows": 200,
                "target_rows": 200,
                "strategy": "fitted_markov_joint",
            }
            self._req = case_ctx
            self._report = DummyReport()
            self.events: list[tuple[str, str, Any]] = []

        def requirements(self) -> Any:
            return self._req

        def latest_report(self) -> Any:
            return self._report

        def plan(self) -> Any:
            return dict(self._plan)

        def set_plan(self, plan: Any) -> None:
            self._plan = dict(plan)

        def event(self, stage: str, status: str, details: Any = None) -> None:
            self.events.append((stage, status, details))

    ctx = DummyRunContext()
    hy_capabilities.case_repair(ctx)

    repaired_plan = ctx.plan()
    assert repaired_plan["strategy"] == "fitted_markov_balanced"
    assert repaired_plan["seed"] == 42
    assert repaired_plan["n_rows"] == 200
    assert ctx.events == [("case_repair", "ok", {"strategy": "fitted_markov_balanced"})]
