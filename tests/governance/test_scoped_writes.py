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
phase subjects appearing in ``HEAD`` history in order). It never lists
branch refs, so it passes identically in a full clone and in a
single-ref CI checkout, which has no local phase-branch refs. Subjects
are matched instead of SHAs because the rebase-only PR flow rewrites
commit SHAs on merge (the DP-01 branch tips landed on main with new
SHAs, tree-identical), while subject lines survive the rebase; a
SHA-pinned check could never verify a phase merged through a PR.
History checks scope to ``HEAD`` for the same reason: ``git log --all``
narrows to whatever refs the clone fetched, while ``HEAD`` is the
shipped product (every phase branch merges fast-forward-only, so
``HEAD`` contains them).

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

#: Ordered phase subjects, oldest first. Each entry is a subject-line
#: fragment matched by substring against ``HEAD`` history; a future
#: phase extends this table with its own fragment. Subjects (not SHAs)
#: are matched because the rebase-only PR flow rewrites commit SHAs on
#: merge while subject lines survive, so each fragment must be stable
#: across a rebase and unique within history.
PHASE_SUBJECTS_IN_ORDER = (
    "RP-00: audit rule registry, counterexample fixtures, CI skeleton",
    "RP-01: private engine authority and required-check contracts",
    "RP-02: unique persistent runs, immutable attempts, durable terminal",
    "RP-03: frozen reviewed release with manifest-driven membership",
    "RP-04 rework: CC0 only on explicit recorded output-terms grant",
    "RP-05: HyLogger depth/missingness/label/joint-sequence quality",
    "RP-06: schematic shared scene/image registration",
    "RP-07: versioned GenerationSpec, bounded plan/repair, evidence report",
    "RP-08 rework: registry paragraph for strict planner wire schema",
    "RP-09: durable dispatch with leases/fencing plus separate publication",
    "CP-01: dispatch/publication trust repairs",
    "CP-02: depth geometry fidelity + TIR sequence repair",
    "CP-03: atomic download + expiry retry budget",
    "CP-04: executable aggregate gate",
    "CP-05: governance section-9 + restricted model invocation",
    "CP-06: ref-free scoped-writes topology for single-ref CI",
    "DP-01: request-to-run binding + snapshot publish",
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


def _ordered_subjects_problems(
    entries_newest_first: list[tuple[str, str]],
    subjects_oldest_first: list[str],
) -> list[str]:
    """Check ordered phase subjects appear in HEAD history in order.

    Each expected fragment must occur as a substring of some HEAD-history
    subject, and the first occurrences must sit at strictly increasing
    oldest-first positions. SHA-free by construction, so a rebase-only PR
    merge (which rewrites SHAs but preserves subjects) keeps the topology
    intact. Returns a list of problem descriptions; empty means the
    phased topology holds. Pure function over log entries so miniature
    repositories can exercise the same code path.
    """
    oldest_first = [subject for _, subject in reversed(entries_newest_first)]
    problems: list[str] = []
    previous: int | None = None
    for fragment in subjects_oldest_first:
        position = next(
            (index for index, subject in enumerate(oldest_first) if fragment in subject),
            None,
        )
        if position is None:
            problems.append(f"phase subject {fragment!r} not in HEAD history")
            continue
        # Oldest-first positions: walking oldest marker to newest marker
        # must ascend for the sequence to hold.
        if previous is not None and not previous < position:
            problems.append(f"phase subject {fragment!r} out of order")
        else:
            previous = position
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


def test_phase_subject_topology_present() -> None:
    if not _has_git(REPO_ROOT):
        pytest.skip("clean archive ships no git history")
    # Ref-free and SHA-free by construction: only HEAD-history subjects
    # are read, so a single-ref CI checkout (no local phase-branch refs)
    # passes exactly like a full clone, and a rebase-only PR merge (new
    # SHAs, same subjects) keeps the topology intact.
    problems = _ordered_subjects_problems(
        _head_log(REPO_ROOT), list(PHASE_SUBJECTS_IN_ORDER)
    )
    assert not problems, f"phased-subject topology broken: {problems[:5]}"


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
    assert _ordered_subjects_problems(entries, list(subjects)) == []
    # No phase-branch refs exist in this single-branch repository, so a
    # ref-listing check (the pre-CP-06 approach) would report zero here.
    proc = _git(repo, "branch", "-a", "--format=%(refname:short)")
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.splitlines()) <= 1
    # The same predicate rejects wrong content: a missing subject, a
    # reworded fragment matching nothing, and an out-of-order sequence
    # are each reported. A rewritten SHA with the same subject still
    # passes, which is exactly the rebase-only PR merge shape.
    assert _ordered_subjects_problems(entries, ["No such phase"]) != []
    assert _ordered_subjects_problems(entries, ["Phase A: different words"]) != []
    assert _ordered_subjects_problems(entries, list(reversed(subjects))) != []
    rewritten = [(sha, subject) for sha, subject in entries]
    rewritten[0] = ("0" * 40, rewritten[0][1])
    assert _ordered_subjects_problems(rewritten, list(subjects)) == []


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
