"""Canary tests for the executable aggregate gate (CP-04 / A07).

The aggregate stage in scripts/force_pass.py executes every enforced Q
checker named by docs/quality/rules.json, blocks on open findings, and
reports planned stubs without executing them. Real-tree tests use an
injected stub runner (fast); the default runner is proven to really
execute pytest against tiny witness files.
"""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "force_pass", REPO_ROOT / "scripts" / "force_pass.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _registry_doc(rules: list[dict]) -> str:
    return json.dumps({"version": 1, "layout": [], "rules": rules})


def _rule(
    rule_id: str,
    status: str,
    checker: str = "tests/audit/test_probe.py",
    implemented: bool = True,
    audit_ref=None,
) -> dict:
    return {
        "id": rule_id,
        "scope": "Q-TEST",
        "requirement": "canary",
        "checker": checker,
        "positive_fixture": "canary",
        "violation_fixture": "canary",
        "required_proof": "canary",
        "owner": "canary",
        "status": status,
        "checker_implemented": implemented,
        "audit_ref": audit_ref,
    }


def _stub_runner(results: dict[str, tuple[bool, str]] | None = None):
    def run(root: Path, checker: str) -> tuple[bool, str]:
        if results is not None and checker in results:
            return results[checker]
        return True, f"{checker} stub-passed"

    return run


FP_OK = {"check_doc1": ("FP-DOC-1", True)}

# --- real tree: classification passes without executing pytest ----------


def test_aggregate_passes_on_real_tree_with_stub_runner() -> None:
    payload = gate.load_registry(REPO_ROOT)
    assert payload is not None
    ok, outcomes = gate.check_aggregate(
        REPO_ROOT, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert ok, outcomes
    by_id = {rule_id: (passed, evidence) for rule_id, passed, evidence in outcomes}
    q_rules = [r for r in payload["rules"] if str(r["id"]).startswith("Q-")]
    enforced = [r for r in q_rules if r["status"] == "enforced"]
    planned = [r for r in q_rules if r["status"] == "planned"]
    assert len([o for o in outcomes if o[1]]) == len(outcomes)
    assert len(by_id) == len(enforced) + len(planned)
    for rule in enforced:
        assert rule["id"] in by_id, rule["id"]
    for rule in planned:
        passed, evidence = by_id[rule["id"]]
        assert passed
        assert "not executed" in evidence


def test_aggregate_real_tree_outcomes_carry_status_tokens() -> None:
    # The entry-point contract prints one Q line per rule carrying its
    # status token; the aggregate evidence must keep that property.
    ok, outcomes = gate.check_aggregate(
        REPO_ROOT, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert ok
    for _, _, evidence in outcomes:
        assert (
            "ENFORCED" in evidence
            or "PLANNED" in evidence
            or "OPEN_FINDING" in evidence
        ), evidence


def test_aggregate_real_tree_never_runs_planned_checkers() -> None:
    seen: list[str] = []

    def recording(root: Path, checker: str) -> tuple[bool, str]:
        seen.append(checker)
        return True, "stub-passed"

    ok, _ = gate.check_aggregate(REPO_ROOT, fp_results=dict(FP_OK), runner=recording)
    assert ok
    payload = gate.load_registry(REPO_ROOT)
    assert payload is not None
    planned_targets = {
        str(rule["checker"]) for rule in payload["rules"] if rule["status"] == "planned"
    }
    assert not (set(seen) & planned_targets)


# --- planted violations must block ----------------------------------------


def test_planted_open_finding_blocks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [
                _rule("Q-OPEN-1", "open_finding", audit_ref="R01"),
                _rule("Q-OK-1", "enforced"),
            ]
        ),
    )
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert not ok
    blocked = [o for o in outcomes if o[0] == "Q-OPEN-1"]
    assert len(blocked) == 1 and not blocked[0][1]
    assert "OPEN_FINDING" in blocked[0][2]


def test_planted_unimplemented_enforced_checker_blocks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc([_rule("Q-NOIMPL-1", "enforced", implemented=False)]),
    )
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert not ok
    assert any(o[0] == "Q-NOIMPL-1" and not o[1] for o in outcomes)


def test_planted_unknown_status_blocks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc([_rule("Q-WEIRD-1", "waived")]),
    )
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert not ok
    assert "unknown status" in outcomes[0][2]


def test_enforced_checker_failure_propagates(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [
                _rule("Q-FAIL-1", "enforced", checker="tests/audit/test_fail.py"),
                _rule("Q-OK-1", "enforced", checker="tests/audit/test_ok.py"),
            ]
        ),
    )
    runner = _stub_runner({"tests/audit/test_fail.py": (False, "1 failed")})
    ok, outcomes = gate.check_aggregate(tmp_path, fp_results=dict(FP_OK), runner=runner)
    assert not ok
    failed = [o for o in outcomes if o[0] == "Q-FAIL-1"]
    assert len(failed) == 1 and not failed[0][1]
    assert "1 failed" in failed[0][2]
    assert any(o[0] == "Q-OK-1" and o[1] for o in outcomes)


def test_missing_checker_file_blocks_fail_closed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [_rule("Q-GONE-1", "enforced", checker="tests/audit/test_gone.py")]
        ),
    )
    ok, outcomes = gate.check_aggregate(tmp_path, fp_results=dict(FP_OK))
    assert not ok
    assert any("missing" in o[2] for o in outcomes if o[0] == "Q-GONE-1")


def test_missing_registry_blocks_fail_closed(tmp_path: Path) -> None:
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert not ok
    assert outcomes[0][0] == "AGGREGATE"


# --- in-gate checkers resolve to the FP run -------------------------------


def test_in_gate_checker_covered_by_fp_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [
                _rule(
                    "Q-INGATE-1", "enforced", checker="scripts/force_pass.py:check_doc1"
                )
            ]
        ),
    )
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results={"check_doc1": ("FP-DOC-1", True)}, runner=_stub_runner()
    )
    assert ok, outcomes
    assert "covered by FP-DOC-1" in outcomes[0][2]


def test_in_gate_checker_fp_failure_propagates(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [
                _rule(
                    "Q-INGATE-1", "enforced", checker="scripts/force_pass.py:check_doc1"
                )
            ]
        ),
    )
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results={"check_doc1": ("FP-DOC-1", False)}, runner=_stub_runner()
    )
    assert not ok
    assert "covered by FP-DOC-1: FAIL" in outcomes[0][2]


def test_in_gate_unknown_member_blocks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [
                _rule(
                    "Q-INGATE-1", "enforced", checker="scripts/force_pass.py:check_nope"
                )
            ]
        ),
    )
    ok, _ = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert not ok


# --- the default runner really executes pytest ----------------------------


def test_default_runner_executes_passing_pytest_file(tmp_path: Path) -> None:
    _write(tmp_path, "test_mini_pass.py", "def test_mini():\n    assert True\n")
    ok, evidence = gate.run_q_checker(tmp_path, "test_mini_pass.py")
    assert ok, evidence
    assert "passed" in evidence


def test_default_runner_reports_failing_pytest_file(tmp_path: Path) -> None:
    _write(tmp_path, "test_mini_fail.py", "def test_mini():\n    assert False\n")
    ok, evidence = gate.run_q_checker(tmp_path, "test_mini_fail.py")
    assert not ok
    assert "exit" in evidence


def test_default_runner_rejects_member_shape(tmp_path: Path) -> None:
    ok, evidence = gate.run_q_checker(tmp_path, "scripts/force_pass.py:check_doc1")
    assert not ok
    assert "unsupported checker shape" in evidence


# --- module-local in-gate checks execute live (CP-05) -------------------------


def test_in_gate_module_local_check_executes_live(tmp_path: Path) -> None:
    # check_code3 is a real module-local check_* function absent from the
    # FP results: the gate executes it in this run instead of treating
    # it as unknown. A stub runner would report "stub-passed" and an
    # unknown member would block; the live evidence names the check.
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [
                _rule(
                    "Q-INGATE-1",
                    "enforced",
                    checker="scripts/force_pass.py:check_code3",
                )
            ]
        ),
    )
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert ok, outcomes
    assert outcomes[0][0] == "Q-INGATE-1"
    assert "in-gate check_code3" in outcomes[0][2]
    # A planted sha256 helper fails the same live check, so the gate
    # executes rather than merely reporting the member.
    _write(tmp_path, "engine/evil.py", "def evil_sha256(x):\n    return x\n")
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert not ok
    assert "in-gate check_code3" in outcomes[0][2]
    assert "evil_sha256" in outcomes[0][2]


def test_in_gate_non_check_member_still_blocks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/quality/rules.json",
        _registry_doc(
            [
                _rule(
                    "Q-INGATE-1",
                    "enforced",
                    checker="scripts/force_pass.py:REGISTRY_REL",
                )
            ]
        ),
    )
    ok, outcomes = gate.check_aggregate(
        tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
    )
    assert not ok
    assert "not in this run" in outcomes[0][2]


def test_in_gate_raising_check_blocks_fail_closed(tmp_path: Path) -> None:
    def _boom(root: Path) -> tuple[bool, str]:
        raise RuntimeError("boom")

    gate.check_boom_canary = _boom  # type: ignore[attr-defined]
    try:
        _write(
            tmp_path,
            "docs/quality/rules.json",
            _registry_doc(
                [
                    _rule(
                        "Q-INGATE-1",
                        "enforced",
                        checker="scripts/force_pass.py:check_boom_canary",
                    )
                ]
            ),
        )
        ok, outcomes = gate.check_aggregate(
            tmp_path, fp_results=dict(FP_OK), runner=_stub_runner()
        )
    finally:
        del gate.check_boom_canary  # type: ignore[attr-defined]
    assert not ok
    assert "raised RuntimeError" in outcomes[0][2]
