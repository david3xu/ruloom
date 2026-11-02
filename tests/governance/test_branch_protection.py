"""Branch-protection repo-observable half (governance G01).

The live GitHub ruleset (id 26349205, ``main-gate``, active, main-only)
requires the ``gate`` status check on every PR to main. GitHub-side
settings are not product-observable, so the recorded receipt in
``docs/quality/branch-protection-g01.md`` carries the live read-only
verification. What this checker enforces -- the part the repo owns --
is the binding the ruleset depends on: the CI workflow must expose a
job named exactly ``gate``, and that job must run the full suite plus
the executable aggregate gate. Renaming the job or hollowing its steps
would silently break the required check the ruleset gates on, so the
gate fails closed here first.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
RECEIPT = REPO / "docs" / "quality" / "branch-protection-g01.md"

REQUIRED_CONTEXT = "gate"
RULESET_ID = "26349205"


def _workflow() -> dict:
    assert WORKFLOW.is_file(), f"CI workflow missing: {WORKFLOW}"
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), "CI workflow is not a mapping"
    return payload


def test_ci_exposes_required_gate_job() -> None:
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), "CI workflow defines no jobs"
    assert REQUIRED_CONTEXT in jobs, (
        f"CI workflow has no {REQUIRED_CONTEXT!r} job: the live "
        "branch ruleset requires exactly that status-check context"
    )


def test_gate_job_runs_suite_and_force_pass() -> None:
    gate = _workflow()["jobs"][REQUIRED_CONTEXT]
    steps = gate.get("steps", [])
    assert isinstance(steps, list) and steps, "gate job defines no steps"
    runs = " ".join(
        str(step.get("run", "")) for step in steps if isinstance(step, dict)
    )
    assert "pytest" in runs, "gate job never runs the test suite"
    assert "scripts/force_pass.py" in runs, (
        "gate job never runs the executable aggregate gate"
    )


def test_gate_job_covers_push_and_pull_request() -> None:
    workflow = _workflow()
    # YAML 1.1 parses the unquoted `on:` key as boolean True.
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, dict):
        names = set(triggers)
    elif isinstance(triggers, list):
        names = set(triggers)
    else:
        names = set()
    assert {"push", "pull_request"} <= names, (
        "the gate must run on both push and pull_request, otherwise the "
        "required check never reports on PRs"
    )


def test_recorded_receipt_binds_live_ruleset() -> None:
    assert RECEIPT.is_file(), f"G01 receipt missing: {RECEIPT}"
    text = RECEIPT.read_text(encoding="utf-8")
    for field in (
        RULESET_ID,
        "main-gate",
        "refs/heads/main",
        "required_status_checks",
        "gate",
        "rebase",
    ):
        assert field in text, f"G01 receipt no longer records {field!r}"
