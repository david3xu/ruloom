"""Q-GOV-SCOPED-WRITES: branch-only, never stage data or run output.

The product half of scoped writes is mechanical git archaeology: no
``data/`` or ``runs/`` path is tracked or was ever committed, the
generated roots are git-ignored, the history stays a phased-branch
sequence, and no new merge commit lands on top of the one grandfathered
Loop-3 merge. The other half -- branch-only writes, never pushing, and
lease-gated edits -- is enforced by the Runtime coordinator and the
front-door ``--ff-only`` merge discipline, not by product code; the
registry entry records that split explicitly.

Tests needing history skip in a clean archive (``git archive`` ships no
``.git`` directory); the working-tree checks still run there.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Merge commits grandfathered into history before the phased-branch
#: discipline. The gate fails on any merge commit outside this set, so
#: a non-fast-forward merge can never land silently.
GRANDFATHERED_MERGES = {"6c65cf6d8ec920c7fbc85407155995ec7e6ad109"}

#: Refs proving the phased-branch topology (rp/cp/loop work branches).
PHASE_BRANCH_RE = re.compile(r"^(?:remotes/origin/)?(?:rp|cp|loop)\d+/")


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
    proc = _git(
        REPO_ROOT,
        "log",
        "--all",
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
    proc = _git(REPO_ROOT, "branch", "-a", "--format=%(refname:short)")
    assert proc.returncode == 0, proc.stderr
    phased = [
        line for line in proc.stdout.splitlines() if PHASE_BRANCH_RE.match(line)
    ]
    assert len(phased) >= 5, (
        f"expected phased work branches (rpNN/..., cpNN/..., loopN/...), "
        f"found {len(phased)}"
    )


def test_no_new_merge_commits() -> None:
    """Fast-forward-only merges: history gains no new merge commits."""
    if not _has_git(REPO_ROOT):
        pytest.skip("clean archive ships no git history")
    proc = _git(REPO_ROOT, "log", "--all", "--merges", "--format=%H %s")
    assert proc.returncode == 0, proc.stderr
    merges = [
        line.split()[0] for line in proc.stdout.splitlines() if line.strip()
    ]
    unknown = [sha for sha in merges if sha not in GRANDFATHERED_MERGES]
    assert not unknown, f"new merge commits in history: {unknown}"
