"""Tests for the opt-in full-workflow acceptance harness.

Two layers:

* always-on tests of the harness's own logic -- status aggregation (a
  missing or unfinished interface can never read as a pass), redaction,
  environment isolation, the labelled invented fixture, and the tamper
  helpers checked against the REAL consumer-side verifier (no mocks);
* one opt-in live test that runs the whole workflow through the real CLI
  and checks the report is sound. It needs
  ``RULOOM_FULL_WORKFLOW_ACCEPTANCE=1``; set
  ``RULOOM_FULL_WORKFLOW_ACCEPTANCE_STRICT=1`` as well to require an
  overall ``pass`` (the actual acceptance gate). Set
  ``RULOOM_FULL_WORKFLOW_LAYOUT=tree`` to skip the wheel build.

Run just this file: ``.venv/bin/python -m pytest
tests/test_full_workflow_acceptance.py``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "full_workflow_acceptance.py"
DOC = ROOT / "docs" / "full-workflow-acceptance.md"

LIVE = os.environ.get("RULOOM_FULL_WORKFLOW_ACCEPTANCE") == "1"
STRICT = os.environ.get("RULOOM_FULL_WORKFLOW_ACCEPTANCE_STRICT") == "1"
CANARY_SECRET = "ENGINEERING-TEST-CANARY-SECRET-0123456789"


def _load():
    spec = importlib.util.spec_from_file_location("full_workflow_acceptance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


# --------------------------------------------------------------------------
# Status semantics: nothing unfinished may read as a pass
# --------------------------------------------------------------------------


def test_combine_is_worst_wins_and_never_passes_empty_or_unknown():
    assert mod.combine([]) == mod.PENDING
    assert mod.combine([mod.PASS]) == mod.PASS
    assert mod.combine([mod.PASS, mod.PENDING]) == mod.PENDING
    assert mod.combine([mod.PASS, mod.PENDING, mod.FAIL]) == mod.FAIL
    assert mod.combine([mod.PASS, "skipped"]) == mod.FAIL
    assert mod.combine(["weird"]) == mod.FAIL


def test_scenario_without_steps_is_pending():
    assert mod.Scenario("x", "empty").status == mod.PENDING


def test_run_step_maps_every_outcome_and_never_swallows_errors(tmp_path):
    ctx = SimpleNamespace(work=tmp_path)

    ok = mod.run_step(ctx, "a", "ok", lambda: {"detail": "fine", "n": 1})
    assert (ok.status, ok.detail, ok.evidence) == (mod.PASS, "fine", {"n": 1})

    def fails():
        mod.require(False, "nope")

    assert mod.run_step(ctx, "b", "fail", fails).status == mod.FAIL

    def pending():
        raise mod.StepPending("the engine's verify command", "not built")

    step = mod.run_step(ctx, "c", "pending", pending)
    assert step.status == mod.PENDING
    assert step.dependency == "the engine's verify command"

    def crashes():
        raise KeyError("surprise")

    crashed = mod.run_step(ctx, "d", "crash", crashes)
    assert crashed.status == mod.FAIL
    assert "KeyError" in crashed.detail


def test_summary_and_exit_code_never_pass_with_pending_or_failed_steps():
    def scenario(*statuses):
        return mod.Scenario(
            "s",
            "s",
            [
                mod.Step(
                    f"i{n}", "t", status, "d", "dep" if status == mod.PENDING else None
                )
                for n, status in enumerate(statuses)
            ],
        )

    def report(*scenarios):
        return mod.build_report(
            scenarios=list(scenarios),
            ctx=None,
            revision={},
            layout={},
            started="t",
            elapsed_s=0.0,
        )

    assert mod.exit_code(report(scenario(mod.PASS))) == 0
    pending = report(scenario(mod.PASS, mod.PENDING))
    assert pending["summary"]["overall"] == mod.PENDING
    assert mod.exit_code(pending) == 3
    assert pending["pending_dependencies"] == ["dep"]
    failed = report(scenario(mod.PASS, mod.PENDING, mod.FAIL))
    assert failed["summary"]["overall"] == mod.FAIL
    assert mod.exit_code(failed) == 1
    assert mod.exit_code(report()) == 3


def test_report_with_credential_shaped_text_cannot_pass():
    leaky = mod.Scenario(
        "s", "s", [mod.Step("i", "t", mod.PASS, "leaked API_KEY=abcdef123456")]
    )
    report = mod.build_report(
        scenarios=[leaky], ctx=None, revision={}, layout={}, started="t", elapsed_s=0
    )
    assert report["hygiene"]["credential_shaped_text_found"] is True
    assert report["summary"]["overall"] == mod.FAIL


def test_blocked_scenarios_are_pending_and_name_their_dependency():
    blocked = mod.blocked_scenarios("blocked: test", "a working interpreter")
    assert len(blocked) == 7
    assert {s.id for s in blocked} | {"prerequisites"} == set(mod.REQUIRED_SCENARIO_IDS)
    for scenario in blocked:
        assert scenario.status == mod.PENDING
        assert scenario.steps[0].dependency == "a working interpreter"


def test_pending_case_steps_name_the_exact_engine_dependencies():
    for text in (mod.ENGINE_CASE_DEP, mod.ENGINE_VERIFY_DEP, mod.ENGINE_AI_DEP):
        assert "engine" in text
    assert "--case" in mod.ENGINE_CASE_DEP
    assert "--require-case" in mod.ENGINE_VERIFY_DEP
    for stage in mod.CASE_AI_STAGES:
        assert stage in mod.ENGINE_AI_DEP


def test_need_case_is_pending_not_pass_without_the_interface():
    ctx = SimpleNamespace(caps={"prepare_case_option": False})
    with pytest.raises(mod.StepPending):
        mod.need_case(ctx)
    with pytest.raises(mod.StepPending):
        mod.need_verify(SimpleNamespace(caps={}))
    ready = SimpleNamespace(
        caps={
            "prepare_case_option": True,
            "manifest_declares_cases": True,
            "profile_workflow_helpers": True,
        }
    )
    mod.need_case(ready)


def test_verifier_option_without_manifest_cases_is_pending_not_fail():
    # The engine can expose `verify --require-case` before the profile manifest
    # declares any case; case-package steps then have no release to check.
    half = SimpleNamespace(
        caps={
            "verify_require_case": True,
            "prepare_case_option": True,
            "manifest_declares_cases": False,
            "profile_workflow_helpers": True,
        }
    )
    with pytest.raises(mod.StepPending) as info:
        mod.need_verify(half)
    assert info.value.dependency == mod.ENGINE_CASE_DEP


def test_expect_refusal_requires_a_typed_verifier_refusal():
    good = mod.Invocation(
        0,
        {"verified": False, "error_type": "ReleaseVerificationError", "message": "x"},
    )
    assert mod.expect_refusal(good)["refusal"] == "x"
    accepted = mod.Invocation(0, {"verified": True})
    with pytest.raises(mod.StepFailure):
        mod.expect_refusal(accepted)
    crashed = mod.Invocation(
        0, {"verified": False, "error_type": "KeyError", "message": "boom"}
    )
    with pytest.raises(mod.StepFailure):
        mod.expect_refusal(crashed)
    with pytest.raises(mod.HarnessError):
        # No result marker is a broken driver (a harness fault), not a product
        # verdict about the package.
        mod.expect_refusal(mod.Invocation(1, None))


def test_parse_marker_reads_only_the_final_marker_line():
    text = "noise\n" + mod.MARKER + '{"rc": 2}\n'
    assert mod.parse_marker(text) == {"rc": 2}
    assert mod.parse_marker("no marker") is None
    assert mod.parse_marker(mod.MARKER + "not json") is None


# --------------------------------------------------------------------------
# Credentials and environment values are never inherited or logged
# --------------------------------------------------------------------------


def test_redact_removes_credential_shaped_text():
    raw = (
        "Authorization: Bearer abcdefghijklmnop "
        "ANTHROPIC_API_KEY=sk-ant-0123456789abcdef "
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdef "
        "AZURE_CLIENT_SECRET=hunter2hunter2"
    )
    assert mod.contains_secret(raw)
    cleaned = mod.redact(raw)
    for leaked in ("abcdefghijklmnop", "0123456789abcdef", "eyJhbGci", "hunter2"):
        assert leaked not in cleaned
    assert not mod.contains_secret(cleaned)


def test_ordinary_words_are_not_credentials_and_redaction_is_idempotent():
    plain = "[engine] wrote keys=['a', 'b'] tokens=3 author: me"
    assert mod.redact(plain) == plain
    assert not mod.contains_secret(plain)
    redacted = mod.redact("API_KEY=abcdef123456\n\nnext")
    assert not mod.contains_secret(redacted)
    assert mod.redact(redacted) == redacted
    # A real secret inside JSON-escaped text (backslash-n, quotes) is still caught.
    assert mod.contains_secret('{"x": "API_KEY=abcdef123456\\n\\n"}')
    assert not mod.contains_secret(json.dumps({"x": redacted}))


def test_redact_hides_work_and_home_paths_and_truncates(tmp_path):
    text = f"{tmp_path}/out/file {Path.home()}/x" + "y" * 5000
    cleaned = mod.redact(text, hide=[tmp_path], limit=200)
    assert str(tmp_path) not in cleaned
    assert str(Path.home()) not in cleaned
    assert len(cleaned) <= 203


def test_child_env_is_an_allowlist_without_credentials(tmp_path):
    parent = {
        "PATH": "/usr/bin",
        "LANG": "C",
        "ANTHROPIC_API_KEY": CANARY_SECRET,
        "GITHUB_TOKEN": CANARY_SECRET,
        "AZURE_CLIENT_SECRET": CANARY_SECRET,
        "RULOOM_THRESHOLDS": "/tmp/evil.json",
        "RULOOM_ROLE_PLANNER_ADAPTER": "meta_cli",
        "HOME": "/Users/someone",
    }
    env = mod.child_env(tmp_path / "root", tmp_path / "home", parent)
    assert CANARY_SECRET not in json.dumps(env)
    assert not any(name.startswith("RULOOM_") for name in env)
    assert env["HOME"] == str(tmp_path / "home")
    assert env["PYTHONPATH"] == str(tmp_path / "root")
    policy = json.dumps(mod.env_policy(parent))
    assert CANARY_SECRET not in policy
    assert "/usr/bin" not in policy
    assert json.loads(policy)["values_logged"] is False


# --------------------------------------------------------------------------
# Labelled invented fixtures
# --------------------------------------------------------------------------


def test_fixture_is_deterministic_labelled_and_uses_installed_vocabulary(tmp_path):
    first = mod.build_fixture(tmp_path / "one")
    second = mod.build_fixture(tmp_path / "two")
    assert first.csv_sha256 == second.csv_sha256
    assert first.unsuitable_sha256 == second.unsuitable_sha256
    assert first.csv_sha256 != first.unsuitable_sha256
    assert first.csv.name.startswith("INVENTED-")
    sidecar = json.loads(first.sidecar.read_text())
    assert "INVENTED" in sidecar["attribution"]

    vocabulary = json.loads(
        (ROOT / "profiles" / "hylogger" / "thresholds.json").read_text()
    )["vocabulary"]
    rows = [line.split(",") for line in first.csv.read_text().splitlines()]
    assert rows[0][-1] == "Invented_Row_Id"
    masks = {row[1] for row in rows[1:]}
    assert masks == {"0", "1"}
    for row in rows[1:]:
        assert row[-1].startswith(mod.CANARY_PREFIX)
        assert row[2] in vocabulary["swir"] + [""]
        assert row[3] in vocabulary["tir"] + [""]
        if row[1] == "0":
            assert row[2] == row[3] == ""
    single = {
        line.split(",")[1] for line in first.unsuitable_csv.read_text().splitlines()[1:]
    }
    assert single == {"1"}


def test_fixture_median_spacing_is_inside_the_installed_band():
    thresholds = json.loads(
        (ROOT / "profiles" / "hylogger" / "thresholds.json").read_text()
    )["bands"]["spacing_median_m"]
    depths = [
        float(line.split(",")[0]) for line in mod.fixture_csv_text().splitlines()[1:]
    ]
    steps = sorted(b - a for a, b in zip(depths, depths[1:], strict=False) if b > a)
    median = steps[len(steps) // 2]
    assert thresholds["lo"] <= median <= thresholds["hi"]


# --------------------------------------------------------------------------
# Tamper helpers against the REAL consumer-side verifier (no mocks)
# --------------------------------------------------------------------------


@pytest.fixture
def release(tmp_path):
    from engine.artifacts.hashing import sha256_bytes
    from engine.artifacts.package import build_checksums, build_zip_bytes

    data = b"depth_m,valid\n1.0,true\n"
    report = {
        "artifact_name": "t",
        "artifact_sha256": sha256_bytes(data),
        "verdict": "pass",
        "results": [],
    }
    files = {
        "data.csv": data,
        "validation-report.json": (json.dumps(report) + "\n").encode(),
        "evidence-report.md": b"# evidence\n",
    }
    members = {**files, "checksums.sha256": build_checksums(files).encode()}
    path = tmp_path / "release.zip"
    path.write_bytes(build_zip_bytes(members))
    manifest = {
        "members": [
            {"path": n, "sha256": sha256_bytes(b), "size": len(b)}
            for n, b in files.items()
        ]
    }
    return SimpleNamespace(
        path=path, members=members, manifest=manifest, tmp=tmp_path, report=report
    )


def _verify(path, manifest=None):
    from engine.artifacts.consumer import verify_release

    return verify_release(path, manifest=manifest)


def _refused(path, manifest=None):
    from engine.artifacts.consumer import ReleaseVerificationError

    with pytest.raises(ReleaseVerificationError):
        _verify(path, manifest)


def test_untouched_recut_roundtrip_still_verifies(release):
    again = mod.recut_zip(release.members, {}, release.tmp / "again.zip")
    assert _verify(again, release.manifest)["artifact_sha256"]


def test_report_tampers_with_recut_checksums_are_refused(release):
    for key, value in (("verdict", "fail"), ("artifact_sha256", "0" * 64)):
        report = {**release.report, key: value}
        dest = release.tmp / f"{key}.zip"
        mod.recut_zip(
            release.members,
            {"validation-report.json": json.dumps(report).encode()},
            dest,
        )
        _refused(dest)


def test_data_tamper_is_refused_with_and_without_recut(release):
    edits = {"data.csv": release.members["data.csv"] + b"2.0,true\n"}
    stale = mod.recut_zip(
        release.members, edits, release.tmp / "stale.zip", recut_checksums=False
    )
    _refused(stale)
    recut = mod.recut_zip(release.members, edits, release.tmp / "recut.zip")
    _refused(recut)
    _refused(recut, release.manifest)


def test_missing_and_extra_members_are_refused_only_with_an_anchor(release):
    missing = mod.recut_zip(
        release.members, {"evidence-report.md": None}, release.tmp / "missing.zip"
    )
    extra = mod.recut_zip(
        release.members, {"extra.txt": b"x"}, release.tmp / "extra.zip"
    )
    _refused(missing, release.manifest)
    _refused(extra, release.manifest)
    # The documented limit, measured rather than assumed: without an external
    # anchor a re-cut package is only as strong as its own checksums.
    assert _verify(missing)["artifact_sha256"]
    assert _verify(extra)["artifact_sha256"]


def test_path_escape_member_is_refused(release):
    dest = mod.recut_zip(
        release.members, {"../escape.txt": b"x"}, release.tmp / "escape.zip"
    )
    _refused(dest)


def test_mutate_first_number_changes_a_number_and_leaves_input_alone():
    original = {"a": "text", "b": [True, {"c": 0.02}], "d": 5}
    mutated, hit = mod.mutate_first_number(original)
    assert hit and mutated != original
    assert original["b"][1]["c"] == 0.02
    unchanged, hit = mod.mutate_first_number({"a": "x", "b": [True, None]})
    assert not hit and unchanged == {"a": "x", "b": [True, None]}


# --------------------------------------------------------------------------
# Driver: a forbidden model really is forbidden; nothing reaches the network
# --------------------------------------------------------------------------


@pytest.fixture
def live_ctx(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return mod.Ctx(
        work=work,
        install=mod.Install(kind="source-tree", root=ROOT),
        fixture=mod.build_fixture(work),
        python=sys.executable,
    )


def _prepare_argv(ctx, out, max_attempts="2"):
    return [
        "prepare",
        str(ctx.fixture.csv),
        "--profile",
        "hylogger",
        "--request",
        mod.REQUEST_TEXT,
        "--max-attempts",
        max_attempts,
        "--out",
        str(out),
        *mod.PREPARE_ARGS_TAIL,
    ]


def test_driver_counts_a_forbidden_model_call_and_creates_no_draft(live_ctx):
    out = live_ctx.work / "draft"
    inv = mod.cli(live_ctx, _prepare_argv(live_ctx, out))  # no responses: forbid
    result = inv.require_driver_ok()
    assert inv.calls == 1
    assert result["rc"] == 1
    assert not (out / "spec.json").exists()
    assert not (out / "plan.json").exists()


def test_driver_bad_bound_refuses_before_any_model_call(live_ctx):
    out = live_ctx.work / "draft"
    inv = mod.cli(
        live_ctx,
        _prepare_argv(live_ctx, out, "0"),
        responses=[mod.prepare_response()],
    )
    inv.require_driver_ok()
    assert inv.rc == 1
    assert inv.calls == 0


def test_driver_prepare_hands_the_model_no_source_rows(live_ctx):
    out = live_ctx.work / "draft"
    inv = mod.cli(
        live_ctx,
        _prepare_argv(live_ctx, out),
        responses=[mod.prepare_response()],
    )
    inv.require_driver_ok()
    assert inv.rc == 0, inv.text
    assert inv.calls == 1
    assert inv.canary_hits == 0
    spec = json.loads((out / "spec.json").read_text())
    assert spec["source"]["sha256"] == live_ctx.fixture.csv_sha256
    receipt = json.loads((out / "request.json").read_text())
    assert receipt["source_rows_sent"] is False
    assert receipt["prompt_sha256"] in inv.result["prompt_sha256"]


def test_driver_disables_the_network(live_ctx):
    # Run the driver's guard directly: the same socket patches it installs.
    inv = mod.invoke(live_ctx, {"mode": "import_probe"})
    assert inv.require_driver_ok()["imports"]["engine.cli"] == "ok"
    guard = [
        line
        for line in mod.DRIVER_SOURCE.splitlines()
        if line.startswith("socket.") and "= _deny" in line
    ]
    assert {line.split(" = ")[0] for line in guard} >= {
        "socket.socket.connect",
        "socket.create_connection",
        "socket.getaddrinfo",
    }


# --------------------------------------------------------------------------
# Stable revision: a run is valid only if commit and tree never move
# --------------------------------------------------------------------------


def _snap(label, head="a" * 40, digest="d" * 64, entries=(), available=True):
    return {
        "label": label,
        "available": available,
        "git_head": head,
        "tree_digest": digest,
        "changed_path_count": len(entries),
        "_entries": [list(e) for e in entries],
    }


def test_compare_snapshots_is_stable_only_when_commit_and_tree_match():
    same = mod.compare_snapshots([_snap("start"), _snap("after-build"), _snap("end")])
    assert same["stable"] and not same["moved_at"]
    assert "_entries" not in json.dumps(same)  # per-file digests are not reported

    head = mod.compare_snapshots([_snap("start"), _snap("end", head="b" * 40)])
    assert not head["stable"] and head["commit_changed"] and not head["tree_changed"]

    start = _snap("start", entries=[(" M", "engine/core/runner.py", "1")])
    end = _snap("end", digest="e" * 64, entries=[(" M", "engine/core/runner.py", "2")])
    tree = mod.compare_snapshots([start, end])
    assert not tree["stable"] and tree["tree_changed"] and not tree["commit_changed"]
    assert tree["moved_at"] == ["end"]
    assert "engine/core/runner.py" in tree["paths_that_moved"]

    unknown = mod.compare_snapshots([_snap("start"), _snap("end", available=False)])
    assert not unknown["stable"] and unknown["git_unavailable_at"] == ["end"]


def test_tree_snapshot_tracks_commit_and_file_contents(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                *args,
            ],
            check=True,
            capture_output=True,
            env=env,
        )

    git("init", "-q")
    tracked = repo / "tracked.txt"
    tracked.write_text("one\n")
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "first")
    monkeypatch.setattr(mod, "ROOT", repo)

    clean = mod.tree_snapshot("clean")
    assert clean["available"] and clean["changed_path_count"] == 0
    tracked.write_text("two\n")
    first_edit = mod.tree_snapshot("edit-1")
    tracked.write_text("three\n")  # same git status line, different content
    second_edit = mod.tree_snapshot("edit-2")
    assert (
        len(
            {
                clean["tree_digest"],
                first_edit["tree_digest"],
                second_edit["tree_digest"],
            }
        )
        == 3
    )
    (repo / "new.txt").write_text("untracked\n")
    assert mod.tree_snapshot("untracked")["changed_path_count"] == 2

    tracked.write_text("one\n")
    (repo / "new.txt").unlink()
    assert mod.tree_snapshot("reverted")["tree_digest"] == clean["tree_digest"]
    git("commit", "-q", "--allow-empty", "-m", "second")
    assert mod.tree_snapshot("later")["git_head"] != clean["git_head"]


def _scenario(sid, *statuses, fault=None):
    return mod.Scenario(
        sid,
        sid,
        [
            mod.Step(
                f"{sid}-{n}",
                "t",
                status,
                "d",
                "dep" if status == mod.PENDING else None,
                fault=fault if status == mod.FAIL else None,
            )
            for n, status in enumerate(statuses)
        ],
    )


def _all_pass():
    return [_scenario(sid, mod.PASS) for sid in mod.REQUIRED_SCENARIO_IDS]


def _report(scenarios, *, stability=None, strict=False):
    return mod.build_report(
        scenarios=scenarios,
        ctx=None,
        revision={},
        layout={},
        started="t",
        elapsed_s=0.0,
        stability=stability,
        strict=strict,
    )


def test_a_moved_revision_makes_even_a_clean_run_inconclusive():
    moved = mod.compare_snapshots([_snap("start"), _snap("end", digest="e" * 64)])
    report = _report(_all_pass(), stability=moved, strict=True)
    assert report["summary"]["overall"] == mod.INCONCLUSIVE
    assert report["summary"]["observed_overall"] == mod.PASS  # kept for diagnosis
    assert report["summary"]["verdict_kind"] == "inconclusive"
    assert report["strict"]["passed"] is False
    assert any("inconclusive" in r for r in report["strict"]["reasons"])
    assert mod.exit_code(report) == mod.EXIT_INCONCLUSIVE
    # Even a failing run on a moving tree is inconclusive, not a verdict.
    failing = _report([_scenario("s", mod.FAIL)], stability=moved)
    assert failing["summary"]["overall"] == mod.INCONCLUSIVE


def test_run_with_the_wrong_expected_revision_is_inconclusive_without_running(
    monkeypatch,
):
    start = _snap("start")
    monkeypatch.setattr(
        mod, "tree_snapshot", lambda label="snapshot": dict(start, label=label)
    )

    def never(*args, **kwargs):
        raise AssertionError("a wheel was built for the wrong revision")

    monkeypatch.setattr(mod, "build_install", never)
    report = mod.run_acceptance(expect_head="f" * 40, strict=True)
    assert report["summary"]["overall"] == mod.INCONCLUSIVE
    assert report["stability"]["expected_mismatch"]
    assert report["strict"]["passed"] is False
    assert all(s["status"] == mod.PENDING for s in report["scenarios"])


# --------------------------------------------------------------------------
# Strict gate: any failed, skipped or pending required scenario fails it
# --------------------------------------------------------------------------


def test_strict_passes_only_when_every_required_scenario_passes():
    report = _report(_all_pass(), strict=True)
    assert report["strict"] == {
        "enabled": True,
        "passed": True,
        "required_scenarios": list(mod.REQUIRED_SCENARIO_IDS),
        "reasons": [],
    }
    assert mod.exit_code(report) == mod.EXIT_PASS


def test_strict_fails_on_pending_and_is_not_the_lenient_pending_exit_code():
    scenarios = _all_pass()
    scenarios[-1] = _scenario("case-controls", mod.PASS, mod.PENDING)
    strict = _report(scenarios, strict=True)
    assert strict["strict"]["passed"] is False
    assert any(
        "case-controls" in r and "pending" in r for r in strict["strict"]["reasons"]
    )
    assert mod.exit_code(strict) == mod.EXIT_FAIL
    lenient = _report(scenarios, strict=False)
    assert (
        lenient["strict"]["enabled"] is False and lenient["strict"]["passed"] is False
    )
    assert mod.exit_code(lenient) == mod.EXIT_PENDING


def test_strict_fails_on_a_skipped_required_scenario():
    scenarios = [s for s in _all_pass() if s.id != "offline-replay"]
    report = _report(scenarios, strict=True)
    assert report["strict"]["passed"] is False
    assert any(
        "offline-replay" in r and "skipped" in r for r in report["strict"]["reasons"]
    )
    assert mod.exit_code(report) == mod.EXIT_FAIL


def test_strict_lists_failed_steps_with_their_fault():
    scenarios = [
        _scenario(s.id, mod.PASS, mod.FAIL, fault=mod.FAULT_PRODUCT)
        if s.id == "operator-decision"
        else s
        for s in _all_pass()
    ]
    report = _report(scenarios, strict=True)
    assert report["strict"]["passed"] is False
    assert any("product fault" in r for r in report["strict"]["reasons"])


# --------------------------------------------------------------------------
# Product failures are not harness errors
# --------------------------------------------------------------------------


def test_run_step_classifies_who_is_at_fault(tmp_path):
    ctx = SimpleNamespace(work=tmp_path)

    def product():
        mod.require(False, "the engine accepted tampered input")

    def harness():
        mod.harness_require(False, "driver produced no result marker")

    def crash():
        raise KeyError("oops")

    assert mod.run_step(ctx, "a", "t", product).fault == mod.FAULT_PRODUCT
    assert mod.run_step(ctx, "b", "t", harness).fault == mod.FAULT_HARNESS
    assert mod.run_step(ctx, "c", "t", crash).fault == mod.FAULT_HARNESS
    assert mod.run_step(ctx, "d", "t", lambda: {"detail": "ok"}).fault is None
    for step in (
        mod.run_step(ctx, "b", "t", harness),
        mod.run_step(ctx, "c", "t", crash),
    ):
        assert step.status == mod.FAIL  # a harness fault still blocks a pass


def test_a_broken_environment_is_a_harness_fault_not_a_product_failure(tmp_path):
    ctx = SimpleNamespace(
        work=tmp_path,
        install=SimpleNamespace(root=tmp_path, kind="x", wheel_sha256=None),
        caps={
            "interpreter": "3.14.7",
            "imports": {"engine.cli": "ModuleNotFoundError: jsonschema"},
        },
    )
    scenario = mod.scenario_prerequisites(ctx)
    assert [s.status for s in scenario.steps] == [mod.FAIL] * 3
    assert {s.fault for s in scenario.steps} == {mod.FAULT_HARNESS}
    report = _report([scenario])
    assert report["summary"]["product_failures"] == 0
    assert report["summary"]["verdict_kind"] == "harness_error"


def test_case_tamper_controls_wait_for_a_genuine_package_to_verify(monkeypatch):
    # If the verifier refuses everything, a refused tampered package proves
    # nothing, so those controls must be pending, and only the root step fails.
    ctx = SimpleNamespace(cache={})

    def refuses_everything(_ctx):
        raise mod.StepFailure("archive carries no manifest.json")

    monkeypatch.setattr(mod, "case_recipient_control", refuses_everything)
    with pytest.raises(mod.StepPending) as info:
        mod.need_recipient_control(ctx)
    assert "verify-case-anchor" in info.value.dependency
    with pytest.raises(mod.StepFailure):
        mod.case_recipient_root(ctx)


def test_case_tamper_controls_wait_for_a_genuine_lone_zip_to_verify(monkeypatch):
    # The tampered ZIPs are verified alone in a directory with an explicit
    # profile. If a genuine ZIP in that same situation is refused, a refused
    # tampered ZIP proves nothing, so those controls must be pending.
    ctx = SimpleNamespace(cache={})

    def cannot_pick_a_profile(_ctx):
        raise mod.StepFailure("no profile could be determined")

    monkeypatch.setattr(mod, "case_zip_only_control", cannot_pick_a_profile)
    with pytest.raises(mod.StepPending) as info:
        mod.need_zip_only_control(ctx)
    assert "case-recipient-zip-only-verifies" in info.value.dependency
    with pytest.raises(mod.StepFailure):
        mod.case_zip_only_root(ctx)


def test_lone_zip_flags_name_the_profile_and_the_case_anchor():
    assert mod.ZIP_ONLY_FLAGS == (
        "--profile",
        mod.PROFILE_ID,
        "--require-case",
        mod.CASE_REF,
    )


def test_summary_separates_product_failures_from_harness_errors():
    harness_only = _report(
        [_scenario("s", mod.PASS, mod.FAIL, fault=mod.FAULT_HARNESS)]
    )
    assert harness_only["summary"]["harness_errors"] == 1
    assert harness_only["summary"]["product_failures"] == 0
    assert harness_only["summary"]["verdict_kind"] == "harness_error"
    assert mod.exit_code(harness_only) == mod.EXIT_HARNESS_ERROR

    product = _report([_scenario("s", mod.FAIL, fault=mod.FAULT_PRODUCT)])
    assert product["summary"]["verdict_kind"] == "product_failure"
    assert mod.exit_code(product) == mod.EXIT_FAIL

    mixed = _report(
        [
            _scenario("a", mod.FAIL, fault=mod.FAULT_HARNESS),
            _scenario("b", mod.FAIL, fault=mod.FAULT_PRODUCT),
        ]
    )
    assert mixed["summary"]["verdict_kind"] == "product_failure"
    assert mod.exit_code(mixed) == mod.EXIT_FAIL
    unclassified = _report([_scenario("s", mod.FAIL)])
    assert unclassified["summary"]["product_failures"] == 1  # never hidden as harness


# --------------------------------------------------------------------------
# Every report is preserved
# --------------------------------------------------------------------------


def test_report_dir_never_overwrites_an_earlier_report(tmp_path):
    report = _report(_all_pass(), strict=True)
    report["revision"] = {"git_head": "abcdef0123456789", "tree_digest": "f" * 64}
    folder = tmp_path / "reports"
    first = mod.write_preserved_report(folder, report)
    kept = first.read_bytes()
    second = mod.write_preserved_report(folder, report)
    third = mod.write_preserved_report(folder, dict(report, started_at="later"))
    assert len({first, second, third}) == 3
    assert first.read_bytes() == kept
    assert first.name.startswith("report-") and "abcdef01" in first.name
    lines = [json.loads(ln) for ln in (folder / "index.jsonl").read_text().splitlines()]
    assert [ln["file"] for ln in lines] == [first.name, second.name, third.name]
    assert lines[0]["report_sha256"] == mod.sha256_bytes(kept)
    assert lines[0]["strict_passed"] is True and lines[0]["overall"] == mod.PASS


# --------------------------------------------------------------------------
# Case controls, proven with the REAL profile code (no engine mocks)
# --------------------------------------------------------------------------

INVENTED_ID = "invented_primary_source_2026"


@pytest.fixture(scope="module")
def case_env(tmp_path_factory):
    from profiles.hylogger.validation import workflow

    work = tmp_path_factory.mktemp("case-env")
    fixture = mod.build_fixture(work)
    spec = {
        "case": {"id": mod.CASE_ID, "version": mod.CASE_VERSION},
        "target_rows": mod.N_ROWS,
        "source": {"sha256": fixture.csv_sha256},
        "outputs": ["scalar"],
    }
    patch = pytest.MonkeyPatch()
    patch.delenv("RULOOM_THRESHOLDS", raising=False)
    try:
        context = workflow.prepare_case_context(fixture.csv, spec, "CC-BY-4.0", None)
    finally:
        patch.undo()
    return SimpleNamespace(workflow=workflow, fixture=fixture, context=context)


def _plan(strategy=mod.JOINT, seed=mod.SEED, rows=mod.N_ROWS):
    return {"seed": seed, "n_rows": rows, "strategy": strategy}


def test_repair_moves_joint_to_balanced_keeping_seed_and_target(case_env):
    plan = _plan()
    before = json.dumps(plan, sort_keys=True)
    repaired = case_env.workflow.repair_case_plan(
        plan, ["case.inclusion_fraction"], case_env.context
    )
    assert repaired == _plan(strategy=mod.STRATEGY)
    assert json.dumps(plan, sort_keys=True) == before  # the input plan is untouched


def test_repair_changes_nothing_when_no_supported_correction_applies(case_env):
    repair = case_env.workflow.repair_case_plan
    for failed in (
        [],
        ["case.row_count"],
        ["case.depth_spacing", "case.tir_answer_rate"],
    ):
        assert repair(_plan(), failed, case_env.context) == _plan()
    balanced = _plan(strategy=mod.STRATEGY)
    assert repair(balanced, ["case.inclusion_fraction"], case_env.context) == balanced


def test_repair_can_never_reroll_the_seed_or_change_the_target(case_env):
    repair = case_env.workflow.repair_case_plan
    for failed in (
        [],
        ["case.inclusion_fraction"],
        list(case_env.workflow.CASE_CHECK_IDS),
    ):
        for seed in (0, 8, 4294967295):
            assert repair(_plan(seed=seed), failed, case_env.context)["seed"] == seed
    for bad in (True, 8.0, "8", None):
        with pytest.raises(ValueError):
            repair(_plan(seed=bad), ["case.inclusion_fraction"], case_env.context)
    with pytest.raises(ValueError):
        repair(
            _plan(rows=mod.N_ROWS + 1), ["case.inclusion_fraction"], case_env.context
        )
    with pytest.raises(TypeError):
        repair(_plan(), "case.inclusion_fraction", case_env.context)


def test_repair_has_no_model_parameter(case_env):
    parameters = list(inspect.signature(case_env.workflow.repair_case_plan).parameters)
    assert parameters == ["plan", "failed_ids", "context"]


# --------------------------------------------------------------------------
# No seed search; fixed disclosed controls; nothing passes without the positive
# control; the three product findings stay open
# --------------------------------------------------------------------------


def test_the_harness_performs_no_seed_search():
    for name in ("SEED_SEARCH", "case_seed", "joint_failing_seed"):
        assert not hasattr(mod, name), f"{name} would search or select a seed"
    tree = ast.parse(SCRIPT.read_text())
    loops = [
        ast.unparse(node.target)
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and "seed" in ast.unparse(node.target).lower()
    ]
    assert not loops, f"a loop iterates over seeds: {loops}"
    # Every seed the harness ever hands the product is one of the three pinned
    # controls (or the legacy plan seed); none is computed.
    pinned = {
        mod.KNOWN_GOOD_CASE_SEED,
        mod.KNOWN_QUALITY_FAILURE_SEED,
        mod.REPAIR_CONTROL_SEED,
        mod.SEED,
    }
    assert all(type(s) is int for s in pinned)


def test_the_harness_never_overrides_the_calibrated_thresholds(tmp_path):
    # The environment override is never inherited by the product under test.
    parent = {"PATH": "/usr/bin", "RULOOM_THRESHOLDS": str(tmp_path / "evil.json")}
    assert "RULOOM_THRESHOLDS" not in mod.child_env(tmp_path, tmp_path, parent)


def test_controls_are_fixed_distinct_and_disclosed():
    seeds = {
        mod.KNOWN_GOOD_CASE_SEED,
        mod.KNOWN_QUALITY_FAILURE_SEED,
        mod.REPAIR_CONTROL_SEED,
    }
    assert len(seeds) == 3 and all(type(s) is int for s in seeds)
    controls = mod.CONTROLS
    assert controls["search"].startswith("none")
    assert controls["known_good_release_positive"]["seed"] == mod.KNOWN_GOOD_CASE_SEED
    assert controls["known_quality_failure"]["seed"] == mod.KNOWN_QUALITY_FAILURE_SEED
    assert controls["repair_control"]["strategy"] == mod.JOINT
    for key in (
        "known_good_release_positive",
        "known_quality_failure",
        "repair_control",
    ):
        assert "CONTROL" in controls[key]["label"]
    report = _report(_all_pass())
    assert report["fixtures"]["controls"] == controls


def test_default_case_roles_use_the_fixed_known_good_seed(monkeypatch):
    captured = {}

    def fake_prepare(ctx, source, source_sha, *, roles=None, max_attempts=2):
        captured["roles"] = roles
        raise mod.StepFailure("stop after reading the roles")

    monkeypatch.setattr(mod, "case_prepare", fake_prepare)
    with pytest.raises(mod.StepFailure):
        mod.case_execute(
            SimpleNamespace(fixture=SimpleNamespace(csv="x", csv_sha256="y")), "n"
        )
    assert captured["roles"]["plan"]["seed"] == mod.KNOWN_GOOD_CASE_SEED
    assert captured["roles"]["plan"]["strategy"] == mod.STRATEGY


def _stub_ctx(tmp_path):
    catalogue = json.loads((ROOT / "profiles" / "hylogger" / "cases.json").read_text())
    caps = {
        "prepare_case_option": True,
        "manifest_declares_cases": True,
        "profile_workflow_helpers": True,
        "verify_require_case": True,
        "manifest_cases": [
            {
                "id": mod.CASE_ID,
                "version": mod.CASE_VERSION,
                "catalogue_sha256": mod.sha256_canonical(catalogue),
            }
        ],
        "case_member_names": ["case-binding.json"],
        "case_check_ids": ["case.row_count"],
    }
    fixture = SimpleNamespace(
        csv=tmp_path / "x.csv", csv_sha256="0" * 64, unsuitable_csv=tmp_path / "u.csv"
    )
    return SimpleNamespace(
        work=tmp_path,
        caps=caps,
        cache={},
        hashes={},
        observations=[],
        fixture=fixture,
        install=SimpleNamespace(root=ROOT),
        fresh=lambda prefix: tmp_path / prefix,
    )


def test_no_case_step_can_pass_when_the_known_good_control_fails(tmp_path, monkeypatch):
    # The genuine positive control (an accepted, released case run) fails.
    def control_fails(ctx):
        raise mod.StepFailure("the known-good control did not release")

    monkeypatch.setattr(mod, "case_accept_raw", control_fails)
    scenario = mod.scenario_case_controls(_stub_ctx(tmp_path))
    by_id = {s.id: s for s in scenario.steps}
    # The manifest pin does not depend on running the route; the root reports.
    assert by_id["case-manifest-pins-catalogue"].status == mod.PASS
    assert by_id[mod.CASE_ROOT_STEP].status == mod.FAIL
    assert by_id[mod.CASE_ROOT_STEP].fault == mod.FAULT_PRODUCT
    leaked = [
        s.id
        for s in scenario.steps
        if s.id not in ("case-manifest-pins-catalogue", mod.CASE_ROOT_STEP)
        and s.status != mod.PENDING
    ]
    assert not leaked, f"these passed or failed without a working control: {leaked}"
    for step in scenario.steps:
        if step.status == mod.PENDING:
            assert step.dependency


def test_the_prepare_refusals_wait_for_a_working_valid_prepare(tmp_path, monkeypatch):
    ctx = _stub_ctx(tmp_path)

    def prepare_broken(c, *args, **kwargs):
        raise mod.StepFailure("valid prepare is broken")

    monkeypatch.setattr(mod, "case_prepare", prepare_broken)
    scenario = mod.scenario_request_preparation(ctx)
    by_id = {s.id: s for s in scenario.steps}
    assert by_id["case-prepare-pins-case-and-source"].status == mod.FAIL
    for step_id in (
        "case-prepare-unknown-case-zero-calls",
        "case-prepare-unknown-version-zero-calls",
        "case-unsuitable-source-refused-before-model",
        "case-infeasible-count-refused-before-draft",
        "case-source-change-during-preparation-refused",
    ):
        assert by_id[step_id].status == mod.PENDING, step_id


def test_repair_controls_are_pending_when_the_pinned_plan_no_longer_fails():
    def rec(failed):
        return SimpleNamespace(
            receipts=lambda: [{"seed": 1, "verdict": "fail"}],
            checks=lambda: [[{"check_id": c, "status": "fail"} for c in failed]],
        )

    assert mod.first_attempt_failed(
        rec(["case.inclusion_fraction"]), "case.inclusion_fraction"
    )
    assert not mod.first_attempt_failed(
        rec(["case.row_count"]), "case.inclusion_fraction"
    )
    assert not mod.first_attempt_failed(rec([]), "case.inclusion_fraction")
    empty = SimpleNamespace(receipts=lambda: [], checks=lambda: [])
    assert not mod.first_attempt_failed(empty, "case.inclusion_fraction")


def test_the_three_open_findings_track_their_own_steps():
    ids = [f["id"] for f in mod.OPEN_FINDINGS]
    assert ids == ["F1", "F2", "F3"]
    assert {f["step"] for f in mod.OPEN_FINDINGS} == {
        "verify-case-anchor",
        "case-supported-correction-same-seed",
        "case-prepare-unknown-case-zero-calls",
    }

    def scenarios(status_by_step):
        return [
            mod.Scenario(
                f["scenario"],
                f["scenario"],
                [
                    mod.Step(
                        f["step"],
                        "t",
                        status_by_step[f["step"]],
                        "d",
                        "dep" if status_by_step[f["step"]] == mod.PENDING else None,
                    )
                ],
            )
            for f in mod.OPEN_FINDINGS
        ]

    steps = {f["step"]: mod.FAIL for f in mod.OPEN_FINDINGS}
    open_state = mod.findings_state(scenarios(steps))
    assert all(f["state"] == "open: reproduced" for f in open_state)
    steps["verify-case-anchor"] = mod.PASS
    steps["case-supported-correction-same-seed"] = mod.PENDING
    mixed = {f["id"]: f["state"] for f in mod.findings_state(scenarios(steps))}
    assert mixed["F1"].startswith("not reproduced")
    assert mixed["F2"].startswith("unverified")
    assert mixed["F3"] == "open: reproduced"
    assert all(f["state"].startswith("unverified") for f in mod.findings_state([]))
    report = _report(
        scenarios({f["step"]: mod.FAIL for f in mod.OPEN_FINDINGS}), strict=True
    )
    assert [f["id"] for f in report["findings"]] == ["F1", "F2", "F3"]
    assert report["strict"]["passed"] is False  # an open finding can never pass


@pytest.fixture
def role_mock():
    """The driver's RoleMock, loaded from the driver source the harness ships."""
    source = mod.DRIVER_SOURCE
    body = source[source.index("class RoleMock") : source.index("def make_model")]

    def build(mutate=None, **extra):
        namespace = {
            "re": re,
            "json": json,
            "hashlib": hashlib,
            "OUT": {"model_calls": 0, "canary_hits": 0, "prompt_sha256": []},
            "CANARY": None,
            "LABEL": mod.MOCK_LABEL,
        }
        exec(body, namespace)  # noqa: S102 -- the harness's own trusted source
        return namespace["RoleMock"]({"mutate": mutate or {}, **extra})

    return build


def test_role_mock_routes_every_real_schema(case_env, role_mock):
    from engine.cli.request import request_schema
    from engine.core.manifest import load_manifest
    from profiles import manifest_path
    from profiles.hylogger.generation.plan import provider_schema

    route = role_mock().role_of
    workflow = case_env.workflow
    assert route(workflow.RESEARCH_CASE_SCHEMA) == "research"
    assert route(workflow.ASSESS_SOURCE_SCHEMA) == "assess"
    assert route(workflow.REVIEW_CANDIDATE_SCHEMA) == "review"
    assert route(provider_schema([mod.STRATEGY])) == "plan"
    assert route(request_schema(load_manifest(manifest_path("hylogger")))) == "prepare"


def test_role_mock_touch_changes_only_the_named_copy_and_only_while_interpreting(
    tmp_path, role_mock
):
    from engine.cli.request import request_schema
    from engine.core.manifest import load_manifest
    from profiles import manifest_path
    from profiles.hylogger.generation.plan import provider_schema

    target = tmp_path / "copy.csv"
    target.write_text("a\n")
    bystander = tmp_path / "shared.csv"
    bystander.write_text("b\n")
    mock = role_mock(
        touch={"path": str(target), "append": "x\n"},
        prepare=mod.prepare_response(),
        plan=mod.plan_response(),
    )
    # Not the interpreting role: nothing changes.
    mock.complete("p", provider_schema([mod.STRATEGY]))
    assert target.read_text() == "a\n"
    # The interpreting (prepare) role changes only the named throwaway copy.
    mock.complete("p", request_schema(load_manifest(manifest_path("hylogger"))))
    assert target.read_text() == "a\nx\n"
    assert bystander.read_text() == "b\n"


PURPOSE = "Teach inspection of depth-linked inclusion flags and blank labels."


def test_valid_research_and_assessment_handoffs_pass_the_real_validators(
    case_env, role_mock
):
    research = case_env.workflow.research_case(role_mock(), PURPOSE, case_env.context)
    assert research["advisory_kind"] == "case_research"
    assert research["cited_evidence_ids"]
    assessed = case_env.workflow.assess_case_source(role_mock(), case_env.context)
    assert assessed["advisory_kind"] == "source_assessment"
    assert assessed["source_sha256"] == case_env.fixture.csv_sha256


def test_invented_or_tampered_research_citations_are_refused(case_env, role_mock):
    research = case_env.workflow.research_case
    invented = {
        "research": {"cited_evidence_ids": [INVENTED_ID], "passage_citations": []}
    }
    with pytest.raises(ValueError):
        research(role_mock(invented), PURPOSE, case_env.context)
    admitted = research(role_mock(), PURPOSE, case_env.context)["cited_evidence_ids"][0]
    forged = {
        "research": {
            "passage_citations": [
                {"evidence_id": admitted, "passage_sha256": "0" * 64, "relevance": "x"}
            ]
        }
    }
    with pytest.raises(ValueError):
        research(role_mock(forged), PURPOSE, case_env.context)


def test_assessment_naming_another_source_is_refused(case_env, role_mock):
    wrong = {"assess": {"source_sha256": "0" * 64}}
    with pytest.raises(ValueError):
        case_env.workflow.assess_case_source(role_mock(wrong), case_env.context)


def _candidate(case_env):
    from profiles.hylogger.analysis.statistics import Profile
    from profiles.hylogger.generation.generate import generate
    from profiles.hylogger.intake.reader import read_scalar_csv

    fixture = case_env.fixture
    profile = Profile.from_samples(
        read_scalar_csv(fixture.csv), fixture.csv.stem, fixture.csv_sha256
    )
    payload = generate(profile, _plan(strategy=mod.STRATEGY))
    return payload, case_env.workflow.evaluate_case_context(payload, case_env.context)


def test_reviewer_must_acknowledge_the_code_verdict(case_env, role_mock):
    payload, utility = _candidate(case_env)
    review = case_env.workflow.review_case_candidate
    advisory = review(role_mock(), payload, case_env.context, utility)
    assert advisory["advisory_kind"] == "candidate_review"
    # The current contract wants structured facts quoting verified measurements.
    assert advisory["evidence_facts"]
    for fact in advisory["evidence_facts"]:
        assert set(fact) == {"measurement", "value"}
    code_says = "pass" if utility.get("passed") else "fail"
    assert advisory["code_verdict_acknowledged"] == code_says
    opposite = "fail" if code_says == "pass" else "pass"
    contradict = {"review": {"code_verdict_acknowledged": opposite}}
    with pytest.raises(ValueError):
        review(role_mock(contradict), payload, case_env.context, utility)
    # A claimed measurement that disagrees with the recomputation is refused.
    wrong_fact = {
        "review": {"evidence_facts": [{"measurement": "row_count", "value": 999999}]}
    }
    with pytest.raises(ValueError):
        review(role_mock(wrong_fact), payload, case_env.context, utility)
    with pytest.raises(ValueError):
        review(role_mock(), payload + b"x", case_env.context, utility)


# --------------------------------------------------------------------------
# Ruff (the repository's own tool, run from the existing .venv on these files)
# --------------------------------------------------------------------------

RUFF = ROOT / ".venv" / "bin" / "ruff"
OWNED_PYTHON = (SCRIPT, Path(__file__).resolve())


def _ruff(*args):
    return subprocess.run(
        [str(RUFF), *args, *map(str, OWNED_PYTHON)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(not RUFF.exists(), reason="no .venv/bin/ruff")
def test_owned_python_files_pass_ruff_check_and_format():
    check = _ruff("check")
    assert check.returncode == 0, check.stdout + check.stderr
    formatted = _ruff("format", "--check")
    assert formatted.returncode == 0, formatted.stdout + formatted.stderr


# --------------------------------------------------------------------------
# Opt-in behaviour and repository rules
# --------------------------------------------------------------------------


def test_main_is_opt_in_and_does_not_run_by_default(monkeypatch, capsys):
    monkeypatch.delenv(mod.OPT_IN_ENV, raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("harness ran without opt-in")

    monkeypatch.setattr(mod, "run_acceptance", boom)
    assert mod.main([]) == 4
    assert "opt-in" in capsys.readouterr().err


def test_print_revision_reports_git_state_without_opt_in_or_running(
    monkeypatch, capsys
):
    monkeypatch.delenv(mod.OPT_IN_ENV, raising=False)
    snap = _snap(
        "print-revision", digest="d" * 64, entries=[(" M", "engine/x.py", "1")]
    )

    def never_runs(*args, **kwargs):
        raise AssertionError("--print-revision started a run")

    monkeypatch.setattr(mod, "run_acceptance", never_runs)
    monkeypatch.setattr(
        mod, "tree_snapshot", lambda label="snapshot": dict(snap, label=label)
    )
    assert mod.main(["--print-revision"]) == mod.EXIT_PASS
    printed = json.loads(capsys.readouterr().out)
    assert printed["git_head"] == snap["git_head"]
    assert printed["tree_digest"] == "d" * 64
    assert not any(key.startswith("_") for key in printed)  # no per-file digests
    monkeypatch.setattr(
        mod,
        "tree_snapshot",
        lambda label="snapshot": dict(snap, label=label, available=False),
    )
    assert mod.main(["--print-revision"]) == mod.EXIT_FAIL  # unknown is not a pin


def test_main_refuses_to_overwrite_an_existing_report(tmp_path, monkeypatch):
    existing = tmp_path / "report.json"
    existing.write_text("{}")
    monkeypatch.setattr(mod, "run_acceptance", lambda *a, **k: 1 / 0)
    assert mod.main(["--run", "--report", str(existing)]) == 2
    assert existing.read_text() == "{}"


def test_resolve_python_keeps_venv_symlink_path():
    assert mod.resolve_python(None) is None
    resolved = mod.resolve_python(".venv/bin/python")
    assert resolved == str(ROOT / ".venv" / "bin" / "python")


def test_python_requirement_reads_the_project_bound():
    ok, detail = mod.python_requirement("3.12.4")
    assert ok and "3.12.4" in detail
    assert not mod.python_requirement("3.14.7")[0]
    assert not mod.python_requirement("3.11.9")[0]
    assert not mod.python_requirement("unknown")[0]


def test_script_defines_no_sha256_helper_and_reuses_the_engine_one():
    # AGENTS.md FP-CODE-3: only engine/artifacts/hashing.py defines one.
    tree = ast.parse(SCRIPT.read_text())
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert not [n for n in names if "sha256" in n]
    assert mod.sha256_bytes.__module__ == "engine.artifacts.hashing"


def test_script_makes_no_model_or_network_calls_of_its_own():
    source = SCRIPT.read_text()
    for forbidden in ("urllib.request", "http.client", "requests", "import socket\n"):
        # The only socket import is inside the driver string, where it disables
        # sockets; the controller itself imports none of these.
        head = source.split("DRIVER_SOURCE = ", 1)[0]
        assert forbidden not in head


def test_revision_info_records_head_and_working_tree_state():
    info = mod.revision_info()
    assert {"git_head", "branch", "working_tree_clean", "changed_paths_sha256"} <= set(
        info
    )
    assert len(info["changed_paths_sha256"]) == 64


def test_doc_exists_names_every_scenario_and_has_no_open_markers():
    assert DOC.is_file(), "docs/full-workflow-acceptance.md is missing"
    text = DOC.read_text()
    ids = ["prerequisites"] + [s.id for s in mod.blocked_scenarios("x", "y")]
    missing = [i for i in ids if i not in text]
    assert not missing, missing
    for marker in ("TBD", "TODO"):  # AGENTS.md FP-DOC-4
        assert marker not in text
    for dependency in (mod.ENGINE_CASE_DEP, mod.ENGINE_VERIFY_DEP, mod.ENGINE_AI_DEP):
        assert dependency.split(":")[0] in text


# --------------------------------------------------------------------------
# Opt-in live acceptance run (the real workflow through the real CLI)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not LIVE, reason="opt-in: set RULOOM_FULL_WORKFLOW_ACCEPTANCE=1 to run"
)
def test_live_acceptance_report_is_sound(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY_SECRET)
    layout = os.environ.get("RULOOM_FULL_WORKFLOW_LAYOUT", "wheel")
    report = mod.run_acceptance(layout=layout, python=sys.executable, strict=STRICT)
    text = json.dumps(report)

    assert CANARY_SECRET not in text
    assert report["hygiene"]["credential_shaped_text_found"] is False
    assert report["schema"] == mod.SCHEMA
    assert report["limitations"]
    assert report["fixtures"]["gate_note"].startswith("ENGINEERING TEST")
    assert "not owner approval" in report["purpose"]
    assert report["revision"]["git_head"]
    assert report["hashes"]["fixture_csv_sha256"]
    assert report["hashes"]["harness_script_sha256"]
    if layout == "wheel":
        assert len(report["hashes"]["wheel_sha256"]) == 64

    ids = [s["id"] for s in report["scenarios"]]
    assert ids[0] == "prerequisites"
    assert set(ids[1:]) == {s.id for s in mod.blocked_scenarios("x", "y")}

    counts = {mod.PASS: 0, mod.FAIL: 0, mod.PENDING: 0}
    for scenario in report["scenarios"]:
        assert scenario["steps"], scenario["id"]
        assert scenario["status"] == mod.combine(
            [s["status"] for s in scenario["steps"]]
        )
        for step in scenario["steps"]:
            assert step["status"] in counts
            counts[step["status"]] += 1
            if step["status"] == mod.PENDING:
                assert step["dependency"], step["id"]
            if step["status"] == mod.PASS:
                assert step["dependency"] is None
    assert counts == report["summary"]["step_counts"]
    expected = mod.combine([s["status"] for s in report["scenarios"]])
    if report["stability"]["stable"]:
        assert report["summary"]["overall"] == expected
    else:
        assert report["summary"]["overall"] == mod.INCONCLUSIVE
        assert report["summary"]["observed_overall"] == expected
    assert {s["label"] for s in report["stability"]["snapshots"]} >= {"start", "end"}
    assert report["summary"]["verdict_kind"]
    assert set(report["strict"]["required_scenarios"]) == set(mod.REQUIRED_SCENARIO_IDS)

    if STRICT:
        assert report["strict"]["passed"], json.dumps(
            report["strict"]["reasons"], indent=1
        )
        assert report["summary"]["overall"] == mod.PASS
