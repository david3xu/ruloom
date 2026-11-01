"""Ruloom executable force-pass gate.

Runs the numbered FP-DOC / FP-CODE rules against the repository tree and
prints one PASS/FAIL line per rule with evidence. Exits 0 only when every
rule passes; any FAIL blocks merge and sends rework.

Usage (from the repo root)::

    .venv/bin/python scripts/force_pass.py

Standard library only. Reviewers must score every rule PASS or FAIL with
evidence in the verdict; this script is the mechanical enforcer (see
AGENTS.md).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PRODUCT_DIRS = ("engine", "models", "profiles")
HYLOGGER_DIR = Path("profiles/hylogger")
TESTS_DIR = Path("tests")

# profiles/hylogger files allowed to import the models.base contract.
ALLOWED_MODELS_BASE = {
    "profiles/hylogger/plan.py",
    "profiles/hylogger/repair.py",
}
# The single composition root allowed to import models.meta_cli directly.
COMPOSITION_ROOT = "engine/cli.py"
# engine/hashing.py owns the sha256 helpers; nothing else may define one.
HASH_OWNER = "engine/hashing.py"

UNMEASURED_RE = re.compile(r"\b(?:TBD|TODO)\b")


def _py_files(root: Path):
    for dirname in PRODUCT_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def check_doc1(root: Path) -> tuple[bool, str]:
    """FP-DOC-1: every path named in the README Layout table exists."""
    text = (root / "README.md").read_text(encoding="utf-8")
    section = text.split("## Layout", 1)[1] if "## Layout" in text else ""
    paths: list[str] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 2:
            continue
        match = re.search(r"`([^`]+)`", cells[1])
        if match:
            paths.append(match.group(1))
    if not paths:
        return False, "no Layout table paths found in README.md"
    missing = [p for p in paths if not (root / p).exists()]
    if missing:
        return False, "missing paths: " + ", ".join(missing)
    shown = ", ".join(paths)
    return True, f"Layout paths exist ({len(paths)}/{len(paths)}: {shown})"


def _feature_modules(root: Path) -> list[str]:
    hylogger = root / HYLOGGER_DIR
    if not hylogger.is_dir():
        return []
    return sorted(
        p.stem for p in hylogger.glob("*.py") if p.name != "__init__.py"
    )


def _docs_corpus(root: Path) -> str:
    parts = [(root / "README.md").read_text(encoding="utf-8")]
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.glob("*.md")):
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def check_doc2(root: Path) -> tuple[bool, str]:
    """FP-DOC-2: every profiles/hylogger feature module is named in docs."""
    modules = _feature_modules(root)
    if not modules:
        return False, "no profiles/hylogger feature modules found"
    corpus = _docs_corpus(root)
    missing = [m for m in modules if m not in corpus]
    if missing:
        return False, "modules not named in README.md or docs/: " + ", ".join(missing)
    return True, f"all {len(modules)} feature modules named in docs"


def check_doc4(root: Path) -> tuple[bool, str]:
    """FP-DOC-4: no TBD/TODO phrasing in README.md or docs/*.md."""
    files = [root / "README.md"]
    docs = root / "docs"
    if docs.is_dir():
        files.extend(sorted(docs.glob("*.md")))
    hits: list[str] = []
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in UNMEASURED_RE.finditer(line):
                hits.append(f"{path.name}:{lineno}:{match.group(0)}")
    if hits:
        return False, "unmeasured-claim phrasing: " + ", ".join(hits)
    return True, f"no TBD/TODO in {len(files)} doc files"


def _import_violations(rel: str, tree: ast.AST) -> list[str]:
    """Apply FP-CODE-2 import boundaries to one parsed module."""
    top0 = rel.split("/")[0]
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                top = name.split(".")[0]
                violations.extend(
                    _classify(rel, node.lineno, top0, top, name, None, top)
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue  # relative intra-package import (from . import x)
            if node.level and node.module:
                continue  # relative intra-package import (from .x import y)
            mod = node.module or ""
            top = mod.split(".")[0]
            imported = [a.name for a in node.names]
            violations.extend(
                _classify(rel, node.lineno, top0, top, mod, imported, top)
            )
    return violations


def _classify(
    rel: str,
    lineno: int,
    top0: str,
    top: str,
    mod: str,
    imported: list[str] | None,
    _top: str,
) -> list[str]:
    label = f"{rel}:{lineno}"
    if top0 == "profiles":
        if top == "engine":
            return [f"{label}: profiles imports engine ({mod})"]
        if top == "models":
            base_only = mod == "models.base" or mod.startswith("models.base.")
            if base_only and rel in ALLOWED_MODELS_BASE:
                return []
            return [f"{label}: profiles imports {mod} (only models.base in plan.py/repair.py)"]
        return []
    if top0 == "models":
        if top in ("profiles", "engine"):
            return [f"{label}: models imports {top} ({mod})"]
        return []
    if top0 == "engine":
        meta = (
            mod == "models.meta_cli"
            or mod.startswith("models.meta_cli.")
            or (mod == "models" and any((i == "meta_cli" or i.startswith("meta_cli.")) for i in (imported or [])))
        )
        if meta and rel != COMPOSITION_ROOT:
            return [f"{label}: engine imports models.meta_cli outside {COMPOSITION_ROOT}"]
        return []
    return []


def check_code2(root: Path) -> tuple[bool, str]:
    """FP-CODE-2: engine/profile/model layer import boundaries (AST)."""
    violations: list[str] = []
    scanned = 0
    for path in _py_files(root):
        rel = _rel(path, root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            violations.append(f"{rel}: unparseable ({exc})")
            continue
        scanned += 1
        violations.extend(_import_violations(rel, tree))
    if not scanned:
        return False, "no product files found under engine/ models/ profiles/"
    if violations:
        return False, "; ".join(violations)
    return True, f"import boundaries hold ({scanned} files scanned)"


def check_code3(root: Path) -> tuple[bool, str]:
    """FP-CODE-3: sha256 helpers live only in engine/hashing.py.

    The gate fails any module outside engine/hashing.py that *defines* its
    own sha256 helper (``def *sha256*``). Plain one-line stdlib digest calls
    at a use site are uses, not helper definitions, so they do not fail;
    new helpers must reuse engine/hashing.py instead.
    """
    offenders: list[str] = []
    scanned = 0
    for path in _py_files(root):
        rel = _rel(path, root)
        if rel == HASH_OWNER:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "sha256" in node.name:
                    offenders.append(f"{rel}:{node.lineno}: def {node.name}")
    if offenders:
        return False, "sha256 helper defined outside engine/hashing.py: " + "; ".join(offenders)
    return True, f"no sha256 helper outside engine/hashing.py ({scanned} files scanned)"


def check_code4(root: Path) -> tuple[bool, str]:
    """FP-CODE-4: every feature module has a matching tests/test_<feature>.py."""
    modules = _feature_modules(root)
    if not modules:
        return False, "no profiles/hylogger feature modules found"
    missing = [
        m for m in modules if not (root / TESTS_DIR / f"test_{m}.py").is_file()
    ]
    if missing:
        return False, "missing mirror tests: " + ", ".join(f"tests/test_{m}.py" for m in missing)
    return True, f"all {len(modules)} feature modules have mirror tests"


def _tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line]


def _gitignore_has_data_rule(root: Path) -> bool:
    ignore_file = root / ".gitignore"
    if not ignore_file.is_file():
        return False
    for line in ignore_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        core = stripped.lstrip("/").rstrip("/")
        if core.endswith("*"):
            core = core.rstrip("*").rstrip("/")
        if core == "data":
            return True
    return False


def check_code5(root: Path, tracked: list[str] | None = None) -> tuple[bool, str]:
    """FP-CODE-5: no data/ path tracked; .gitignore carries a data/ rule.

    ``tracked`` injects the file list (tests simulate a tracked data path
    without touching git); None reads the real ``git ls-files`` output.
    """
    files = list(tracked) if tracked is not None else _tracked_files(root)
    data_paths = [f for f in files if f == "data" or f.startswith("data/")]
    has_rule = _gitignore_has_data_rule(root)
    problems: list[str] = []
    if data_paths:
        problems.append("tracked data/ paths: " + ", ".join(sorted(data_paths)[:5]))
    if not has_rule:
        problems.append("no data/ rule in .gitignore")
    if problems:
        return False, "; ".join(problems)
    return True, f"no data/ path tracked ({len(files)} files); .gitignore data/ rule present"


CHECKS = (
    ("FP-DOC-1", check_doc1),
    ("FP-DOC-2", check_doc2),
    ("FP-DOC-4", check_doc4),
    ("FP-CODE-2", check_code2),
    ("FP-CODE-3", check_code3),
    ("FP-CODE-4", check_code4),
    ("FP-CODE-5", check_code5),
)


def run_all(
    root: Path, tracked: list[str] | None = None
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    for rule_id, func in CHECKS:
        if rule_id == "FP-CODE-5":
            ok, evidence = func(root, tracked)
        else:
            ok, evidence = func(root)
        results.append((rule_id, ok, evidence))
    return results


def main(argv: list[str] | None = None) -> int:
    root = REPO_ROOT
    results = run_all(root)
    failed = 0
    for rule_id, ok, evidence in results:
        print(f"{rule_id}: {'PASS' if ok else 'FAIL'} — {evidence}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
