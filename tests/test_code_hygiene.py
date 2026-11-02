"""Q-CODE-HYGIENE checker: ruff lint and format stay clean (GP-03 G7).

Executed by the aggregate gate (``scripts/force_pass.py``) as
``pytest -q tests/test_code_hygiene.py`` and registered as enforced in
``docs/quality/rules.json``. The enforced commands are exactly the ones
CI runs (``.github/workflows/ci.yml``): ``ruff check .`` and
``ruff format --check .`` against the pinned ``[tool.ruff]`` contract
in ``pyproject.toml``.

No skip-as-pass: when no ruff binary is importable the tests fail
closed with remediation instructions. CI provides ruff (pinned
``ruff==0.17.3`` installed beside the dev dependencies); a local venv
gains it via ``pip install ruff==0.17.3`` (or ``uv pip install`` into
the venv) -- see ``docs/quality/README.md``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUFF_VERSION = "0.17.3"
TIMEOUT_S = 300


def _ruff() -> str:
    """Resolve the ruff binary, failing closed (never skipping) when absent."""
    sibling = Path(sys.executable).parent / "ruff"
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("ruff")
    if found:
        return found
    pytest.fail(
        "ruff binary not found: install it with "
        f"`python -m pip install ruff=={RUFF_VERSION}` "
        "(CI installs the same pin beside the dev dependencies; "
        "a local venv gains it via `uv pip install "
        f"ruff=={RUFF_VERSION}`). Skipping is not allowed: "
        "Q-CODE-HYGIENE must execute, never pass vacuously.",
        pytrace=False,
    )
    raise AssertionError("unreachable")  # for type checkers


def _run_ruff(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_ruff(), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        timeout=TIMEOUT_S,
    )


def test_ruff_check_clean() -> None:
    """The whole tree passes the pinned `ruff check .` contract."""
    proc = _run_ruff("check", ".")
    assert proc.returncode == 0, f"ruff check . failed:\n{proc.stdout}\n{proc.stderr}"


def test_ruff_format_clean() -> None:
    """The whole tree passes `ruff format --check .` (no reflow drift)."""
    proc = _run_ruff("format", "--check", ".")
    assert proc.returncode == 0, (
        f"ruff format --check . failed:\n{proc.stdout}\n{proc.stderr}"
    )


def test_planted_violation_detected(tmp_path: Path) -> None:
    """Violation fixture: the same checker flags a planted dirty file.

    Proves the two tests above are not vacuous -- an unused import plus
    unsorted imports fail `ruff check`, and unformatted layout fails
    `ruff format --check`, under the repo's own pinned config.
    """
    dirty = tmp_path / "dirty.py"
    dirty.write_text(
        "import sys\nimport os\n\nimport json\n\n\ndef f():\n    return ( 1 )\n",
        encoding="utf-8",
    )
    config = str(ROOT / "pyproject.toml")
    check = _run_ruff("check", "--config", config, "--no-cache", str(dirty))
    assert check.returncode != 0, (
        "ruff check passed a planted file with unused/unsorted imports"
    )
    assert "F401" in check.stdout or "I001" in check.stdout, (
        f"expected an F401/I001 finding, got:\n{check.stdout}"
    )
    fmt = _run_ruff("format", "--check", "--config", config, "--no-cache", str(dirty))
    assert fmt.returncode != 0, "ruff format --check passed a planted unformatted file"
