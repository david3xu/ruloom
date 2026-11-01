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
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REGISTRY_REL = Path("docs/quality/rules.json")


def load_registry(root: Path) -> dict | None:
    """Load the versioned rule registry (single source of rule metadata).

    Returns the parsed payload, or None when the file is missing or
    invalid. Callers that only enrich evidence fall back to the
    pre-registry behaviour; the aggregate gate treats a missing or
    invalid registry as a failure.
    """
    path = root / REGISTRY_REL
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def registry_layout_kinds(root: Path) -> dict[str, str] | None:
    """Map README Layout path -> 'tracked' | 'generated', or None."""
    payload = load_registry(root)
    if payload is None:
        return None
    layout = payload.get("layout")
    if not isinstance(layout, list):
        return None
    kinds: dict[str, str] = {}
    for entry in layout:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str):
            kinds[path] = entry.get("kind", "tracked")
    return kinds


def registry_q_rules(root: Path) -> list[dict] | None:
    """Q-group stub entries (R01-R12) in registry order, or None."""
    payload = load_registry(root)
    if payload is None:
        return None
    rules = payload.get("rules")
    if not isinstance(rules, list):
        return None
    return [
        rule
        for rule in rules
        if isinstance(rule, dict) and str(rule.get("id", "")).startswith("Q-")
    ]

PRODUCT_DIRS = ("engine", "models", "profiles")
PROFILES_DIR = Path("profiles")
TESTS_DIR = Path("tests")
ENGINE_DIR = Path("engine")

#: File names a profile root may hold directly (manifest/wiring only).
#: Every feature module lives one level down in intake/, analysis/,
#: generation/, or validation/.
PROFILE_ROOT_ALLOWED = ("__init__.py", "capabilities.py")

#: Words no engine module may use outside comments (the engine knows no
#: domain). Listed here so the rule can name them; FP-CODE-6 forbids them
#: in engine/ code, strings, and docstrings alike.
DOMAIN_WORDS = (
    "depth",
    "mineral",
    "swir",
    "tir",
    "TSG",
    "Markov",
    "hole",
    "hylogger",
)
DOMAIN_RE = re.compile(
    r"\b(?:depth|mineral|swir|tir|tsg|markov|hole|hylogger)\b",
    re.IGNORECASE,
)

def _allowed_models_base(rel: str) -> bool:
    """Generation wiring may import the models.base contract only.

    ``profiles/<profile>/generation/plan.py`` and
    ``profiles/<profile>/generation/repair.py`` are the two files that
    build model prompts from statistics; nothing else under profiles/
    may import any of ``models``.
    """
    parts = rel.split("/")
    return (
        len(parts) == 4
        and parts[0] == "profiles"
        and parts[2] == "generation"
        and parts[3] in ("plan.py", "repair.py")
    )


# The single composition root allowed to import models.meta_cli directly.
COMPOSITION_ROOT = "engine/cli/__init__.py"
# engine/artifacts/hashing.py owns the sha256 helpers; nothing else defines one.
HASH_OWNER = "engine/artifacts/hashing.py"

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


def _gitignore_entries(root: Path) -> list[str]:
    """Raw non-comment patterns from .gitignore (prefix match fallback)."""
    ignore_file = root / ".gitignore"
    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Narrow fallback: single top-level names only.
        if "/" in stripped.strip("/"):
            continue
        core = stripped.lstrip("/").rstrip("/")
        if core.endswith("*"):
            core = core.rstrip("*").rstrip("/")
        if core:
            entries.append(core)
    return entries


def _is_ignored(root: Path, rel: str) -> bool:
    """True when a Layout path is covered by .gitignore (generated output).

    Directory Layout paths (``runs/``) are probed both as written and
    via a synthetic child file: bare ``git check-ignore -- runs``
    does not match a directory-only ``runs/`` pattern when the
    directory is absent from disk (a fresh clone/CI checkout), while
    ``runs/`` itself and ``runs/<child>`` do match.
    """
    candidates = [rel]
    if rel.endswith("/"):
        candidates.append(rel + ".force-pass-ignore-probe")
    for candidate in candidates:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", candidate],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True
        if proc.returncode not in (0, 1):
            break
    else:
        # Every probe ran and none matched: not ignored.
        return False
    # No git metadata (clean archive): fall back to prefix matching.
    top = rel.strip("/").split("/")[0]
    return top in _gitignore_entries(root)


def check_doc1(root: Path) -> tuple[bool, str]:
    """FP-DOC-1: tracked Layout paths exist; generated ones are ignored.

    Kinds come from the registry layout table (``tracked`` vs
    ``generated``). Without a registry every path is treated as tracked,
    which is the pre-registry behaviour. ``runs/`` is generated
    runtime output: the gate requires it to be git-ignored and never
    requires it to exist, so a fresh clone without ``runs/`` passes.
    """
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
    kinds = registry_layout_kinds(root)
    missing = [
        p
        for p in paths
        if (kinds is None or kinds.get(p, "tracked") == "tracked")
        and not (root / p).exists()
    ]
    if missing:
        return False, "missing paths: " + ", ".join(missing)
    generated = [p for p in paths if kinds is not None and kinds.get(p) == "generated"]
    unignored = [p for p in generated if not _is_ignored(root, p)]
    if unignored:
        return False, "generated paths must be git-ignored: " + ", ".join(unignored)
    shown = ", ".join(paths)
    if generated:
        return (
            True,
            f"Layout paths ok ({len(paths) - len(generated)} tracked exist; "
            f"generated ignored, existence not required: {', '.join(generated)}; "
            f"all: {shown})",
        )
    return True, f"Layout paths exist ({len(paths)}/{len(paths)}: {shown})"


def _profile_ids(root: Path) -> list[str]:
    base = root / PROFILES_DIR
    if not base.is_dir():
        return []
    return sorted(
        p.name
        for p in base.iterdir()
        if p.is_dir()
        and not p.name.startswith(("_", "."))
        and (p / "profile.json").is_file()
    )


def _feature_modules(root: Path) -> dict[str, list[str]]:
    """Feature stems per profile: every .py below the profile root."""
    out: dict[str, list[str]] = {}
    for profile_id in _profile_ids(root):
        base = root / PROFILES_DIR / profile_id
        out[profile_id] = sorted(
            p.stem
            for p in base.rglob("*.py")
            if "__pycache__" not in p.parts and p.name != "__init__.py"
        )
    return out


def _docs_corpus(root: Path) -> str:
    parts = [(root / "README.md").read_text(encoding="utf-8")]
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.glob("*.md")):
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def check_doc2(root: Path) -> tuple[bool, str]:
    """FP-DOC-2: every profile feature module is named in docs."""
    by_profile = _feature_modules(root)
    if not by_profile:
        return False, "no profile feature modules found"
    corpus = _docs_corpus(root)
    missing = [
        f"{profile}/{stem}"
        for profile, stems in by_profile.items()
        for stem in stems
        if stem not in corpus
    ]
    if missing:
        return False, "modules not named in README.md or docs/: " + ", ".join(missing)
    total = sum(len(stems) for stems in by_profile.values())
    return True, f"all {total} feature modules named in docs"


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
            if base_only and _allowed_models_base(rel):
                return []
            return [f"{label}: profiles imports {mod} (only models.base in generation/plan.py|repair.py)"]
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
    """FP-CODE-3: sha256 helpers live only in engine/artifacts/hashing.py.

    The gate fails any module outside engine/artifacts/hashing.py that
    *defines* its own sha256 helper (``def *sha256*``). Plain one-line
    stdlib digest calls at a use site are uses, not helper definitions,
    so they do not fail; new helpers must reuse engine/artifacts/hashing.py
    instead.
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
        return False, "sha256 helper defined outside engine/artifacts/hashing.py: " + "; ".join(offenders)
    return True, f"no sha256 helper outside engine/artifacts/hashing.py ({scanned} files scanned)"


def check_code4(root: Path) -> tuple[bool, str]:
    """FP-CODE-4: every feature module mirrors to tests/profiles/<id>/test_<feature>.py."""
    by_profile = _feature_modules(root)
    if not by_profile:
        return False, "no profile feature modules found"
    missing = [
        f"tests/profiles/{profile}/test_{stem}.py"
        for profile, stems in by_profile.items()
        for stem in stems
        if not (root / TESTS_DIR / "profiles" / profile / f"test_{stem}.py").is_file()
    ]
    if missing:
        return False, "missing mirror tests: " + ", ".join(missing)
    total = sum(len(stems) for stems in by_profile.values())
    return True, f"all {total} feature modules have mirror tests"


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
    Without git metadata (a clean archive has no ``.git`` directory) the
    check falls back to scanning the working tree for ``data/`` paths.
    """
    via = "git ls-files"
    if tracked is not None:
        files = list(tracked)
        via = "injected list"
    else:
        try:
            files = _tracked_files(root)
        except RuntimeError:
            data_top = root / "data"
            files = []
            if data_top.exists():
                files = ["data/ present in working tree (no git metadata to consult)"]
            via = "working-tree scan (no git metadata)"
    data_paths = [f for f in files if f == "data" or f.startswith("data/")]
    has_rule = _gitignore_has_data_rule(root)
    problems: list[str] = []
    if data_paths:
        problems.append("tracked data/ paths: " + ", ".join(sorted(data_paths)[:5]))
    if not has_rule:
        problems.append("no data/ rule in .gitignore")
    if problems:
        return False, "; ".join(problems)
    return True, f"no data/ path tracked ({len(files)} files via {via}); .gitignore data/ rule present"


def check_code6(root: Path) -> tuple[bool, str]:
    """FP-CODE-6: engine/ uses no domain words outside comments.

    Comments are stripped with :mod:`tokenize` (so ``#`` inside strings is
    safe) and the remaining code, strings, and docstrings must not contain
    any of :data:`DOMAIN_WORDS`. A comment may name the rule itself; all
    other comments are ignored as well.
    """
    offenders: list[str] = []
    scanned = 0
    base = root / ENGINE_DIR
    if not base.is_dir():
        return False, "no engine/ directory found"
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = _rel(path, root)
        scanned += 1
        try:
            readline = io.StringIO(path.read_text(encoding="utf-8")).readline
            tokens = tokenize.generate_tokens(readline)
            for token in tokens:
                if token.type == tokenize.COMMENT:
                    continue
                for match in DOMAIN_RE.finditer(token.string):
                    offenders.append(
                        f"{rel}:{token.start[0]}: {match.group(0)!r}"
                    )
        except (tokenize.TokenError, SyntaxError, UnicodeDecodeError) as exc:
            offenders.append(f"{rel}: unscannable ({exc})")
    if not scanned:
        return False, "no engine modules found"
    if offenders:
        return False, "domain words in engine/: " + "; ".join(offenders)
    return True, f"no domain words in engine/ ({scanned} files scanned)"


def _manifest_files(root: Path) -> list[Path]:
    base = root / "profiles"
    if not base.is_dir():
        return []
    return sorted(
        p
        for p in base.rglob("profile.json")
        if "__pycache__" not in p.parts
    )


def _manifest_payloads(root: Path) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for path in _manifest_files(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            out.append((_rel(path, root), payload))
    return out


def check_code9(root: Path) -> tuple[bool, str]:
    """FP-CODE-9: open-weight requirements never ride on a hosted adapter.

    Any role binding declaring ``requires.open_weight`` in any profile
    manifest must bind the ``glimmer_local`` adapter. A hosted
    (``meta_cli``) adapter on such a role fails the gate.
    """
    payloads = _manifest_payloads(root)
    if not payloads:
        return False, "no profile manifests (profiles/*/profile.json) found"
    problems: list[str] = []
    checked = 0
    for rel, payload in payloads:
        models = payload.get("models", {})
        roles = models.get("roles", {}) if isinstance(models, dict) else {}
        if not isinstance(roles, dict):
            continue
        for role, binding in roles.items():
            if not isinstance(binding, dict):
                continue
            requires = binding.get("requires", {})
            if (
                isinstance(requires, dict)
                and requires.get("open_weight") is True
            ):
                checked += 1
                adapter = binding.get("adapter", "meta_cli")
                if adapter != "glimmer_local":
                    problems.append(
                        f"{rel}: role {role!r} requires open_weight but "
                        f"binds adapter {adapter!r}"
                    )
    if problems:
        return False, "; ".join(problems)
    return (
        True,
        f"open_weight bindings all local "
        f"({checked} open_weight roles in {len(payloads)} manifests)",
    )


def check_code10(root: Path) -> tuple[bool, str]:
    """FP-CODE-10: budgets declared; the reporter seat stays disabled.

    Every profile manifest declares
    ``models.budget.max_tokens_per_run``, and the hylogger manifest
    contains a disabled ``reporter`` role (the seat exists but can never
    resolve, so no reporter capability can be wired to it).
    """
    payloads = _manifest_payloads(root)
    if not payloads:
        return False, "no profile manifests (profiles/*/profile.json) found"
    problems: list[str] = []
    hylogger_seen = False
    for rel, payload in payloads:
        models = payload.get("models", {})
        budget = models.get("budget", {}) if isinstance(models, dict) else {}
        ceiling = budget.get("max_tokens_per_run") if isinstance(budget, dict) else None
        if (
            not isinstance(ceiling, int)
            or isinstance(ceiling, bool)
            or ceiling < 1
        ):
            problems.append(
                f"{rel}: missing models.budget.max_tokens_per_run"
            )
        if payload.get("id") == "hylogger":
            hylogger_seen = True
            roles = models.get("roles", {}) if isinstance(models, dict) else {}
            reporter = roles.get("reporter", {}) if isinstance(roles, dict) else {}
            if (
                not isinstance(reporter, dict)
                or reporter.get("enabled", True) is not False
            ):
                problems.append(
                    f"{rel}: missing disabled reporter role"
                )
    if not hylogger_seen:
        problems.append("no hylogger manifest found")
    if problems:
        return False, "; ".join(problems)
    return (
        True,
        f"budgets declared and reporter disabled "
        f"({len(payloads)} manifests)",
    )


def check_code7(root: Path) -> tuple[bool, str]:
    """FP-CODE-7: manifests use only step/loop/gate; loops bind max_attempts.

    Every profile manifest graph node must declare exactly one of the
    three node kinds, and every loop node must bind ``max_attempts`` to the
    caller-supplied value (the literal binding, never a default). Engine
    functions must also take ``max_attempts`` with no default.
    """
    problems: list[str] = []
    manifests = _manifest_files(root)
    if not manifests:
        return False, "no profile manifests (profiles/*/profile.json) found"
    for path in manifests:
        rel = _rel(path, root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{rel}: invalid JSON ({exc})")
            continue
        graph = payload.get("graph")
        if not isinstance(graph, list) or not graph:
            problems.append(f"{rel}: 'graph' must be a non-empty list")
            continue
        for index, node in enumerate(graph):
            where = f"{rel} node {index}"
            kinds = (
                [k for k in node if k in ("step", "loop", "gate")]
                if isinstance(node, dict)
                else []
            )
            if not isinstance(node, dict) or len(kinds) != 1:
                problems.append(
                    f"{where}: must declare exactly one of step/loop/gate"
                )
                continue
            if kinds[0] == "loop" and node.get("max_attempts") != "max_attempts":
                problems.append(
                    f"{where}: loop must bind 'max_attempts' with no default"
                )
    base = root / ENGINE_DIR
    scanned = 0
    for path in sorted(base.rglob("*.py")) if base.is_dir() else []:
        if "__pycache__" in path.parts:
            continue
        rel = _rel(path, root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"{rel}: unparseable ({exc})")
            continue
        scanned += 1
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            args = node.args
            pos = list(args.args)
            pos_defaults = list(args.defaults)
            offset = len(pos) - len(pos_defaults)
            for position, arg in enumerate(pos):
                if arg.arg == "max_attempts" and position >= offset:
                    problems.append(
                        f"{rel}:{node.lineno}: max_attempts has a default"
                    )
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if arg.arg == "max_attempts" and default is not None:
                    problems.append(
                        f"{rel}:{node.lineno}: max_attempts has a default"
                    )
    if problems:
        return False, "; ".join(problems)
    return (
        True,
        f"graphs use step/loop/gate with bound loops "
        f"({len(manifests)} manifests, {scanned} engine files scanned)",
    )


def check_code8(root: Path) -> tuple[bool, str]:
    """FP-CODE-8: feature modules live in subpackages, never flat.

    No feature ``.py`` module sits directly under ``engine/`` (only
    ``__init__.py``) or directly under ``profiles/<profile>/`` (only
    ``__init__.py`` and ``capabilities.py`` as manifest/wiring). Engine
    features live in ``core/``, ``artifacts/``, ``audit/``, ``cli/``;
    profile features live in ``intake/``, ``analysis/``, ``generation/``,
    ``validation/``.
    """
    offenders: list[str] = []
    base = root / ENGINE_DIR
    if not base.is_dir():
        return False, "no engine/ directory found"
    for path in sorted(base.glob("*.py")):
        if path.name != "__init__.py":
            offenders.append(_rel(path, root))
    profiles = root / PROFILES_DIR
    if not profiles.is_dir():
        return False, "no profiles/ directory found"
    for profile_dir in sorted(p for p in profiles.iterdir() if p.is_dir()):
        if not (profile_dir / "profile.json").is_file():
            continue
        for path in sorted(profile_dir.glob("*.py")):
            if path.name not in PROFILE_ROOT_ALLOWED:
                offenders.append(_rel(path, root))
    if offenders:
        return False, "flat feature modules (move into a feature subpackage): " + ", ".join(offenders)
    return True, "no flat feature modules under engine/ or profile roots"


CHECKS = (
    ("FP-DOC-1", check_doc1),
    ("FP-DOC-2", check_doc2),
    ("FP-DOC-4", check_doc4),
    ("FP-CODE-2", check_code2),
    ("FP-CODE-3", check_code3),
    ("FP-CODE-4", check_code4),
    ("FP-CODE-5", check_code5),
    ("FP-CODE-6", check_code6),
    ("FP-CODE-7", check_code7),
    ("FP-CODE-8", check_code8),
    ("FP-CODE-9", check_code9),
    ("FP-CODE-10", check_code10),
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
    payload = load_registry(root)
    if payload is None:
        print(f"REGISTRY {REGISTRY_REL}: FAIL — missing or invalid registry")
        return 1
    version = payload.get("version")
    q_rules = registry_q_rules(root) or []
    counts: dict[str, int] = {}
    for rule in q_rules:
        status = str(rule.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    enforced = sum(
        1 for rule in payload.get("rules", []) if rule.get("status") == "enforced"
    )
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    print(
        f"REGISTRY {REGISTRY_REL} v{version}: "
        f"{enforced} enforced incl. FP rules; Q stubs: {summary or 'none'}"
    )
    for rule in q_rules:
        status = str(rule.get("status", "unknown")).upper()
        audit_ref = rule.get("audit_ref")
        ref = f" (audit {audit_ref})" if audit_ref else ""
        checker = rule.get("checker", "?")
        print(f"{rule.get('id')}: {status}{ref} — checker {checker}")
        if status not in ("ENFORCED", "OPEN_FINDING", "PLANNED"):
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
