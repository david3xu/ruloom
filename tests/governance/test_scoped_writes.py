"""Q-GOV-SCOPED-WRITES: branch-only, never stage data or run output.

The product half of scoped writes is mechanical git archaeology: no
``data/`` or ``runs/`` path is tracked or was ever committed, the
generated roots are git-ignored, the history stays a phased sequence,
and no new merge commit lands on top of the one grandfathered
Loop-3 merge. The other half -- branch-only writes, never pushing, and
lease-gated edits -- is enforced by the Runtime coordinator and the
front-door ``--ff-only`` merge discipline, not by product code; the
registry entry records that split explicitly.

The topology check reads only history reachable from ``HEAD`` (ordered
phase-commit tips as ancestors of ``HEAD``). It never lists branch
refs, so it passes identically in a full clone and in a single-ref CI
checkout, which has no local phase-branch refs. History checks scope
to ``HEAD`` for the same reason: ``git log --all`` narrows to whatever
refs the clone fetched, while ``HEAD`` is the shipped product (every
phase branch merges fast-forward-only, so ``HEAD`` contains them).

Tests needing history skip in a clean archive (``git archive`` ships no
``.git`` directory); the working-tree checks still run there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Merge commits grandfathered into history before the phased-branch
#: discipline. The gate fails on any merge commit outside this set, so
#: a non-fast-forward merge can never land silently.
GRANDFATHERED_MERGES = {"6c65cf6d8ec920c7fbc85407155995ec7e6ad109"}

#: Ordered phased-branch tips, oldest first. Each entry pins the full
#: SHA of a phase branch tip plus a subject fragment. A future phase
#: extends this table with its own tip; history rewrites are forbidden
#: by this same rule, so pinned SHAs stay valid.
PHASE_TIPS_IN_ORDER = (
    (
        "d4e7e154e210ed2a68f124ac71d85df68bf2ff1a",
        "RP-00: audit rule registry, counterexample fixtures, CI skeleton",
    ),
    (
        "1c4d155d642e10a70b3f064c07c70176ff70e8f0",
        "RP-01: private engine authority and required-check contracts",
    ),
    (
        "6ce06351589ceaead5f013305d700869fcf24694",
        "RP-02: unique persistent runs, immutable attempts, durable terminal",
    ),
    (
        "432f95ce6e863d1666267daa38f084f1de2c2fce",
        "RP-03: frozen reviewed release with manifest-driven membership",
    ),
    (
        "316caa6d6f8138f712837512f7df7f9e021c1be4",
        "RP-04 rework: CC0 only on explicit recorded output-terms grant",
    ),
    (
        "99b23b22509c40ce2b711a2c2fe5abfccfd03dd3",
        "RP-05: HyLogger depth/missingness/label/joint-sequence quality",
    ),
    (
        "fe9efbf77d32ad165ca643135cca42f7219ee8b9",
        "RP-06: schematic shared scene/image registration",
    ),
    (
        "f1ed40db9fef306634a6581e75904d5bcce48beb",
        "RP-07: versioned GenerationSpec, bounded plan/repair, evidence report",
    ),
    (
        "0be6039c86a94cb74424cf6859896935dc3d5b04",
        "RP-08 rework: registry paragraph for strict planner wire schema",
    ),
    (
        "fe5524123a1006a96fe3663de427c069d8c844c5",
        "RP-09: durable dispatch with leases/fencing plus separate publication",
    ),
    (
        "4195b9089cb61bed8f2b977027a63914b5b258f0",
        "CP-01: dispatch/publication trust repairs",
    ),
    (
        "37140c448e3ca103de7f54ace277a745f2452840",
        "CP-02: depth geometry fidelity + TIR sequence repair",
    ),
    (
        "85d24d60f93264bb2f28b94cc3761aa95c56df79",
        "CP-03: atomic download + expiry retry budget",
    ),
    (
        "342151dd59a45bb2e2fc622c138381a1ffe4556f",
        "CP-04: executable aggregate gate",
    ),
    (
        "a405d2db2a2bc4c54ab2c63dbe1ba0792d36a3ac",
        "CP-05: governance section-9 + restricted model invocation",
    ),
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _has_git(repo: Path) -> bool:
    return (repo / ".git").is_dir() and _git(repo, "rev-parse").returncode == 0


def _gitignore_text(repo: Path) -> str:
    return (repo / ".gitignore").read_text(encoding="utf-8")


def _gitignore_has_rule(repo: Path, name: str) -> bool:
    for line in _gitignore_text(repo).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.strip("/") == name:
            return True
    return False


def _head_log(repo: Path) -> list[tuple[str, str]]:
    """History reachable from HEAD, newest first, as (sha, subject)."""
    proc = _git(repo, "log", "HEAD", "--format=%H%x1f%s")
    assert proc.returncode == 0, proc.stderr
    entries: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        sha, _, subject = line.partition("\x1f")
        if sha:
            entries.append((sha, subject))
    return entries


def _ordered_markers_problems(
    entries_newest_first: list[tuple[str, str]],
    markers_oldest_first: list[tuple[str, str]],
) -> list[str]:
    """Check pinned phase tips appear as ordered HEAD ancestors.

    Returns a list of problem descriptions; empty means the phased
    topology holds. Pure function over log entries so miniature
    repositories can exercise the same code path.
    """
    position = {sha: index for index, (sha, _) in enumerate(entries_newest_first)}
    subjects = dict(entries_newest_first)
    problems: list[str] = []
    previous: int | None = None
    for sha, fragment in markers_oldest_first:
        if sha not in position:
            problems.append(f"phase commit {sha[:12]} ({fragment}) not an ancestor of HEAD")
            continue
        if fragment not in subjects[sha]:
            problems.append(
                f"phase commit {sha[:12]} subject {subjects[sha]!r} "
                f"does not contain {fragment!r}"
            )
        # Newest-first log: an older marker sits deeper (larger index),
        # so walking oldest marker to newest marker must descend.
        if previous is not None and not position[sha] < previous:
            problems.append(f"phase commit {sha[:12]} ({fragment}) out of order")
        previous = position[sha]
    return problems


def test_gitignore_declares_generated_roots() -> None:
    assert _gitignore_has_rule(REPO_ROOT, "data"), "no data/ rule in .gitignore"
    assert _gitignore_has_rule(REPO_ROOT, "runs"), "no runs/ rule in .gitignore"


def test_no_tracked_data_or_run_output() -> None:
    if _has_git(REPO_ROOT):
        proc = _git(REPO_ROOT, "ls-files")
        assert proc.returncode == 0, proc.stderr
        tracked = proc.stdout.splitlines()
        offenders = [
            path
            for path in tracked
            if path == "data"
            or path.startswith(("data/", "runs/"))
            or path == "runs"
        ]
        assert not offenders, f"tracked generated paths: {offenders[:5]}"
    else:
        # Clean archive: no git metadata to consult, so the ignore rules
        # are the policy evidence (checked above) and any generated
        # directory present must be untracked output, never source.
        assert _gitignore_has_rule(REPO_ROOT, "data")
        assert _gitignore_has_rule(REPO_ROOT, "runs")


def test_history_never_added_data_or_run_output() -> None:
    if not _has_git(REPO_ROOT):
        pytest.skip("clean archive ships no git history")
    # Scoped to HEAD, not --all: a single-ref clone fetches only the
    # checked-out ref, so --all would narrow to it anyway; HEAD is the
    # shipped product and contains every ff-only phase branch.
    proc = _git(
        REPO_ROOT,
        "log",
        "HEAD",
        "--diff-filter=A",
        "--name-only",
        "--pretty=format:",
    )
    assert proc.returncode == 0, proc.stderr
    added = {
        line
        for line in proc.stdout.splitlines()
        if line == "data"
        or line.startswith(("data/", "runs/"))
        or line == "runs"
    }
    assert not added, f"history committed generated paths: {sorted(added)[:5]}"


def test_phase_branch_topology_present() -> None:
    if not _has_git(REPO_ROOT):
        pytest.skip("clean archive ships no git history")
    # Ref-free by construction: only HEAD history is read, so a
    # single-ref CI checkout (no local phase-branch refs) passes exactly
    # like a full clone.
    problems = _ordered_markers_problems(_head_log(REPO_ROOT), list(PHASE_TIPS_IN_ORDER))
    assert not problems, f"phased-commit topology broken: {problems[:5]}"


def test_phase_topology_single_ref_shape(tmp_path: Path) -> None:
    """The topology check needs no branch refs (CI single-ref shape).

    Builds a miniature repository with phased-like history on one
    branch only, then exercises the same order predicate the real
    topology test uses. The old ref-listing approach finds nothing
    phased here, proving it cannot work ref-free.
    """
    repo = tmp_path / "mini"
    repo.mkdir()
    assert _git(repo, "init").returncode == 0
    assert _git(repo, "config", "user.email", "test@example.com").returncode == 0
    assert _git(repo, "config", "user.name", "Test").returncode == 0
    subjects = ("Phase A: foundation", "Phase B: rework", "Phase C: gate")
    for index, subject in enumerate(subjects):
        (repo / f"file{index}.txt").write_text(f"{index}\n", encoding="utf-8")
        assert _git(repo, "add", ".").returncode == 0
        assert _git(repo, "commit", "-m", subject).returncode == 0
    entries = _head_log(repo)
    assert len(entries) == 3
    shas_oldest_first = [sha for sha, _ in reversed(entries)]
    markers = list(zip(shas_oldest_first, subjects, strict=True))
    assert _ordered_markers_problems(entries, markers) == []
    # No phase-branch refs exist in this single-branch repository, so a
    # ref-listing check (the pre-CP-06 approach) would report zero here.
    proc = _git(repo, "branch", "-a", "--format=%(refname:short)")
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.splitlines()) <= 1
    # The same predicate rejects wrong content: missing, reworded, and
    # out-of-order markers are each reported.
    assert _ordered_markers_problems(entries, [("0" * 40, "Phase A")]) != []
    assert (
        _ordered_markers_problems(entries, [(markers[0][0], "different words")]) != []
    )
    assert _ordered_markers_problems(entries, list(reversed(markers))) != []


def test_no_new_merge_commits() -> None:
    """Fast-forward-only merges: history gains no new merge commits."""
    if not _has_git(REPO_ROOT):
        pytest.skip("clean archive ships no git history")
    # Scoped to HEAD, not --all: side-branch merges that never shipped
    # are not the product; HEAD is.
    proc = _git(REPO_ROOT, "log", "HEAD", "--merges", "--format=%H %s")
    assert proc.returncode == 0, proc.stderr
    merges = [
        line.split()[0] for line in proc.stdout.splitlines() if line.strip()
    ]
    unknown = [sha for sha in merges if sha not in GRANDFATHERED_MERGES]
    assert not unknown, f"new merge commits in history: {unknown}"
