#!/usr/bin/env python3
"""Opt-in full-workflow acceptance harness for the HyLogger product workflow.

This harness drives the real Ruloom command line (``prepare``, ``run``,
the human gate and the recipient-side verifier) from an installed layout
and reports what the workflow actually did. It is an engineering test,
not an owner approval and not a benchmark:

* model responses are scripted mocks labelled ``ENGINEERING-TEST-MOCK``;
* the source is an invented fixture generated here, never real data;
* validation, quality decisions and the acceptance gate are NOT mocked --
  the installed engine and profile code decide;
* automated gate input carries a note stating it is an engineering test.

Every step ends as ``pass``, ``fail`` or ``pending``. A step whose
interface does not exist yet is ``pending`` and names its dependency; it
never passes. The overall result is ``pass`` only when every step passes.

Opt-in: nothing runs unless ``--run`` is given or
``RULOOM_FULL_WORKFLOW_ACCEPTANCE=1`` is set. The harness makes no model,
paid or network call (the driver subprocess also disables sockets), reads
no credentials, and logs no environment values.

Exit codes: 0 pass, 1 fail, 2 usage, 3 pending only, 4 not opted in.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import dataclasses
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if sys.path[:1] != [str(ROOT)]:
    sys.path.insert(0, str(ROOT))

from engine.artifacts.hashing import sha256_bytes, sha256_canonical  # noqa: E402

SCHEMA = "ruloom.full-workflow-acceptance/1"
OPT_IN_ENV = "RULOOM_FULL_WORKFLOW_ACCEPTANCE"
TIMEOUT_S = 300
MARKER = "RULOOM_ACCEPTANCE_RESULT="

PASS = "pass"
FAIL = "fail"
PENDING = "pending"
#: Report-level only: the tree moved during the run, so no verdict is valid.
INCONCLUSIVE = "inconclusive"

#: Who is at fault for a failing step. A product fault is an assertion about
#: engine or profile behaviour that did not hold; a harness fault is a setup,
#: driver or programming error in this harness (the product is not judged).
FAULT_PRODUCT = "product"
FAULT_HARNESS = "harness"

STRICT_ENV = "RULOOM_FULL_WORKFLOW_ACCEPTANCE_STRICT"
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PENDING = 3
EXIT_NOT_OPTED_IN = 4
EXIT_INCONCLUSIVE = 5
EXIT_HARNESS_ERROR = 7

MOCK_LABEL = "ENGINEERING-TEST-MOCK"
ENGINEERING_NOTE = (
    "ENGINEERING TEST AUTOMATED INPUT: not owner approval and not a human "
    "review of the data."
)
FIXTURE_LABEL = (
    "INVENTED ENGINEERING TEST FIXTURE: not real source data; no real "
    "licence, origin or scientific meaning."
)
CANARY_PREFIX = "CANARY-ROW-"
CASE_ID = "hylogger_scalar_teaching_v1"
CASE_VERSION = 1
CASE_REF = f"{CASE_ID}@{CASE_VERSION}"
CASE_AI_STAGES = ("case_research", "case_source_assess", "case_candidate_review")
SEED = 8
N_ROWS = 800
SMALL_ROWS = 30
#: Below the case's minimum satisfiable row count, so a count of this size is
#: infeasible however the source looks (the harness never tunes the case).
INFEASIBLE_ROWS = 10
STRATEGY = "fitted_markov_balanced"
JOINT = "fitted_markov_joint"

#: FIXED, DISCLOSED CONTROL SEEDS. The harness never searches for a seed and
#: never changes one to obtain a pass: a plan that fails the real case checks
#: stays a failure. Each control is labelled in the report under
#: ``fixtures.controls``.
#:
#: KNOWN-GOOD release-positive control: the invented fixture with this plan
#: passes every real case check and releases. Provenance, stated plainly: this
#: seed was first observed to pass in exploratory runs (see the preserved
#: reports) and is pinned here; it is not derived by the harness. If it ever
#: stops passing, the root step fails; nothing replaces it automatically.
KNOWN_GOOD_CASE_SEED = 9
#: KNOWN quality-failure control: the same fixture with this seed fails
#: ``case.swir_answer_rate`` (observed 0.0279 against a 0.02 limit). It proves
#: a real quality failure blocks the gate and any release.
KNOWN_QUALITY_FAILURE_SEED = 8
#: Repair control: with the joint strategy this seed fails
#: ``case.inclusion_fraction`` on the first attempt, so the repair stage runs.
#: If it stops failing, the controls that need it are pending, never pass.
REPAIR_CONTROL_SEED = 1
CONTROLS = {
    "known_good_release_positive": {
        "label": "KNOWN-GOOD CONTROL: pinned fixture and plan expected to pass and release",
        "seed": KNOWN_GOOD_CASE_SEED,
        "n_rows": N_ROWS,
        "strategy": STRATEGY,
    },
    "known_quality_failure": {
        "label": "KNOWN QUALITY-FAILURE CONTROL: pinned plan expected to fail a real case check",
        "seed": KNOWN_QUALITY_FAILURE_SEED,
        "n_rows": N_ROWS,
        "strategy": STRATEGY,
    },
    "repair_control": {
        "label": "REPAIR CONTROL: pinned joint-strategy plan expected to fail case.inclusion_fraction first",
        "seed": REPAIR_CONTROL_SEED,
        "n_rows": N_ROWS,
        "strategy": JOINT,
    },
    "search": "none: the harness performs no seed search and tunes no threshold",
}
REQUEST_TEXT = (
    "Create 800 practice rows for a data-checking lesson that keep the "
    "source inclusion fraction."
)
ALLOWED_ENV_NAMES = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
)


# --------------------------------------------------------------------------
# Results model
# --------------------------------------------------------------------------


class StepFailure(AssertionError):
    """A step assertion about the product did not hold (a product fault)."""


class HarnessError(Exception):
    """The harness, driver or its setup is broken (a harness fault).

    The product is not judged by these: the step reports ``fail`` with
    ``fault=harness`` so it is never mistaken for a product defect, and
    it still blocks a pass.
    """


class StepPending(Exception):
    """A step cannot run yet; carries the exact missing dependency."""

    def __init__(self, dependency: str, detail: str = "") -> None:
        super().__init__(detail or dependency)
        self.dependency = dependency
        self.detail = detail or dependency


def require(condition: object, message: str) -> None:
    """Fail the current step unless ``condition`` is truthy."""
    if not condition:
        raise StepFailure(message)


def harness_require(condition: object, message: str) -> None:
    """Report a harness fault (not a product fault) unless ``condition``."""
    if not condition:
        raise HarnessError(message)


@dataclasses.dataclass
class Step:
    id: str
    title: str
    status: str
    detail: str
    dependency: str | None = None
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    fault: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Scenario:
    id: str
    title: str
    steps: list[Step] = dataclasses.field(default_factory=list)

    @property
    def status(self) -> str:
        return combine([step.status for step in self.steps])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
        }


def combine(statuses: Sequence[str]) -> str:
    """Worst-wins aggregation. Empty or unknown input is never ``pass``."""
    if not statuses:
        return PENDING
    if any(s == FAIL for s in statuses):
        return FAIL
    if any(s not in (PASS, PENDING) for s in statuses):
        return FAIL
    if any(s == PENDING for s in statuses):
        return PENDING
    return PASS


# --------------------------------------------------------------------------
# Redaction and environment
# --------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{6,}"), "Bearer [REDACTED]"),
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
        "[REDACTED-JWT]",
    ),
    (
        re.compile(r"\b(?:sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{12,}"),
        "[REDACTED-KEY]",
    ),
    (
        re.compile(
            r"(?i)\b((?:[A-Z0-9]+_)*(?:API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|SECRET|TOKEN"
            r"|PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION)(?:_[A-Z0-9]+)*|[A-Z0-9]+_KEY"
            r"|KEY)\b\s*[=:]\s*[^\s\"',;]+"
        ),
        r"\1=[REDACTED]",
    ),
)


def redact(text: str, *, limit: int = 1200, hide: Sequence[Path] = ()) -> str:
    """Remove credential-shaped text and local paths; keep the tail."""
    for path in hide:
        text = text.replace(str(path), "<work>")
    try:
        text = text.replace(str(Path.home()), "~")
    except RuntimeError:
        pass
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text


def contains_secret(text: str) -> bool:
    """True when credential-shaped text survives in ``text``.

    A match that already carries a ``[REDACTED`` marker is a redaction, not
    a leak; a raw secret never contains that marker.
    """
    for pattern, _replacement in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if "[REDACTED" not in match.group(0):
                return True
    return False


def child_env(
    root: Path, home: Path, parent: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Allowlisted environment for the driver; credentials are never inherited."""
    source = os.environ if parent is None else parent
    env = {name: source[name] for name in ALLOWED_ENV_NAMES if name in source}
    env.update(
        HOME=str(home),
        PYTHONPATH=str(root),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED="0",
    )
    return env


def env_policy(parent: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Describe (names only, never values) what the driver inherits."""
    source = os.environ if parent is None else parent
    return {
        "mode": "allowlist",
        "inherited_names": [n for n in ALLOWED_ENV_NAMES if n in source],
        "set_names": [
            "HOME",
            "PYTHONPATH",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONHASHSEED",
        ],
        "values_logged": False,
        "note": "Credentials, tokens and RULOOM_* overrides are not inherited.",
    }


def file_digest(path: Path) -> str:
    """SHA-256 of a file via the engine's shared hashing helper."""
    return sha256_bytes(Path(path).read_bytes())


# --------------------------------------------------------------------------
# Revision
# --------------------------------------------------------------------------


def _git_bytes(*args: str) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git(*args: str) -> str | None:
    raw = _git_bytes(*args)
    return None if raw is None else raw.decode("utf-8", "replace").strip()


def tree_snapshot(label: str = "snapshot") -> dict[str, Any]:
    """Commit plus a CONTENT digest of every changed or untracked path.

    Unlike a digest of ``git status`` lines, this changes when a file is
    edited again after it was already modified, so a worker saving a file
    mid-run is detected. Ignored paths (``runs/``, caches) are excluded.
    """
    head = _git("rev-parse", "HEAD") or "unavailable"
    raw = _git_bytes("status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries: list[list[str]] = []
    if raw is not None:
        parts = raw.split(b"\0")
        index = 0
        while index < len(parts):
            item = parts[index]
            index += 1
            if len(item) < 4:
                continue
            code = item[:2].decode("ascii", "replace")
            path = item[3:].decode("utf-8", "replace")
            if "R" in code or "C" in code:
                index += 1  # the rename/copy source path follows
            target = ROOT / path
            try:
                digest = (
                    sha256_bytes(target.read_bytes()) if target.is_file() else "absent"
                )
            except OSError:
                digest = "unreadable"
            entries.append([code, path, digest])
    entries.sort()
    return {
        "label": label,
        "available": raw is not None and head != "unavailable",
        "git_head": head,
        "tree_digest": sha256_canonical(entries),
        "changed_path_count": len(entries),
        "_entries": entries,
    }


def public_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The snapshot as reported (no per-file digests)."""
    return {k: v for k, v in snapshot.items() if not k.startswith("_")}


def compare_snapshots(snapshots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Stable only if every snapshot matches the first in commit AND tree."""
    first = snapshots[0]
    base = {tuple(e) for e in first.get("_entries", [])}
    moved: list[str] = []
    changed_paths: set[str] = set()
    for snap in snapshots[1:]:
        if (snap["git_head"], snap["tree_digest"]) != (
            first["git_head"],
            first["tree_digest"],
        ):
            moved.append(str(snap["label"]))
            now = {tuple(e) for e in snap.get("_entries", [])}
            changed_paths |= {entry[1] for entry in base ^ now}
    unavailable = [str(s["label"]) for s in snapshots if not s.get("available")]
    return {
        "stable": not moved and not unavailable,
        "commit_changed": any(s["git_head"] != first["git_head"] for s in snapshots),
        "tree_changed": any(
            s["tree_digest"] != first["tree_digest"] for s in snapshots
        ),
        "moved_at": moved,
        "git_unavailable_at": unavailable,
        "paths_that_moved": sorted(changed_paths)[:50],
        "snapshots": [public_snapshot(s) for s in snapshots],
    }


def revision_info(snap: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Commit, branch and content digest of the working tree being tested."""
    snap = snap if snap is not None else tree_snapshot("revision")
    entries = snap["_entries"]
    return {
        **public_snapshot(snap),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unavailable",
        "working_tree_clean": (not entries) if snap["available"] else None,
        "changed_paths_sha256": sha256_canonical([[e[0], e[1]] for e in entries]),
        "changed_paths": [f"{e[0]} {e[1]}" for e in entries][:300],
        "note": "The tested layout is built from the working tree, including "
        "uncommitted changes listed here; tree_digest covers their contents.",
    }


# --------------------------------------------------------------------------
# Invented fixtures
# --------------------------------------------------------------------------


def fixture_csv_text(*, rows: int = 2000, single_state: bool = False) -> str:
    """Deterministic INVENTED scalar table (not derived from any real source).

    Labels are drawn from the installed vocabulary only so that vocabulary
    checks apply; the assignment is a seeded pseudo-random pattern with no
    geological meaning. ``Invented_Row_Id`` carries a canary that must never
    reach a model prompt or a released file.
    """
    rng = random.Random(20261104)
    labels = ["CARBONATE", "CHLORITE", "KAOLIN"]
    lines = ["StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST,Invented_Row_Id"]
    valid = True
    swir = tir = None
    for i in range(rows):
        if not single_state:
            stay = 0.95 if valid else 0.85
            if rng.random() >= stay:
                valid = not valid
        if valid:
            if swir is None or rng.random() > 0.9:
                swir = rng.choice(labels)
            if tir is None or rng.random() > 0.9:
                tir = rng.choice(labels)
            swir_cell = swir if rng.random() < 0.6 else ""
            tir_cell = tir if rng.random() < 0.99 else ""
        else:
            swir_cell = tir_cell = ""
        lines.append(
            f"{120.0 + i * 0.006:.6f},{1 if valid else 0},{swir_cell},"
            f"{tir_cell},{CANARY_PREFIX}{i:05d}"
        )
    return "\n".join(lines) + "\n"


@dataclasses.dataclass
class Fixture:
    directory: Path
    csv: Path
    sidecar: Path
    csv_sha256: str
    sidecar_sha256: str
    unsuitable_csv: Path
    unsuitable_sha256: str


def _write_fixture(directory: Path, name: str, text: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    csv = directory / f"{name}-scalars.csv"
    sidecar = directory / f"{name}-source.json"
    csv.write_text(text, encoding="utf-8")
    sidecar.write_text(
        json.dumps(
            {
                "dataset_name": name,
                "licence": "CC-BY-4.0",
                "attribution": FIXTURE_LABEL,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return csv, sidecar


def build_fixture(work: Path) -> Fixture:
    """Write the labelled invented fixtures under ``work/producer``."""
    base = work / "producer"
    csv, sidecar = _write_fixture(base / "main", "INVENTED-hole", fixture_csv_text())
    bad_csv, _ = _write_fixture(
        base / "unsuitable",
        "INVENTED-single-state-hole",
        fixture_csv_text(rows=200, single_state=True),
    )
    return Fixture(
        directory=base,
        csv=csv,
        sidecar=sidecar,
        csv_sha256=file_digest(csv),
        sidecar_sha256=file_digest(sidecar),
        unsuitable_csv=bad_csv,
        unsuitable_sha256=file_digest(bad_csv),
    )


# --------------------------------------------------------------------------
# Layout: installed wheel (default) or source tree
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Install:
    kind: str
    root: Path
    wheel_name: str | None = None
    wheel_sha256: str | None = None
    console_script: str | None = None


class InstallError(Exception):
    """The requested layout could not be prepared."""


BUILD_SNIPPET = (
    "import sys\n"
    "from hatchling.build import build_wheel\n"
    "print('WHEEL=' + build_wheel(sys.argv[1]))\n"
)


def build_install(work: Path, layout: str, python: str | None = None) -> Install:
    """Build and extract a real wheel, or use the source tree explicitly."""
    python = python or sys.executable
    if layout == "tree":
        return Install(kind="source-tree", root=ROOT)
    if layout != "wheel":
        raise InstallError(f"unknown layout {layout!r}")
    dist = work / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    home = work / "home"
    home.mkdir(exist_ok=True)
    try:
        proc = subprocess.run(
            [python, "-c", BUILD_SNIPPET, str(dist)],
            cwd=ROOT,
            env=child_env(ROOT, home),
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(
            f"wheel build could not start: {type(exc).__name__}"
        ) from exc
    if proc.returncode != 0:
        tail = redact(proc.stderr or proc.stdout, limit=500, hide=[work])
        raise InstallError(
            "the wheel build failed (the hatchling backend must be importable "
            f"in the chosen interpreter): {tail}"
        )
    name = next(
        (ln[6:] for ln in proc.stdout.splitlines() if ln.startswith("WHEEL=")), ""
    )
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != name:
        raise InstallError(f"expected exactly one built wheel, got {wheels}")
    wheel = wheels[0]
    target = work / "install"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        stray = [
            n
            for n in names
            if n.startswith(("data/", "runs/", "tests/")) or "/runs/" in n
        ]
        if stray:
            raise InstallError(f"wheel ships out-of-package trees: {stray[:5]}")
        if not any(n.endswith(".dist-info/RECORD") for n in names):
            raise InstallError("wheel has no RECORD")
        entry = next((n for n in names if n.endswith("entry_points.txt")), None)
        if entry is None:
            raise InstallError("wheel declares no entry points")
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry).decode("utf-8"))
        archive.extractall(target)
    console = parser.get("console_scripts", "ruloom", fallback="")
    if console != "engine.cli:main":
        raise InstallError(f"unexpected ruloom entry point {console!r}")
    if not (target / "engine" / "cli" / "__init__.py").is_file():
        raise InstallError("extracted wheel lacks engine.cli")
    return Install(
        kind="wheel",
        root=target,
        wheel_name=wheel.name,
        wheel_sha256=file_digest(wheel),
        console_script=console,
    )


# --------------------------------------------------------------------------
# Driver subprocess (runs inside the installed layout, outside the repo)
# --------------------------------------------------------------------------

DRIVER_SOURCE = r'''
import contextlib
import hashlib
import importlib
import io
import json
import re
import socket
import sys
from pathlib import Path

JOB = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))


def _deny(*args, **kwargs):
    raise OSError("ENGINEERING TEST: network access is disabled")


socket.socket.connect = _deny
socket.socket.connect_ex = _deny
socket.create_connection = _deny
socket.getaddrinfo = _deny

import engine
import models
import profiles

OUT = {
    "locations": {
        "engine": str(Path(engine.__file__).resolve()),
        "models": str(Path(models.__file__).resolve()),
        "profiles": str(Path(profiles.__file__).resolve()),
    },
    "model_calls": 0,
    "canary_hits": 0,
    "prompt_sha256": [],
}
CANARY = JOB.get("canary")
LABEL = JOB.get("mock_label", "ENGINEERING-TEST-MOCK")


class Scripted:
    """Scripted mock model. It never validates or decides anything."""

    def __init__(self, spec):
        self.kind = spec.get("kind", "forbid")
        self.responses = list(spec.get("responses", []))

    def complete(self, prompt, schema):
        from models.base import Completion, Usage

        OUT["model_calls"] += 1
        OUT["prompt_sha256"].append(hashlib.sha256(prompt.encode()).hexdigest())
        if CANARY and CANARY in prompt:
            OUT["canary_hits"] += 1
        if self.kind != "scripted":
            raise RuntimeError("ENGINEERING TEST: a model call is forbidden here")
        if not self.responses:
            raise RuntimeError("ENGINEERING TEST: scripted mock is exhausted")
        return Completion(
            data=self.responses.pop(0),
            usage=Usage(model=LABEL, input_tokens=1, output_tokens=1, calls=1),
            meta={"model": LABEL, "mock": True},
        )


class RoleMock:
    """Role-aware mock. It builds handoffs from facts the real prompts state
    (case id, hashes, code verdict) so a valid handoff exists, and applies
    only the single-field mutations a negative control asks for. It validates
    and decides nothing: the profile and the engine do."""

    def __init__(self, spec):
        self.spec = spec
        self.role_calls = {}

    @staticmethod
    def role_of(schema):
        props = set((schema or {}).get("properties", {}))
        if "operator_recommendation" in props:
            return "review"
        if "fit_assessment" in props:
            return "assess"
        if "passage_citations" in props:
            return "research"
        if "unsupported" in props and "summary" in props:
            return "prepare"
        if "seed" in props and "n_rows" in props:
            return "plan"
        return "unknown"

    @staticmethod
    def _find(pattern, prompt, flags=0):
        found = re.search(pattern, prompt, flags)
        if found is None:
            raise RuntimeError(
                "ENGINEERING TEST: mock could not read " + repr(pattern) + " from the prompt"
            )
        return found

    def build(self, role, prompt):
        find = self._find
        if role == "prepare":
            return dict(self.spec["prepare"])
        if role == "plan":
            return dict(self.spec["plan"])
        if role == "research":
            case = find(r"Installed case: (\S+) \(version (\d+)\)", prompt)
            ids = re.findall(
                r"'([^']+)'", find(r"Admitted primary evidence IDs: \[(.*?)\]", prompt).group(1)
            )
            shas = dict(
                re.findall(r"^\[([^\]\n]+)\]: .*sha256:([0-9a-f]{64})", prompt, re.M)
            )
            return {
                "case_id": case.group(1),
                "case_version": int(case.group(2)),
                "cited_evidence_ids": ids,
                "passage_citations": [
                    {
                        "evidence_id": i,
                        "passage_sha256": shas.get(i, "0" * 64),
                        "relevance": "ENGINEERING-TEST-MOCK relevance note.",
                    }
                    for i in ids
                ],
                "supported_requirements": [
                    "exact requested row count",
                    "blank and answered classification label rates",
                ],
                "unsupported_requirements": [],
                "research_summary": "ENGINEERING-TEST-MOCK research note; not a real model.",
                "limitations": ["Scripted mock response."],
            }
        if role == "assess":
            ids = re.findall(
                r"'([^']+)'", find(r"Admitted evidence IDs: \[(.*?)\]", prompt).group(1)
            )
            return {
                "case_id": find(r"^Case: (\S+)", prompt, re.M).group(1),
                "source_sha256": find(r"Source SHA-256: ([0-9a-f]{64})", prompt).group(1),
                "cited_evidence_ids": ids[:1],
                "fit_assessment": "ENGINEERING-TEST-MOCK fit note; invented fixture.",
                "source_limitations": ["Invented engineering-test fixture."],
                "teaching_suitability": "ENGINEERING-TEST-MOCK suitability note.",
            }
        if role == "review":
            verdict = find(r"Code check outcome: (pass|fail)", prompt).group(1)
            # The reviewer contract asks for structured facts that quote exact
            # values from the verified measurement paths the prompt lists; the
            # profile re-checks each one against its own recomputation.
            marker = "Available verified numeric measurement paths:"
            at = prompt.find(marker)
            if at < 0:
                raise RuntimeError(
                    "ENGINEERING TEST: mock could not read the verified measurement paths from the prompt"
                )
            available, _end = json.JSONDecoder().raw_decode(
                prompt[at + len(marker) :].lstrip()
            )
            facts = [
                {"measurement": path, "value": value}
                for path, value in list(available.items())[:3]
            ]
            if not facts:
                raise RuntimeError(
                    "ENGINEERING TEST: mock could not read any verified measurement path from the prompt"
                )
            return {
                "case_id": find(r"^Case: (\S+)", prompt, re.M).group(1),
                "candidate_sha256": find(r"Candidate SHA-256: ([0-9a-f]{64})", prompt).group(1),
                "code_verdict_acknowledged": verdict,
                "operator_recommendation": (
                    "recommend_proceed_to_human_gate"
                    if verdict == "pass"
                    else "recommend_reject"
                ),
                "evidence_facts": facts,
                "evaluator_opinion": "ENGINEERING-TEST-MOCK opinion; advisory only.",
                "strengths": [],
                "concerns": [],
            }
        raise RuntimeError("ENGINEERING TEST: mock has no response for role " + role)

    def complete(self, prompt, schema):
        from models.base import Completion, Usage

        OUT["model_calls"] += 1
        OUT["prompt_sha256"].append(hashlib.sha256(prompt.encode()).hexdigest())
        if CANARY and CANARY in prompt:
            OUT["canary_hits"] += 1
        role = self.role_of(schema)
        touch = self.spec.get("touch")
        if touch and role == "prepare":
            # Test-only: change a throwaway source copy while the request model
            # is "interpreting", to prove a mid-preparation change is refused.
            with open(touch["path"], "a", encoding="utf-8") as handle:
                handle.write(touch["append"])
        self.role_calls[role] = self.role_calls.get(role, 0) + 1
        OUT["role_calls"] = dict(self.role_calls)
        data = self.build(role, prompt)
        data.update(self.spec.get("mutate", {}).get(role, {}))
        return Completion(
            data=data,
            usage=Usage(model=LABEL, input_tokens=1, output_tokens=1, calls=1),
            meta={"model": LABEL, "mock": True},
        )


def make_model(spec):
    return RoleMock(spec) if spec.get("kind") == "roles" else Scripted(spec)


def _tail(text, n=40000):
    return text[-n:]


mode = JOB["mode"]
out_buf, err_buf = io.StringIO(), io.StringIO()
try:
    if mode in ("cli", "help"):
        from engine.cli import main

        model = make_model(JOB.get("model", {}))
        sys.stdin = io.StringIO(JOB.get("stdin", ""))
        rc = None
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                rc = main(JOB["argv"], model=model)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
            except Exception as exc:
                rc = -1
                err_buf.write("driver exception: " + type(exc).__name__ + ": " + str(exc))
        OUT["rc"] = rc
    elif mode == "execute_override":
        # Real engine ``execute`` with ONE profile capability swapped for a
        # deliberately misbehaving one, to prove the engine's own guard (not
        # the profile's good behaviour) refuses it.
        from engine.cli import load_registry, resolve_role_bindings
        from engine.core.manifest import load_manifest
        from engine.core.runner import execute
        from engine.core.spec import load_spec_file

        model = make_model(JOB.get("model", {}))
        sys.stdin = io.StringIO(JOB.get("stdin", ""))
        manifest = load_manifest(profiles.manifest_path("hylogger"))
        registry = dict(load_registry("hylogger"))
        resolved, sources = resolve_role_bindings(manifest)

        def reroll_seed(ctx):
            plan = dict(ctx.plan())
            plan["seed"] = int(plan["seed"]) + 1
            ctx.set_plan(plan)

        if JOB.get("override") == "case_repair_reroll_seed":
            registry["case_repair"] = reroll_seed
        else:
            raise RuntimeError("unknown override " + repr(JOB.get("override")))
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code, outcome = execute(
                manifest,
                registry,
                source=Path(JOB["source"]),
                max_attempts=int(JOB["max_attempts"]),
                model_factory=lambda role, config: model,
                out=Path(JOB["out"]),
                role_bindings=resolved,
                role_binding_sources=sources,
                spec=load_spec_file(JOB["spec"]),
                fixed_plan=None,
                model_policy="hosted",
            )
        OUT["rc"] = code
        OUT["outcome"] = json.loads(json.dumps(outcome, default=str))
    elif mode == "verify_release":
        from engine.artifacts.consumer import ReleaseVerificationError, verify_release

        manifest = None
        if JOB.get("manifest"):
            manifest = json.loads(Path(JOB["manifest"]).read_text(encoding="utf-8"))
        try:
            OUT["verified"] = True
            OUT["verification"] = verify_release(JOB["zip"], manifest=manifest)
        except ReleaseVerificationError as exc:
            OUT["verified"] = False
            OUT["error_type"] = "ReleaseVerificationError"
            OUT["message"] = str(exc)[:400]
            OUT.pop("verification", None)
        except Exception as exc:
            OUT["verified"] = False
            OUT["error_type"] = type(exc).__name__
            OUT["message"] = str(exc)[:400]
            OUT.pop("verification", None)
    elif mode == "import_probe":
        imports = {}
        for name in (
            "engine.cli",
            "engine.cli.request",
            "engine.artifacts.consumer",
            "profiles.hylogger.capabilities",
        ):
            try:
                importlib.import_module(name)
                imports[name] = "ok"
            except Exception as exc:
                imports[name] = type(exc).__name__ + ": " + str(exc)[:200]
        OUT["imports"] = imports
        OUT["interpreter"] = sys.version.split()[0]
        try:
            wf = importlib.import_module("profiles.hylogger.validation.workflow")
            OUT["workflow_importable"] = True
            OUT["case_member_names"] = sorted(getattr(wf, "CASE_MEMBER_NAMES", []))
            OUT["case_check_ids"] = list(getattr(wf, "CASE_CHECK_IDS", []))
        except Exception as exc:
            OUT["workflow_importable"] = False
            OUT["error_type"] = type(exc).__name__
    else:
        OUT["driver_error"] = "unknown mode " + repr(mode)
except Exception as exc:
    OUT["driver_error"] = type(exc).__name__ + ": " + str(exc)[:400]
OUT["stdout"] = _tail(out_buf.getvalue())
OUT["stderr"] = _tail(err_buf.getvalue())
print("RULOOM_ACCEPTANCE_RESULT=" + json.dumps(OUT, sort_keys=True))
'''


@dataclasses.dataclass
class Invocation:
    process_rc: int
    result: dict[str, Any] | None
    stdout: str = ""
    stderr: str = ""
    elapsed_s: float = 0.0

    @property
    def rc(self) -> int | None:
        return None if self.result is None else self.result.get("rc")

    @property
    def calls(self) -> int:
        return int((self.result or {}).get("model_calls", -1))

    @property
    def canary_hits(self) -> int:
        return int((self.result or {}).get("canary_hits", -1))

    @property
    def role_calls(self) -> dict[str, int]:
        return dict((self.result or {}).get("role_calls", {}))

    @property
    def text(self) -> str:
        res = self.result or {}
        return f"{res.get('stdout', '')}\n{res.get('stderr', '')}"

    def require_driver_ok(self) -> dict[str, Any]:
        harness_require(self.result is not None, "driver produced no result marker")
        harness_require(
            "driver_error" not in (self.result or {}),
            f"driver error: {(self.result or {}).get('driver_error')}",
        )
        return self.result or {}


def parse_marker(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARKER):
            try:
                value = json.loads(line[len(MARKER) :])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


@dataclasses.dataclass
class Ctx:
    work: Path
    install: Install
    fixture: Fixture
    caps: dict[str, Any] = dataclasses.field(default_factory=dict)
    hashes: dict[str, str] = dataclasses.field(default_factory=dict)
    observations: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    cache: dict[str, Any] = dataclasses.field(default_factory=dict)
    model_calls_total: int = 0
    counter: int = 0
    python: str = dataclasses.field(default_factory=lambda: sys.executable)

    def fresh(self, prefix: str) -> Path:
        self.counter += 1
        return self.work / f"{prefix}-{self.counter:03d}"


def inside_layout(ctx: Ctx, location: str) -> bool:
    """True when ``location`` lies under the install root (symlinks resolved)."""
    return Path(location).resolve().is_relative_to(ctx.install.root.resolve())


def invoke(
    ctx: Ctx,
    job: Mapping[str, Any],
    *,
    timeout: int = TIMEOUT_S,
    root: Path | None = None,
) -> Invocation:
    """Run one driver job from the installed layout, cwd outside the repo."""
    ctx.counter += 1
    jobs = ctx.work / "jobs"
    jobs.mkdir(exist_ok=True)
    cwd = ctx.work / "cwd"
    cwd.mkdir(exist_ok=True)
    home = ctx.work / "home"
    home.mkdir(exist_ok=True)
    driver = ctx.work / "driver.py"
    if not driver.exists():
        driver.write_text(DRIVER_SOURCE, encoding="utf-8")
    job_path = jobs / f"job-{ctx.counter:03d}.json"
    payload = dict(job)
    payload.setdefault("canary", CANARY_PREFIX)
    payload.setdefault("mock_label", MOCK_LABEL)
    job_path.write_text(json.dumps(payload), encoding="utf-8")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [ctx.python, str(driver), str(job_path)],
            cwd=cwd,
            env=child_env(root or ctx.install.root, home),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Invocation(-1, None, "", "timeout", time.monotonic() - started)
    result = parse_marker(proc.stdout)
    inv = Invocation(
        proc.returncode,
        result,
        redact(proc.stdout, hide=[ctx.work]),
        redact(proc.stderr, hide=[ctx.work]),
        round(time.monotonic() - started, 3),
    )
    if result is not None:
        ctx.model_calls_total += max(inv.calls, 0)
    return inv


def cli(
    ctx: Ctx,
    argv: Sequence[str],
    *,
    stdin: str = "",
    responses: Sequence[Mapping[str, Any]] | None = None,
    roles: Mapping[str, Any] | None = None,
    mode: str = "cli",
) -> Invocation:
    """Run the real CLI; ``roles`` selects the role-aware mock, ``responses`` a
    plain scripted sequence, and neither forbids every model call."""
    model: dict[str, Any]
    if roles is not None:
        model = {"kind": "roles", **dict(roles)}
    elif responses is not None:
        model = {"kind": "scripted", "responses": [dict(r) for r in responses]}
    else:
        model = {"kind": "forbid"}
    return invoke(
        ctx, {"mode": mode, "argv": list(argv), "stdin": stdin, "model": model}
    )


def check_mock_ok(inv: Invocation) -> None:
    """A mock that could not read its prompt is a harness fault, not the product's."""
    harness_require(
        "ENGINEERING TEST: mock could not" not in inv.text,
        "the role mock could not read the facts it echoes from a prompt "
        "(prompt wording changed?): " + inv.text[-300:],
    )


def decision_input(decision: str, note: str = ENGINEERING_NOTE) -> str:
    return f"{decision}\n{note}\n"


def plan_response(
    n_rows: int = N_ROWS, seed: int = SEED, strategy: str = STRATEGY
) -> dict[str, Any]:
    return {"seed": seed, "n_rows": n_rows, "strategy": strategy}


def prepare_response(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "summary": f"{MOCK_LABEL}: scripted response, not a real model.",
        "unsupported": [],
        "n_rows": N_ROWS,
        "seed": SEED,
        "outputs": ["scalar"],
        "strategy": STRATEGY,
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# Run records
# --------------------------------------------------------------------------


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclasses.dataclass
class RunRecord:
    name: str
    inv: Invocation
    out: Path
    run_dir: Path | None
    zips: list[Path]
    draft: Path | None = None

    @property
    def zip_path(self) -> Path | None:
        return self.zips[0] if len(self.zips) == 1 else None

    def decision(self) -> dict[str, Any] | None:
        path = None if self.run_dir is None else self.run_dir / "decision.json"
        return load_json(path) if path is not None and path.is_file() else None

    def run_json(self) -> dict[str, Any] | None:
        path = None if self.run_dir is None else self.run_dir / "run.json"
        return load_json(path) if path is not None and path.is_file() else None

    def receipts(self) -> list[dict[str, Any]]:
        if self.run_dir is None:
            return []
        found = sorted((self.run_dir / "attempts").glob("attempt-*/receipt.json"))
        return [load_json(p) for p in found]

    def checks(self) -> list[list[dict[str, Any]]]:
        if self.run_dir is None:
            return []
        found = sorted((self.run_dir / "attempts").glob("attempt-*/checks.json"))
        return [load_json(p) for p in found]

    def events(self) -> list[dict[str, Any]]:
        path = None if self.run_dir is None else self.run_dir / "events.jsonl"
        if path is None or not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


PREPARE_ARGS_TAIL = (
    "--model-policy",
    "local-only",
    "--role-adapter",
    "planner=glimmer_local",
)


def product_draft(ctx: Ctx, rows: int, max_attempts: int = 2) -> Path:
    """The real product flow: ``prepare`` writes a draft; later runs use its spec."""

    def make() -> Path:
        draft = ctx.fresh(f"draft-run-{rows}-{max_attempts}")
        inv = cli(
            ctx,
            [
                "prepare",
                str(ctx.fixture.csv),
                "--profile",
                "hylogger",
                "--request",
                REQUEST_TEXT,
                "--max-attempts",
                str(max_attempts),
                "--out",
                str(draft),
                *PREPARE_ARGS_TAIL,
            ],
            responses=[prepare_response(n_rows=rows)],
        )
        inv.require_driver_ok()
        require(
            inv.rc == 0, f"prepare for the run failed ({inv.rc}): {inv.text[-300:]}"
        )
        require((draft / "spec.json").is_file(), "prepare wrote no spec.json")
        return draft

    return cached(ctx, f"draft-run-{rows}-{max_attempts}", make)


def execute_run(
    ctx: Ctx,
    name: str,
    *,
    stdin: str,
    responses: Sequence[Mapping[str, Any]] | None,
    source: Path | None = None,
    max_attempts: int = 2,
    extra: Sequence[str] = (),
    profile: str = "hylogger",
    rows: int | None = N_ROWS,
    roles: Mapping[str, Any] | None = None,
) -> RunRecord:
    out = ctx.fresh(f"out-{name}")
    spec_args: list[str] = []
    if rows is not None:
        spec_args = [
            "--spec",
            str(product_draft(ctx, rows, max_attempts) / "spec.json"),
        ]
    argv = [
        "run",
        str(source or ctx.fixture.csv),
        "--profile",
        profile,
        "--max-attempts",
        str(max_attempts),
        "--out",
        str(out),
        *spec_args,
        *extra,
    ]
    inv = cli(ctx, argv, stdin=stdin, responses=responses, roles=roles)
    return collect_run(name, inv, out)


def collect_run(name: str, inv: Invocation, out: Path) -> RunRecord:
    """Locate the single run directory and any release ZIPs under ``out``."""
    runs = out / "runs"
    run_dirs = (
        [p for p in runs.iterdir() if (p / "run.json").is_file()]
        if runs.is_dir()
        else []
    )
    zips = sorted(runs.rglob("release.zip")) if runs.is_dir() else []
    return RunRecord(name, inv, out, run_dirs[0] if len(run_dirs) == 1 else None, zips)


def cached(ctx: Ctx, key: str, factory: Callable[[], Any]) -> Any:
    """Memoise a prerequisite; a failure is replayed to every dependent."""
    if key not in ctx.cache:
        try:
            ctx.cache[key] = ("ok", factory())
        except Exception as exc:  # noqa: BLE001 -- replayed to dependents
            ctx.cache[key] = ("err", exc)
    kind, value = ctx.cache[key]
    if kind == "err":
        raise value
    return value


def required_check_ids(ctx: Ctx, *, case: bool = False) -> list[str]:
    manifest = load_json(ctx.install.root / "profiles" / "hylogger" / "profile.json")
    wanted = {None, "case_selected"} if case else {None}
    return [
        c["id"]
        for c in manifest["checks"]
        if c.get("required") and c.get("applicable_when") in wanted
    ]


def accept_run_raw(ctx: Ctx) -> RunRecord:
    def make() -> RunRecord:
        rec = execute_run(
            ctx,
            "accept",
            stdin=decision_input("accept"),
            responses=[plan_response(), plan_response()],
        )
        rec.inv.require_driver_ok()
        require(rec.inv.rc == 0, f"accept run exit {rec.inv.rc}: {rec.inv.text[-400:]}")
        require(
            len(rec.zips) == 1, f"accepted run left {len(rec.zips)} release.zip files"
        )
        return rec

    return cached(ctx, "accept", make)


ACCEPT_ROOT_STEP = "accept-run-completes"


def accept_run(ctx: Ctx) -> RunRecord:
    """The accepted run, or a pending block when the root step failed."""
    try:
        return accept_run_raw(ctx)
    except StepFailure as exc:
        raise StepPending(
            f"the accepted run must complete (root step {ACCEPT_ROOT_STEP})",
            "blocked: " + str(exc)[:160],
        ) from exc


def reject_run(ctx: Ctx) -> RunRecord:
    def make() -> RunRecord:
        return execute_run(
            ctx,
            "reject",
            stdin=decision_input("reject"),
            responses=[plan_response(), plan_response()],
        )

    return cached(ctx, "reject", make)


def released(ctx: Ctx) -> tuple[RunRecord, Path, dict[str, bytes]]:
    rec = accept_run(ctx)
    require(rec.zip_path is not None, f"accepted run left {len(rec.zips)} release.zip")
    with zipfile.ZipFile(rec.zip_path) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    return rec, rec.zip_path, members


# --------------------------------------------------------------------------
# ZIP tamper helpers (real engine packaging helpers, attacker-style edits)
# --------------------------------------------------------------------------


def recut_zip(
    members: Mapping[str, bytes],
    edits: Mapping[str, bytes | None],
    destination: Path,
    *,
    recut_checksums: bool = True,
) -> Path:
    """Write an edited copy of a release; ``None`` deletes a member."""
    from engine.artifacts.package import build_checksums, build_zip_bytes

    files = {n: b for n, b in members.items() if n != "checksums.sha256"}
    original_checksums = members.get("checksums.sha256")
    for name, payload in edits.items():
        if payload is None:
            files.pop(name, None)
        else:
            files[name] = payload
    if recut_checksums:
        files["checksums.sha256"] = build_checksums(files).encode()
        destination.write_bytes(build_zip_bytes(files))
    else:
        files["checksums.sha256"] = original_checksums or b""
        destination.write_bytes(build_zip_bytes(files))
    return destination


def mutate_first_number(value: Any) -> tuple[Any, bool]:
    """Return (copy with the first number changed, changed?)."""
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)):
        return value + 1, True
    if isinstance(value, list):
        out = list(value)
        for i, item in enumerate(out):
            out[i], hit = mutate_first_number(item)
            if hit:
                return out, True
        return out, False
    if isinstance(value, dict):
        copy = dict(value)
        for key in sorted(copy):
            copy[key], hit = mutate_first_number(copy[key])
            if hit:
                return copy, True
        return copy, False
    return value, False


def verify_zip(ctx: Ctx, archive: Path, manifest: Path | None = None) -> Invocation:
    return invoke(
        ctx,
        {
            "mode": "verify_release",
            "zip": str(archive),
            "manifest": None if manifest is None else str(manifest),
        },
    )


def expect_refusal(inv: Invocation) -> dict[str, Any]:
    result = inv.require_driver_ok()
    require(result.get("verified") is False, "tampered package was accepted")
    require(
        result.get("error_type") == "ReleaseVerificationError",
        f"refusal was not a typed verifier refusal: {result.get('error_type')}",
    )
    return {"refusal": redact(str(result.get("message", "")), limit=300)}


# --------------------------------------------------------------------------
# Capability probes (what the installed layout really offers)
# --------------------------------------------------------------------------


def probe_capabilities(ctx: Ctx) -> dict[str, Any]:
    caps: dict[str, Any] = {}
    prep = cli(ctx, ["prepare", "--help"], mode="help")
    caps["prepare_case_option"] = "--case" in prep.text
    run_help = cli(ctx, ["run", "--help"], mode="help")
    caps["offline_policy"] = "offline" in run_help.text
    caps["plan_replay_option"] = "--plan" in run_help.text
    verify = cli(ctx, ["verify", "--help"], mode="help")
    caps["verify_command"] = verify.rc == 0
    caps["verify_require_case"] = verify.rc == 0 and "--require-case" in verify.text
    probe = invoke(ctx, {"mode": "import_probe"})
    res = probe.result or {}
    caps["profile_workflow_helpers"] = bool(res.get("workflow_importable"))
    caps["imports"] = res.get("imports", {})
    caps["interpreter"] = res.get("interpreter")
    caps["case_member_names"] = res.get("case_member_names", [])
    caps["case_check_ids"] = res.get("case_check_ids", [])
    manifest = load_json(ctx.install.root / "profiles" / "hylogger" / "profile.json")
    declared = manifest.get("generation", {}).get("cases")
    caps["manifest_declares_cases"] = bool(declared)
    caps["manifest_cases"] = declared if isinstance(declared, list) else []
    caps["graph_case_stages"] = sorted(
        s for s in manifest.get("capabilities", {}) if s.startswith("case_")
    )
    caps["verify_anchor_options"] = sorted(
        opt
        for opt in ("--expect-contract-sha256", "--expect-catalogue-sha256", "--source")
        if opt in verify.text
    )
    caps["layout_locations"] = res.get("locations", {})
    return caps


def case_ready(ctx: Ctx) -> bool:
    caps = ctx.caps
    return bool(
        caps.get("prepare_case_option")
        and caps.get("manifest_declares_cases")
        and caps.get("profile_workflow_helpers")
    )


ENGINE_CASE_DEP = (
    "engine integration (parallel-engine-workflow-brief.md): `ruloom prepare "
    "--case ID@VERSION`, spec `case`, manifest `generation.cases`, case checks "
    "in the runner/graph"
)
ENGINE_VERIFY_DEP = (
    "engine integration: `ruloom verify` with `--require-case ID@VERSION` and "
    "case members packaged in release.zip"
)
ENGINE_AI_DEP = (
    "engine integration: runtime roles case_research, case_source_assess, "
    "case_candidate_review in the same graph"
)


def need_case(ctx: Ctx, dependency: str = ENGINE_CASE_DEP) -> None:
    if not case_ready(ctx):
        raise StepPending(dependency, "case interface not present in this layout")


def need_verify(ctx: Ctx) -> None:
    """Verifier steps also need a case release, so the whole interface first."""
    if not ctx.caps.get("verify_require_case"):
        raise StepPending(ENGINE_VERIFY_DEP, "`verify --require-case` not present")
    need_case(ctx)


# --------------------------------------------------------------------------
# Case flow (exercised only when the engine exposes the interface)
# --------------------------------------------------------------------------


def case_roles(
    *,
    strategy: str = STRATEGY,
    seed: int = SEED,
    rows: int = N_ROWS,
    mutate: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Role-mock settings: valid handoffs, plus optional one-field mutations."""
    return {
        "prepare": prepare_response(strategy=strategy, seed=seed, n_rows=rows),
        "plan": plan_response(n_rows=rows, seed=seed, strategy=strategy),
        "mutate": {k: dict(v) for k, v in (mutate or {}).items()},
    }


def case_prepare(
    ctx: Ctx,
    source: Path,
    source_sha: str,
    *,
    roles: Mapping[str, Any] | None = None,
    max_attempts: int = 2,
) -> tuple[Invocation, Path]:
    draft = ctx.fresh("draft-case")
    inv = cli(
        ctx,
        [
            "prepare",
            str(source),
            "--profile",
            "hylogger",
            "--case",
            CASE_REF,
            "--request",
            REQUEST_TEXT,
            "--max-attempts",
            str(max_attempts),
            "--out",
            str(draft),
            *PREPARE_ARGS_TAIL,
        ],
        roles=roles if roles is not None else case_roles(),
    )
    return inv, draft


def case_execute(
    ctx: Ctx,
    name: str,
    *,
    decision: str = "reject",
    roles: Mapping[str, Any] | None = None,
    max_attempts: int = 2,
    extra: Sequence[str] = (),
    stdin: str | None = None,
) -> RunRecord:
    """Real case route: ``prepare --case`` then ``run --spec`` (not cached)."""
    settings = roles if roles is not None else case_roles(seed=KNOWN_GOOD_CASE_SEED)
    inv, draft = case_prepare(
        ctx,
        ctx.fixture.csv,
        ctx.fixture.csv_sha256,
        roles=settings,
        max_attempts=max_attempts,
    )
    inv.require_driver_ok()
    check_mock_ok(inv)
    require(inv.rc == 0, f"case prepare exit {inv.rc}: {inv.text[-300:]}")
    spec = draft / "spec.json"
    require(spec.is_file(), "case prepare wrote no spec.json")
    rec = execute_run(
        ctx,
        name,
        stdin=stdin if stdin is not None else decision_input(decision),
        responses=None,
        roles=settings,
        max_attempts=max_attempts,
        extra=["--spec", str(spec), *extra],
        rows=None,
    )
    rec.inv.require_driver_ok()
    check_mock_ok(rec.inv)
    rec.draft = draft
    return rec


def case_run(ctx: Ctx, decision: str) -> RunRecord:
    return cached(
        ctx,
        f"case-{decision}",
        lambda: case_execute(ctx, f"case-{decision}", decision=decision),
    )


def stage_status(rec: RunRecord) -> dict[str, list[str]]:
    """Every recorded status per stage name, from the hash-chained run log."""
    found: dict[str, list[str]] = {}
    for event in rec.events():
        found.setdefault(str(event.get("stage")), []).append(str(event.get("status")))
    return found


def case_attempts(rec: RunRecord) -> list[dict[str, Any]]:
    """Per attempt: seed, verdict and the failing check ids, from real records."""
    out = []
    for receipt, checks in zip(rec.receipts(), rec.checks(), strict=False):
        failed = sorted(
            str(c.get("check_id")) for c in checks if c.get("status") == "fail"
        )
        out.append(
            {
                "seed": receipt.get("seed"),
                "verdict": receipt.get("verdict"),
                "failed": failed,
            }
        )
    return out


# --------------------------------------------------------------------------
# Step runner
# --------------------------------------------------------------------------


def run_step(
    ctx: Ctx, step_id: str, title: str, fn: Callable[[], Mapping[str, Any] | None]
) -> Step:
    try:
        evidence = dict(fn() or {})
    except StepPending as exc:
        return Step(step_id, title, PENDING, redact(exc.detail), exc.dependency)
    except StepFailure as exc:
        return Step(
            step_id,
            title,
            FAIL,
            redact(str(exc), hide=[ctx.work]),
            fault=FAULT_PRODUCT,
        )
    except HarnessError as exc:
        detail = f"harness error: {exc}"
        return Step(
            step_id,
            title,
            FAIL,
            redact(detail, hide=[ctx.work]),
            fault=FAULT_HARNESS,
        )
    except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
        detail = f"harness error {type(exc).__name__}: {exc}"
        return Step(
            step_id,
            title,
            FAIL,
            redact(detail, hide=[ctx.work]),
            fault=FAULT_HARNESS,
        )
    detail = str(evidence.pop("detail", "ok"))
    return Step(step_id, title, PASS, redact(detail), None, evidence)


def blocked(step_id: str) -> StepPending:
    return StepPending(
        f"upstream prerequisite did not pass ({step_id})",
        "blocked: a prerequisite run failed; see that step",
    )


def python_requirement(version: str | None = None) -> tuple[bool, str]:
    """Whether the driver interpreter satisfies the project's requires-python."""
    with open(ROOT / "pyproject.toml", "rb") as handle:
        spec = str(tomllib.load(handle)["project"].get("requires-python", ""))
    text = version or sys.version.split()[0]
    found = re.match(r"(\d+)\.(\d+)", text)
    current = (int(found.group(1)), int(found.group(2))) if found else (0, 0)
    lower = re.search(r">=\s*(\d+)\.(\d+)", spec)
    upper = re.search(r"<\s*(\d+)\.(\d+)", spec)
    ok = found is not None
    if lower and current < (int(lower.group(1)), int(lower.group(2))):
        ok = False
    if upper and current >= (int(upper.group(1)), int(upper.group(2))):
        ok = False
    return ok, f"driver interpreter {text} vs requires-python {spec!r}"


INTERPRETER_DEP = (
    "an interpreter satisfying requires-python with the project dependencies "
    "installed (for example the repository .venv)"
)


def scenario_prerequisites(ctx: Ctx) -> Scenario:
    sc = Scenario("prerequisites", "Layout and interpreter can run the real workflow")

    def layout() -> dict[str, Any]:
        harness_require(
            (ctx.install.root / "engine" / "cli" / "__init__.py").is_file(),
            "no engine.cli in the layout",
        )
        return {
            "detail": f"{ctx.install.kind} layout prepared",
            "wheel_sha256": ctx.install.wheel_sha256,
        }

    def interpreter() -> dict[str, Any]:
        ok, detail = python_requirement(ctx.caps.get("interpreter"))
        harness_require(ok, f"unsupported interpreter: {detail}")
        return {"detail": detail}

    def imports() -> dict[str, Any]:
        results = ctx.caps.get("imports", {})
        bad = {k: v for k, v in results.items() if v != "ok"}
        harness_require(
            results and not bad,
            f"modules not importable in this interpreter: {bad or 'probe returned nothing'}",
        )
        return {
            "detail": "engine, consumer and profile capabilities import",
            "modules": sorted(results),
        }

    sc.steps.append(
        run_step(ctx, "layout-prepared", "Wheel (or tree) layout prepared", layout)
    )
    sc.steps.append(
        run_step(
            ctx,
            "interpreter-supported",
            "Interpreter satisfies requires-python",
            interpreter,
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "engine-imports",
            "Engine and profile import with their dependencies",
            imports,
        )
    )
    return sc


# --------------------------------------------------------------------------
# Scenario 1: request preparation, pinned case/source, checked handoffs
# --------------------------------------------------------------------------


def scenario_request_preparation(ctx: Ctx) -> Scenario:
    sc = Scenario("request-preparation", "Request preparation and checked AI handoffs")
    fx = ctx.fixture

    def prepare_args(draft: Path, source: Path, max_attempts: str = "2") -> list[str]:
        return [
            "prepare",
            str(source),
            "--profile",
            "hylogger",
            "--request",
            REQUEST_TEXT,
            "--max-attempts",
            max_attempts,
            "--out",
            str(draft),
            "--model-policy",
            "local-only",
            "--role-adapter",
            "planner=glimmer_local",
        ]

    def draft_ok() -> dict[str, Any]:
        draft = ctx.fresh("draft")
        inv = cli(ctx, prepare_args(draft, fx.csv), responses=[prepare_response()])
        inv.require_driver_ok()
        require(inv.rc == 0, f"prepare exit {inv.rc}: {inv.text[-300:]}")
        require(inv.calls == 1, f"expected exactly one mock call, got {inv.calls}")
        require(inv.canary_hits == 0, "source canary reached the model prompt")
        files = {
            n: (draft / n).is_file() for n in ("request.json", "spec.json", "plan.json")
        }
        require(all(files.values()), f"draft files missing: {files}")
        receipt = load_json(draft / "request.json")
        spec = load_json(draft / "spec.json")
        plan = load_json(draft / "plan.json")
        require(
            receipt.get("status") == "draft-needs-human-review",
            "draft not left for review",
        )
        require(
            receipt.get("source_rows_sent") is False,
            "receipt does not deny row disclosure",
        )
        require(
            receipt.get("source_sha256") == fx.csv_sha256, "receipt source hash differs"
        )
        require(
            spec.get("source", {}).get("sha256") == fx.csv_sha256,
            "spec does not pin source",
        )
        require(
            receipt.get("prompt_sha256") in inv.result["prompt_sha256"],
            "receipt prompt digest does not match the prompt actually sent",
        )
        require(
            (plan.get("seed"), plan.get("n_rows")) == (SEED, N_ROWS),
            "plan differs from the checked handoff",
        )
        require(
            "acceptance" in receipt and "draft only" in receipt["acceptance"],
            "draft claims acceptance",
        )
        ctx.hashes["draft_request_json_sha256"] = file_digest(draft / "request.json")
        ctx.hashes["draft_spec_json_sha256"] = file_digest(draft / "spec.json")
        ctx.hashes["draft_plan_json_sha256"] = file_digest(draft / "plan.json")
        return {
            "detail": "draft written; source pinned by hash; no rows sent",
            "model_calls": inv.calls,
        }

    sc.steps.append(
        run_step(
            ctx,
            "prepare-draft-checked-handoff",
            "Draft pins source hash; model sees no rows",
            draft_ok,
        )
    )

    def unsupported_refused() -> dict[str, Any]:
        draft = ctx.fresh("draft-unsupported")
        resp = prepare_response(unsupported=["real spectral curves"])
        inv = cli(ctx, prepare_args(draft, fx.csv), responses=[resp])
        inv.require_driver_ok()
        require(inv.rc == 1, f"expected refusal exit 1, got {inv.rc}")
        require(
            not (draft / "spec.json").exists(), "spec created for unsupported request"
        )
        require(
            not (draft / "plan.json").exists(), "plan created for unsupported request"
        )
        require(
            load_json(draft / "request.json").get("status") == "unsupported",
            "not recorded unsupported",
        )
        return {"detail": "unsupported requirement refused; no spec or plan"}

    sc.steps.append(
        run_step(
            ctx,
            "prepare-unsupported-refused",
            "Unsupported requirement is refused",
            unsupported_refused,
        )
    )

    def malformed_refused() -> dict[str, Any]:
        draft = ctx.fresh("draft-malformed")
        resp = prepare_response(n_rows=0, invented_threshold=0.5)
        inv = cli(ctx, prepare_args(draft, fx.csv), responses=[resp])
        inv.require_driver_ok()
        require(inv.rc == 1, f"expected refusal exit 1, got {inv.rc}")
        require(
            not (draft / "spec.json").exists(), "spec created from malformed handoff"
        )
        require(
            not (draft / "plan.json").exists(), "plan created from malformed handoff"
        )
        require("error" in load_json(draft / "request.json"), "refusal not recorded")
        return {"detail": "schema-violating handoff refused; no spec or plan"}

    sc.steps.append(
        run_step(
            ctx,
            "prepare-malformed-handoff-refused",
            "Malformed AI handoff is refused",
            malformed_refused,
        )
    )

    def bad_bound_before_model() -> dict[str, Any]:
        draft = ctx.fresh("draft-bad-bound")
        inv = cli(ctx, prepare_args(draft, fx.csv, "0"), responses=[prepare_response()])
        inv.require_driver_ok()
        require(inv.rc == 1, f"expected refusal exit 1, got {inv.rc}")
        require(inv.calls == 0, f"model was called {inv.calls} time(s) before refusal")
        return {"detail": "invalid attempt bound refused with zero model calls"}

    sc.steps.append(
        run_step(
            ctx,
            "prepare-bad-bound-zero-calls",
            "Bad bound refused before any model call",
            bad_bound_before_model,
        )
    )

    def case_prepare_pinned() -> dict[str, Any]:
        need_case(ctx)
        inv, draft = case_prepare(ctx, fx.csv, fx.csv_sha256)
        inv.require_driver_ok()
        require(inv.rc == 0, f"case prepare exit {inv.rc}: {inv.text[-300:]}")
        spec = load_json(draft / "spec.json")
        case = spec.get("case") or {}
        require(
            case.get("id") == CASE_ID and case.get("version") == CASE_VERSION,
            f"spec case binding is {case!r}",
        )
        require(
            spec.get("source", {}).get("sha256") == fx.csv_sha256,
            "case spec does not pin the source",
        )
        require(inv.canary_hits == 0, "source canary reached a model prompt")
        require(inv.calls == 1, f"expected one model call, got {inv.calls}")
        return {"detail": "case id/version and source hash pinned in the draft"}

    sc.steps.append(
        run_step(
            ctx,
            "case-prepare-pins-case-and-source",
            "Case-selected draft pins case and source",
            lambda: cached(ctx, "case-prepare-control", case_prepare_pinned),
        )
    )

    def refused_before_model(label: str, ref: str, source: Path) -> dict[str, Any]:
        # A valid case prepare must work first: otherwise any refusal below may
        # just be prepare being broken wholesale and proves nothing.
        need_case(ctx)
        try:
            cached(ctx, "case-prepare-control", case_prepare_pinned)
        except StepFailure as exc:
            raise StepPending(
                "a valid `prepare --case` must work first (step case-prepare-pins-case-and-source)",
                "blocked: " + str(exc)[:160],
            ) from exc
        draft = ctx.fresh(f"draft-{label}")
        argv = prepare_args(draft, source)
        argv[argv.index("--request") : argv.index("--request")] = ["--case", ref]
        inv = cli(ctx, argv, responses=[prepare_response()])
        inv.require_driver_ok()
        require(inv.rc not in (0, None), f"{label}: was not refused")
        require(
            inv.calls == 0,
            f"{label}: the model was called {inv.calls} time(s) before the refusal",
        )
        require(not (draft / "spec.json").exists(), f"{label}: a spec was written")
        return {"detail": f"{label}: refused with zero model calls"}

    for step_id, title, label, ref, source in (
        (
            "case-prepare-unknown-case-zero-calls",
            "Unknown case is refused before any model call",
            "unknown-case",
            "no_such_case@1",
            fx.csv,
        ),
        (
            "case-prepare-unknown-version-zero-calls",
            "Unknown case version is refused before any model call",
            "unknown-version",
            f"{CASE_ID}@999",
            fx.csv,
        ),
    ):
        sc.steps.append(
            run_step(
                ctx,
                step_id,
                title,
                lambda lb=label, rf=ref, sr=source: refused_before_model(lb, rf, sr),
            )
        )

    def unsuitable_source_refused_before_model() -> dict[str, Any]:
        # For a selected case the profile judges the source BEFORE the request
        # model is contacted, so prepare must refuse it with zero model calls.
        need_case(ctx)
        try:
            cached(ctx, "case-prepare-control", case_prepare_pinned)
        except StepFailure as exc:
            raise StepPending(
                "a valid `prepare --case` must work first (step case-prepare-pins-case-and-source)",
                "blocked: " + str(exc)[:160],
            ) from exc
        pre, draft = case_prepare(ctx, fx.unsuitable_csv, fx.unsuitable_sha256)
        pre.require_driver_ok()
        check_mock_ok(pre)
        require(pre.rc not in (0, None), "prepare accepted an unsuitable source")
        require(
            pre.calls == 0,
            f"the request model was called {pre.calls} time(s) before the "
            "unsuitable source was refused",
        )
        require(
            not (draft / "spec.json").exists() and not (draft / "plan.json").exists(),
            "draft settings were written for an unsuitable source",
        )
        require(
            "suitab" in pre.text.lower(),
            f"the refusal does not cite source suitability: {pre.text[-200:]}",
        )
        return {
            "detail": "prepare refused the unsuitable source before any model call, "
            "and wrote no draft settings",
            "independent_recheck": "run intake repeats the check; covered by the "
            "profile tests because no draft exists to run from",
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-unsuitable-source-refused-before-model",
            "Unsuitable source is refused by prepare before any model call",
            unsuitable_source_refused_before_model,
        )
    )

    def gate_on_valid_prepare() -> None:
        need_case(ctx)
        try:
            cached(ctx, "case-prepare-control", case_prepare_pinned)
        except StepFailure as exc:
            raise StepPending(
                "a valid `prepare --case` must work first (step case-prepare-pins-case-and-source)",
                "blocked: " + str(exc)[:160],
            ) from exc

    def infeasible_count_refused_before_draft() -> dict[str, Any]:
        # The count exists only after interpretation, so the model IS called
        # once; what must hold is that no draft settings follow an infeasible one.
        gate_on_valid_prepare()
        pre, draft = case_prepare(
            ctx, fx.csv, fx.csv_sha256, roles=case_roles(rows=INFEASIBLE_ROWS)
        )
        pre.require_driver_ok()
        check_mock_ok(pre)
        require(pre.rc not in (0, None), "an infeasible row count was accepted")
        require(
            pre.calls == 1,
            f"expected one interpretation call before the count check, got {pre.calls}",
        )
        require(
            not (draft / "spec.json").exists() and not (draft / "plan.json").exists(),
            "draft settings were written for an infeasible row count",
        )
        require(
            "refused before any draft was written" in pre.text,
            f"the refusal does not say it came before any draft: {pre.text[-200:]}",
        )
        return {
            "detail": f"{INFEASIBLE_ROWS} rows refused after interpretation, before any draft",
            "requested_rows": INFEASIBLE_ROWS,
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-infeasible-count-refused-before-draft",
            "An infeasible interpreted row count is refused before any draft",
            infeasible_count_refused_before_draft,
        )
    )

    def source_change_during_preparation_refused() -> dict[str, Any]:
        gate_on_valid_prepare()
        folder = ctx.fresh("source-change")
        folder.mkdir(parents=True, exist_ok=True)
        csv = folder / fx.csv.name
        shutil.copy2(fx.csv, csv)
        shutil.copy2(fx.sidecar, folder / fx.sidecar.name)
        # The role mock appends one valid row to this throwaway copy while the
        # request model is "interpreting"; the shared fixture is never touched.
        roles = {
            **case_roles(),
            "touch": {
                "path": str(csv),
                "append": "999.000000,1,CARBONATE,CARBONATE,CANARY-ROW-99999\n",
            },
        }
        pre, draft = case_prepare(ctx, csv, file_digest(csv), roles=roles)
        pre.require_driver_ok()
        check_mock_ok(pre)
        require(
            pre.rc not in (0, None), "a source changed during preparation was accepted"
        )
        require(pre.calls == 1, f"expected one interpretation call, got {pre.calls}")
        require(
            not (draft / "spec.json").exists() and not (draft / "plan.json").exists(),
            "draft settings were written for a source that changed mid-preparation",
        )
        require(
            "source changed during preparation" in pre.text,
            f"the refusal does not cite the changed source: {pre.text[-200:]}",
        )
        return {
            "detail": "a source changed during interpretation was refused; no draft"
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-source-change-during-preparation-refused",
            "A source that changes during preparation is refused",
            source_change_during_preparation_refused,
        )
    )
    return sc


# --------------------------------------------------------------------------
# Scenario 2: generation, practical checks, bounded same-seed repair
# --------------------------------------------------------------------------


def scenario_generation(ctx: Ctx) -> Scenario:
    sc = Scenario(
        "generation-checks-repair", "Generation, practical checks and bounded repair"
    )

    def run_reaches_gate() -> dict[str, Any]:
        rec = reject_run(ctx)
        res = rec.inv.require_driver_ok()
        layout = res.get("locations", {})
        for key, location in layout.items():
            require(inside_layout(ctx, location), f"layout leak: {key} from {location}")
        require(
            rec.inv.rc == 2,
            f"run did not reach the gate and stop on reject: exit {rec.inv.rc}: {rec.inv.text[-300:]}",
        )
        require(rec.run_dir is not None, "no unique run directory")
        require(rec.receipts(), "no attempt receipts recorded")
        return {
            "detail": "generation, validation and packaging reached the human gate",
            "attempts": len(rec.receipts()),
        }

    sc.steps.append(
        run_step(
            ctx,
            "run-reaches-human-gate",
            "Installed run reaches the gate",
            run_reaches_gate,
        )
    )

    def accept_completes() -> dict[str, Any]:
        rec = accept_run_raw(ctx)
        return {
            "detail": "accepted run exited 0 and left exactly one release.zip",
            "attempts": len(rec.receipts()),
        }

    sc.steps.append(
        run_step(
            ctx,
            ACCEPT_ROOT_STEP,
            "Accepted run completes and releases once",
            accept_completes,
        )
    )

    def required_checks_pass() -> dict[str, Any]:
        rec, _zip, members = released(ctx)
        report = json.loads(members["validation-report.json"])
        require(
            report.get("verdict") == "pass", f"report verdict {report.get('verdict')!r}"
        )
        status: dict[str, str] = {}
        for item in report.get("results", []):
            name = item.get("name") or item.get("check_id") or item.get("id")
            require(name, f"unrecognised report result shape: {sorted(item)}")
            status[str(name)] = str(item.get("status"))
        wanted = required_check_ids(ctx)
        missing = [c for c in wanted if c not in status]
        not_pass = [c for c in wanted if status.get(c) != "pass"]
        require(not missing, f"required checks absent from the report: {missing}")
        require(not not_pass, f"required checks not passed: {not_pass}")
        skipped = sorted(n for n, s in status.items() if s == "skip")
        return {
            "detail": f"{len(wanted)} required checks present and passed",
            "skipped_not_counted_as_pass": skipped,
        }

    sc.steps.append(
        run_step(
            ctx,
            "required-checks-recorded-and-pass",
            "Every required practical check is recorded and passes",
            required_checks_pass,
        )
    )

    def failing_first_attempt() -> RunRecord:
        rec = execute_run(
            ctx,
            "repair",
            stdin=decision_input("reject"),
            responses=[
                plan_response(n_rows=SMALL_ROWS),
                plan_response(n_rows=SMALL_ROWS),
            ],
            rows=SMALL_ROWS,
        )
        rec.inv.require_driver_ok()
        return rec

    def bounded_repair() -> dict[str, Any]:
        rec = cached(ctx, "repair", failing_first_attempt)
        receipts = rec.receipts()
        require(
            receipts, f"no attempts recorded (exit {rec.inv.rc}): {rec.inv.text[-300:]}"
        )
        require(len(receipts) <= 2, f"attempt bound exceeded: {len(receipts)} > 2")
        if len(receipts) < 2:
            raise StepPending(
                "a fixture whose first attempt fails the real checks",
                "repair not exercised: the first attempt passed, so no repair evidence exists",
            )
        require(
            receipts[0].get("verdict") == "fail",
            "first attempt verdict is not retained as fail",
        )
        require(rec.inv.calls <= 2, "unexpected extra model calls")
        # The legacy route lets the repair model propose any seed, so the seed
        # is recorded as an observation only. Same-seed repair is a case-route
        # guarantee and is proven by the case-controls scenario instead.
        return {
            "detail": "legacy route: the failing attempt stays visible and the attempt bound holds",
            "attempt_verdicts": [r.get("verdict") for r in receipts],
            "observed_seeds": [r.get("seed") for r in receipts],
            "scope": "no same-seed claim is made here; see case-seed-reroll-refused and case-supported-correction-same-seed",
        }

    sc.steps.append(
        run_step(
            ctx,
            "legacy-repair-bounded-and-visible",
            "Legacy repair: failed attempt stays visible and the bound holds",
            bounded_repair,
        )
    )

    def bound_one_no_release() -> dict[str, Any]:
        rec = execute_run(
            ctx,
            "bound1",
            stdin=decision_input("accept"),
            responses=[plan_response(n_rows=SMALL_ROWS)],
            max_attempts=1,
            rows=SMALL_ROWS,
        )
        rec.inv.require_driver_ok()
        receipts = rec.receipts()
        require(len(receipts) == 1, f"expected exactly 1 attempt, got {len(receipts)}")
        if rec.inv.rc == 0:
            raise StepPending(
                "a fixture whose first attempt fails the real checks",
                "the 30-row plan passed the real checks, so the failing-bound control did not trigger",
            )
        require(rec.inv.rc == 1, f"expected failed exit 1, got {rec.inv.rc}")
        require(not rec.zips, "a release exists although the verdict failed")
        require(rec.inv.calls == 1, "repair model was called despite a bound of 1")
        return {
            "detail": "failing verdict with bound 1: one attempt, no repair call, no gate, no release"
        }

    sc.steps.append(
        run_step(
            ctx,
            "failing-verdict-blocks-release",
            "Failing verdict with bound 1 blocks the gate and release",
            bound_one_no_release,
        )
    )

    def case_checks_and_repair() -> dict[str, Any]:
        need_case(ctx)
        case_accept(ctx)  # pending, not fail, while the case route cannot complete
        rec = case_run(ctx, "reject")
        rec.inv.require_driver_ok()
        expected = set(ctx.caps.get("case_check_ids", []))
        require(expected, "profile exposes no CASE_CHECK_IDS")
        attempts = rec.checks()
        require(attempts, "the case run recorded no attempt, so no case check ran")
        for index, attempt in enumerate(attempts, 1):
            by_id = {c.get("check_id"): c.get("status") for c in attempt}
            require(
                expected <= set(by_id),
                f"attempt {index} omits case checks: {sorted(expected - set(by_id))}",
            )
            skipped = sorted(i for i in expected if by_id[i] not in ("pass", "fail"))
            require(
                not skipped,
                f"attempt {index} left case checks neither passed nor failed: {skipped}",
            )
        seeds = {r.get("seed") for r in rec.receipts()}
        require(
            seeds == {KNOWN_GOOD_CASE_SEED},
            f"seed changed across case attempts: {seeds}",
        )
        return {
            "detail": "every case.* check was evaluated on every attempt; seed constant",
            "case_check_count": len(expected),
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-checks-every-attempt",
            "All case.* checks evaluated each attempt; seed unchanged",
            case_checks_and_repair,
        )
    )

    def ai_roles_recorded() -> dict[str, Any]:
        need_case(ctx, ENGINE_AI_DEP)
        case_accept(ctx)
        rec = case_run(ctx, "reject")
        states = stage_status(rec)
        bad = {
            s: states.get(s) for s in CASE_AI_STAGES if "ok" not in states.get(s, [])
        }
        require(not bad, f"AI role stages not recorded ok in the run log: {bad}")
        return {"detail": "checked AI role stages recorded in the run log"}

    sc.steps.append(
        run_step(
            ctx,
            "case-ai-role-stages-recorded",
            "Case AI role stages are recorded and advisory",
            ai_roles_recorded,
        )
    )
    return sc


# --------------------------------------------------------------------------
# Scenario 3: operator rejection and acceptance
# --------------------------------------------------------------------------


def scenario_operator(ctx: Ctx) -> Scenario:
    sc = Scenario("operator-decision", "Operator rejection and exact-file acceptance")

    def rejection() -> dict[str, Any]:
        rec = reject_run(ctx)
        rec.inv.require_driver_ok()
        require(rec.inv.rc == 2, f"reject exit {rec.inv.rc}, expected 2")
        require(not rec.zips, f"reject produced release.zip: {rec.zips}")
        assert rec.run_dir is not None
        for name in ("release.sha256", "manifest.json"):
            require(not (rec.run_dir / name).exists(), f"reject left {name}")
        decision = rec.decision() or {}
        require(
            decision.get("verdict") == "reject", f"decision {decision.get('verdict')!r}"
        )
        require(
            str(decision.get("note", "")).startswith("ENGINEERING TEST"),
            "gate note is not labelled",
        )
        require(
            (rec.run_json() or {}).get("status") == "declined",
            "terminal status is not declined",
        )
        return {"detail": "reject: exit 2, no release.zip, no release manifest or hash"}

    sc.steps.append(
        run_step(
            ctx, "reject-produces-no-release", "Rejection leaves no release", rejection
        )
    )

    def invalid_decision() -> dict[str, Any]:
        rec = execute_run(
            ctx,
            "invalid",
            stdin=decision_input("approve"),
            responses=[plan_response(), plan_response()],
        )
        rec.inv.require_driver_ok()
        require(rec.inv.rc == 2, f"invalid decision exit {rec.inv.rc}, expected 2")
        require(not rec.zips, "an unrecognised decision produced a release")
        return {"detail": "only the exact word accept releases; anything else does not"}

    sc.steps.append(
        run_step(
            ctx,
            "unrecognised-decision-no-release",
            "Unrecognised decision leaves no release",
            invalid_decision,
        )
    )

    def acceptance_binding() -> dict[str, Any]:
        rec, zip_path, members = released(ctx)
        assert rec.run_dir is not None
        decision = rec.decision() or {}
        require(
            decision.get("verdict") == "accept", f"decision {decision.get('verdict')!r}"
        )
        require(
            str(decision.get("note", "")).startswith("ENGINEERING TEST"),
            "gate note is not labelled",
        )
        bound = decision.get("frozen_manifest_sha256")
        require(
            isinstance(bound, str) and len(bound) == 64,
            "decision binds no frozen digest",
        )
        frozen = load_json(rec.run_dir / "frozen-manifest.json")
        entries = frozen.get("members")
        require(
            isinstance(entries, list) and entries, "frozen manifest lists no members"
        )
        require(
            sha256_canonical(entries) == bound,
            "decision digest does not match the frozen member list",
        )
        require(
            frozen.get("evidence_digest") == bound,
            "frozen manifest digest differs from the decision",
        )
        checked = []
        for entry in entries:
            name = entry["path"]
            frozen_bytes = (rec.run_dir / "frozen" / name).read_bytes()
            require(
                sha256_bytes(frozen_bytes) == entry["sha256"],
                f"frozen copy of {name} differs from its entry",
            )
            require(name in members, f"accepted member {name} missing from the release")
            require(
                members[name] == frozen_bytes,
                f"released {name} differs from the frozen bytes",
            )
            checked.append(name)
        ctx.hashes["decision_bound_frozen_digest"] = bound
        ctx.hashes["release_zip_sha256"] = file_digest(zip_path)
        return {
            "detail": "acceptance digest matches the frozen list; released bytes equal frozen bytes",
            "members_checked": checked,
        }

    sc.steps.append(
        run_step(
            ctx,
            "accept-binds-exact-frozen-files",
            "Acceptance binds the exact checked files",
            acceptance_binding,
        )
    )

    def payload_binding() -> dict[str, Any]:
        rec, zip_path, members = released(ctx)
        report = json.loads(members["validation-report.json"])
        data_sha = sha256_bytes(members["data.csv"])
        require(
            report.get("artifact_sha256") == data_sha,
            "report does not bind the shipped data.csv",
        )
        require(
            CANARY_PREFIX.encode() not in members["data.csv"],
            "source canary was copied into data.csv",
        )
        sidecar = (zip_path.parent / "release.sha256").read_text().split()[0]
        require(
            sidecar == file_digest(zip_path),
            "detached release hash differs from release.zip",
        )
        ctx.hashes["data_csv_sha256"] = data_sha
        ctx.hashes["validation_report_sha256"] = sha256_bytes(
            members["validation-report.json"]
        )
        ctx.hashes["plan_json_sha256"] = sha256_bytes(members["plan.json"])
        return {
            "detail": "report binds data.csv; detached hash matches; no source canary in output"
        }

    sc.steps.append(
        run_step(
            ctx,
            "report-binds-shipped-data",
            "Report binds shipped data; no row copied from source",
            payload_binding,
        )
    )

    def automated_label() -> dict[str, Any]:
        rec = accept_run(ctx)
        decision = rec.decision() or {}
        note = str(decision.get("note", ""))
        require(
            note == ENGINEERING_NOTE,
            "automated gate input is not labelled as an engineering test",
        )
        ctx.observations.append(
            {
                "id": "gate-actor-label",
                "observed": f"decision.json records decided_by={decision.get('decided_by')!r} for piped input",
                "meaning": "the engine cannot distinguish automated input; this harness labels it only through the note and claims no owner approval",
            }
        )
        return {"detail": "gate note states: engineering test, not owner approval"}

    sc.steps.append(
        run_step(
            ctx,
            "automated-input-labelled",
            "Automated gate input is labelled an engineering test",
            automated_label,
        )
    )

    def case_accept_binding() -> dict[str, Any]:
        rec = case_accept(
            ctx
        )  # the root step case-accept-completes owns the run's failure
        require(rec.zip_path is not None, "case accept produced no single release.zip")
        with zipfile.ZipFile(rec.zip_path) as archive:
            names = set(archive.namelist())
        wanted = set(ctx.caps.get("case_member_names", []))
        require(wanted, "profile exposes no CASE_MEMBER_NAMES")
        require(
            wanted <= names,
            f"case members missing from the release: {sorted(wanted - names)}",
        )
        return {
            "detail": "case members shipped inside the accepted, checksummed package"
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-members-in-accepted-package",
            "Accepted case package ships its case evidence",
            case_accept_binding,
        )
    )
    return sc


# --------------------------------------------------------------------------
# Scenario 4: downloaded-package verification without the source
# --------------------------------------------------------------------------


def scenario_recipient(ctx: Ctx) -> Scenario:
    sc = Scenario(
        "recipient-verification", "Downloaded-package verification without the source"
    )

    def isolated_copy(*, with_manifest: bool) -> tuple[Path, Path | None]:
        rec, zip_path, _ = released(ctx)
        assert rec.run_dir is not None
        inbox = ctx.fresh("recipient")
        inbox.mkdir()
        shutil.copy2(zip_path, inbox / "release.zip")
        manifest = None
        if with_manifest:
            shutil.copy2(rec.run_dir / "manifest.json", inbox / "manifest.json")
            manifest = inbox / "manifest.json"
        return inbox / "release.zip", manifest

    def verify_without_source(with_manifest: bool) -> dict[str, Any]:
        copy, manifest = isolated_copy(with_manifest=with_manifest)
        hidden = ctx.fixture.directory.with_name("producer.hidden")
        ctx.fixture.directory.rename(hidden)
        try:
            require(
                not ctx.fixture.csv.exists(),
                "source still reachable during verification",
            )
            inv = verify_zip(ctx, copy, manifest)
        finally:
            hidden.rename(ctx.fixture.directory)
        res = inv.require_driver_ok()
        require(
            res.get("verified") is True,
            f"genuine package refused: {res.get('message')}",
        )
        require(
            res["verification"]["artifact_sha256"] == ctx.hashes.get("data_csv_sha256"),
            "verified artifact hash differs from the accepted data",
        )
        require(
            res["verification"]["zip_sha256"] == file_digest(copy),
            "verified zip hash differs",
        )
        for key, location in res.get("locations", {}).items():
            require(inside_layout(ctx, location), f"layout leak: {key}")
        return {
            "detail": "genuine package verified with the source removed",
            "external_manifest_used": with_manifest,
        }

    sc.steps.append(
        run_step(
            ctx,
            "verify-zip-only",
            "ZIP alone verifies; source unreachable",
            lambda: verify_without_source(False),
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "verify-zip-with-manifest-anchor",
            "ZIP plus external manifest verifies",
            lambda: verify_without_source(True),
        )
    )

    def verify_case_cli() -> dict[str, Any]:
        # The root of every case recipient and tamper control: a genuine case
        # package must verify (source unreachable) or no refusal proves anything.
        result = case_recipient_root(ctx)
        return {
            "detail": "recipient verified the case package with the expected-case anchor",
            "verified_keys": result.get("verified_keys"),
        }

    sc.steps.append(
        run_step(
            ctx,
            "verify-case-anchor",
            "Case package verifies with --require-case",
            verify_case_cli,
        )
    )
    return sc


# --------------------------------------------------------------------------
# Scenario 5: tamper, missing evidence, stripped case
# --------------------------------------------------------------------------


def scenario_tamper(ctx: Ctx) -> Scenario:
    sc = Scenario(
        "tamper-and-missing-evidence",
        "Tampered, missing and stripped packages are refused",
    )

    def tamper(
        edits_fn: Callable[[Mapping[str, bytes]], dict[str, bytes | None]],
        *,
        recut: bool,
        anchored: bool,
    ) -> dict[str, Any]:
        rec, _zip, members = released(ctx)
        assert rec.run_dir is not None
        dest = ctx.fresh("tampered").with_suffix(".zip")
        recut_zip(members, edits_fn(members), dest, recut_checksums=recut)
        manifest = rec.run_dir / "manifest.json" if anchored else None
        evidence = expect_refusal(verify_zip(ctx, dest, manifest))
        evidence["tampered_zip_sha256"] = file_digest(dest)
        evidence["detail"] = "refused with a typed verifier refusal"
        return evidence

    def report_edit(
        key: str, value: Any
    ) -> Callable[[Mapping[str, bytes]], dict[str, bytes | None]]:
        def edit(members: Mapping[str, bytes]) -> dict[str, bytes | None]:
            report = json.loads(members["validation-report.json"])
            report[key] = value
            return {
                "validation-report.json": (
                    json.dumps(report, sort_keys=True) + "\n"
                ).encode()
            }

        return edit

    sc.steps.append(
        run_step(
            ctx,
            "report-verdict-tamper",
            "Report verdict flipped (checksums re-cut)",
            lambda: tamper(report_edit("verdict", "fail"), recut=True, anchored=False),
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "report-binding-tamper",
            "Report bound to other bytes (checksums re-cut)",
            lambda: tamper(
                report_edit("artifact_sha256", "0" * 64), recut=True, anchored=False
            ),
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "data-tamper-stale-checksums",
            "Data edited without re-cutting checksums",
            lambda: tamper(
                lambda m: {"data.csv": m["data.csv"] + b"1,1,,\n"},
                recut=False,
                anchored=False,
            ),
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "data-tamper-recut-anchored",
            "Data edited, checksums re-cut, manifest anchor supplied",
            lambda: tamper(
                lambda m: {"data.csv": m["data.csv"] + b"1,1,,\n"},
                recut=True,
                anchored=True,
            ),
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "evidence-member-missing-anchored",
            "Evidence report removed (re-cut), manifest anchor supplied",
            lambda: tamper(
                lambda m: {"evidence-report.md": None}, recut=True, anchored=True
            ),
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "extra-member-anchored",
            "Unlisted member added (re-cut), manifest anchor supplied",
            lambda: tamper(lambda m: {"extra.txt": b"x"}, recut=True, anchored=True),
        )
    )
    sc.steps.append(
        run_step(
            ctx,
            "path-escape-member",
            "Member escaping its directory",
            lambda: tamper(
                lambda m: {"../escape.txt": b"x"}, recut=True, anchored=False
            ),
        )
    )

    def unanchored_observation() -> dict[str, Any]:
        rec, _zip, members = released(ctx)
        outcomes = {}
        for label, edits in (
            ("evidence-report-removed", {"evidence-report.md": None}),
            ("extra-member-added", {"extra.txt": b"x"}),
        ):
            dest = ctx.fresh("unanchored").with_suffix(".zip")
            recut_zip(members, edits, dest)
            res = verify_zip(ctx, dest).require_driver_ok()
            outcomes[label] = "accepted" if res.get("verified") else "refused"
        ctx.observations.append(
            {
                "id": "unanchored-recut-package",
                "observed": outcomes,
                "meaning": "without an external anchor, a re-cut package is only as strong as its internal checksums; supply the run manifest or --require-case",
            }
        )
        return {
            "detail": "recorded as an observation, not a pass criterion",
            "outcomes": outcomes,
        }

    sc.steps.append(
        run_step(
            ctx,
            "unanchored-limit-recorded",
            "Unanchored re-cut limitation is measured and recorded",
            unanchored_observation,
        )
    )

    def case_member_names() -> list[str]:
        names = list(ctx.caps.get("case_member_names", []))
        require(names, "profile exposes no CASE_MEMBER_NAMES")
        return names

    def case_tamper(
        label: str,
        edit: Callable[[Mapping[str, bytes], list[str]], dict[str, bytes | None]],
    ) -> dict[str, Any]:
        need_verify(ctx)
        # A genuine lone ZIP must verify with these exact flags in this kind of
        # location first, or any refusal below could be blanket refusal.
        need_zip_only_control(ctx)
        rec = case_accept(ctx)
        require(rec.zip_path is not None, "no case release to tamper with")
        with zipfile.ZipFile(rec.zip_path) as archive:
            members = {n: archive.read(n) for n in archive.namelist()}
        names = case_member_names()
        dest = ctx.fresh(f"case-{label}") / "release.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        recut_zip(members, edit(members, names), dest)
        inv = verify_cli(ctx, dest, *ZIP_ONLY_FLAGS)
        require(inv.rc not in (0, None), f"{label}: tampered case package was accepted")
        return {
            "detail": f"{label}: refused (exit {inv.rc})",
            "tampered_zip_sha256": file_digest(dest),
        }

    def mutate_member(
        members: Mapping[str, bytes], name: str
    ) -> dict[str, bytes | None]:
        require(name in members, f"{name} absent from the case package")
        mutated, hit = mutate_first_number(json.loads(members[name]))
        require(hit, f"no numeric field to tamper in {name}")
        return {name: (json.dumps(mutated, sort_keys=True) + "\n").encode()}

    case_edits: dict[
        str, Callable[[Mapping[str, bytes], list[str]], dict[str, bytes | None]]
    ] = {
        "stripped-case-members": lambda m, names: {n: None for n in names},
        "missing-case-reference": lambda m, names: {"case-reference.json": None},
        "tampered-contract": lambda m, names: mutate_member(m, "case-contract.json"),
        "tampered-utility-report": lambda m, names: mutate_member(
            m, "case-utility.json"
        ),
        "tampered-binding-version": lambda m, names: mutate_member(
            m, "case-binding.json"
        ),
    }
    for label, edit in case_edits.items():
        sc.steps.append(
            run_step(
                ctx,
                f"case-{label}",
                f"Case package: {label.replace('-', ' ')} refused",
                lambda lbl=label, fn=edit: case_tamper(lbl, fn),
            )
        )
    return sc


# --------------------------------------------------------------------------
# Scenario 6: offline replay
# --------------------------------------------------------------------------


def scenario_replay(ctx: Ctx) -> Scenario:
    sc = Scenario(
        "offline-replay", "Offline replay yields identical data with no model calls"
    )

    def offline_needs_plan() -> dict[str, Any]:
        rec = execute_run(
            ctx,
            "offline-noplan",
            stdin=decision_input("accept"),
            responses=None,
            extra=["--model-policy", "offline"],
        )
        rec.inv.require_driver_ok()
        require(rec.inv.rc == 1, f"expected refusal exit 1, got {rec.inv.rc}")
        require(rec.inv.calls == 0, "a model call occurred under the offline policy")
        require(not rec.zips, "offline run without a plan released something")
        return {
            "detail": "offline without --plan refused; zero model calls; no release"
        }

    sc.steps.append(
        run_step(
            ctx,
            "offline-requires-plan",
            "Offline policy without a saved plan is refused",
            offline_needs_plan,
        )
    )

    def bad_plan_refused() -> dict[str, Any]:
        bad = ctx.fresh("badplan").with_suffix(".json")
        bad.write_text("[1, 2, 3]\n")
        rec = execute_run(
            ctx,
            "offline-badplan",
            stdin=decision_input("accept"),
            responses=None,
            extra=["--model-policy", "offline", "--plan", str(bad)],
        )
        rec.inv.require_driver_ok()
        require(rec.inv.rc == 1, f"expected refusal exit 1, got {rec.inv.rc}")
        require(
            rec.inv.calls == 0 and not rec.zips,
            "malformed plan was not refused cleanly",
        )
        return {"detail": "malformed saved plan refused; zero calls; no release"}

    sc.steps.append(
        run_step(
            ctx,
            "offline-bad-plan-refused",
            "Malformed saved plan is refused offline",
            bad_plan_refused,
        )
    )

    def replay_identical() -> dict[str, Any]:
        rec, _zip, members = released(ctx)
        plan_path = ctx.fresh("saved-plan").with_suffix(".json")
        plan_path.write_bytes(members["plan.json"])
        replay = execute_run(
            ctx,
            "replay",
            stdin=decision_input("accept", ENGINEERING_NOTE + " (offline replay)"),
            responses=None,
            extra=["--model-policy", "offline", "--plan", str(plan_path)],
        )
        replay.inv.require_driver_ok()
        require(
            replay.inv.rc == 0,
            f"offline replay exit {replay.inv.rc}: {replay.inv.text[-400:]}",
        )
        require(
            replay.inv.calls == 0,
            f"offline replay made {replay.inv.calls} model call(s)",
        )
        require(
            replay.zip_path is not None, "offline replay produced no single release.zip"
        )
        with zipfile.ZipFile(replay.zip_path) as archive:
            again = {n: archive.read(n) for n in archive.namelist()}
        for name in ("data.csv", "plan.json", "seed.txt"):
            require(
                again[name] == members[name],
                f"replayed {name} differs from the original",
            )
        report_a = json.loads(members["validation-report.json"])
        report_b = json.loads(again["validation-report.json"])
        require(
            report_a.get("artifact_sha256") == report_b.get("artifact_sha256"),
            "replayed report binds different data",
        )
        require(report_b.get("verdict") == "pass", "replayed verdict is not pass")
        ctx.hashes["replay_data_csv_sha256"] = sha256_bytes(again["data.csv"])
        return {
            "detail": "identical data, plan and seed with zero model calls",
            "model_calls": replay.inv.calls,
        }

    sc.steps.append(
        run_step(
            ctx,
            "replay-identical-data",
            "Replay from the saved plan reproduces identical data",
            replay_identical,
        )
    )

    def offline_ai_stages() -> dict[str, Any]:
        need_case(ctx, ENGINE_AI_DEP)
        rec = case_accept(ctx)
        require(rec.draft is not None, "accepted case run kept no draft")
        plan = ctx.fresh("case-plan").with_suffix(".json")
        with zipfile.ZipFile(rec.zip_path) as archive:
            plan.write_bytes(archive.read("plan.json"))
        out = execute_run(
            ctx,
            "case-replay",
            stdin=decision_input("accept", ENGINEERING_NOTE + " (offline case replay)"),
            responses=None,
            rows=None,
            extra=[
                "--spec",
                str(rec.draft / "spec.json"),
                "--model-policy",
                "offline",
                "--plan",
                str(plan),
            ],
        )
        out.inv.require_driver_ok()
        require(
            out.inv.rc == 0,
            f"offline case replay exit {out.inv.rc}: {out.inv.text[-300:]}",
        )
        require(
            out.inv.calls == 0,
            f"offline case replay called a model {out.inv.calls} time(s)",
        )
        states = stage_status(out)
        bad = {
            s: states.get(s, [])
            for s in CASE_AI_STAGES
            if states.get(s, []) != ["unavailable"]
        }
        require(not bad, f"AI stages must read unavailable (never ok) offline: {bad}")
        attempts = case_attempts(out)
        require(
            attempts and attempts[-1]["verdict"] == "pass",
            "deterministic case checks did not pass on replay",
        )
        checks = out.checks()[-1]
        ids = {c.get("check_id") for c in checks}
        missing = sorted(set(ctx.caps.get("case_check_ids", [])) - ids)
        require(not missing, f"offline replay skipped case checks: {missing}")
        return {
            "detail": "AI stages recorded unavailable (not pass); deterministic case checks still enforced; zero model calls",
            "stage_status": {s: states[s] for s in CASE_AI_STAGES},
        }

    sc.steps.append(
        run_step(
            ctx,
            "offline-ai-stages-unavailable",
            "Offline AI stages are unavailable, never pass",
            offline_ai_stages,
        )
    )
    return sc


# --------------------------------------------------------------------------
# Case controls: manifest pin, seed, correction, handoffs, evidence, recipient
# --------------------------------------------------------------------------

INVENTED_EVIDENCE_ID = "invented_primary_source_2026"
CASE_ROOT_STEP = "case-accept-completes"


def check_acceptance_binding(
    rec: RunRecord, members: Mapping[str, bytes]
) -> dict[str, Any]:
    """The accepted digest binds exactly the frozen, released bytes."""
    assert rec.run_dir is not None
    decision = rec.decision() or {}
    require(
        decision.get("verdict") == "accept", f"decision {decision.get('verdict')!r}"
    )
    require(
        str(decision.get("note", "")).startswith("ENGINEERING TEST"),
        "gate note is not labelled as an engineering test",
    )
    bound = decision.get("frozen_manifest_sha256")
    require(isinstance(bound, str) and len(bound) == 64, "decision binds no digest")
    entries = load_json(rec.run_dir / "frozen-manifest.json").get("members")
    require(isinstance(entries, list) and entries, "frozen manifest lists no members")
    require(
        sha256_canonical(entries) == bound, "decision digest differs from frozen list"
    )
    names = []
    for entry in entries:
        name = entry["path"]
        frozen = (rec.run_dir / "frozen" / name).read_bytes()
        require(
            sha256_bytes(frozen) == entry["sha256"], f"frozen {name} differs from entry"
        )
        require(name in members, f"accepted member {name} missing from the release")
        require(members[name] == frozen, f"released {name} differs from frozen bytes")
        names.append(name)
    return {"bound": bound, "members": names}


def first_attempt_failed(rec: RunRecord, check_id: str) -> bool:
    """True when the run's first attempt really failed ``check_id``.

    This is a precondition check on a fixed control, never a search: the
    repair controls are only exercised when the pinned plan fails as expected.
    """
    attempts = case_attempts(rec)
    return bool(attempts) and check_id in attempts[0]["failed"]


def case_accept_raw(ctx: Ctx) -> RunRecord:
    def make() -> RunRecord:
        rec = case_run(ctx, "accept")
        require(
            rec.inv.rc == 0, f"case accept exit {rec.inv.rc}: {rec.inv.text[-400:]}"
        )
        require(
            len(rec.zips) == 1, f"case accept left {len(rec.zips)} release.zip files"
        )
        attempts = case_attempts(rec)
        require(attempts, "the accepted case run recorded no attempt")
        require(
            attempts[-1]["verdict"] == "pass" and not attempts[-1]["failed"],
            f"the released attempt did not pass cleanly: {attempts[-1]}",
        )
        require(
            {a["seed"] for a in attempts} == {KNOWN_GOOD_CASE_SEED},
            f"the control ran with another seed: {[a['seed'] for a in attempts]}",
        )
        return rec

    return cached(ctx, "case-accept-ok", make)


def case_accept(ctx: Ctx) -> RunRecord:
    """The accepted case run, or a pending block naming the root step."""
    need_case(ctx)
    try:
        return case_accept_raw(ctx)
    except StepFailure as exc:
        raise StepPending(
            f"the accepted case run must complete (root step {CASE_ROOT_STEP})",
            "blocked: " + str(exc)[:160],
        ) from exc


@contextlib.contextmanager
def source_unreachable(ctx: Ctx):
    """Rename the producer directory away while a recipient check runs."""
    hidden = ctx.fixture.directory.with_name("producer.hidden")
    ctx.fixture.directory.rename(hidden)
    try:
        require(not ctx.fixture.csv.exists(), "source still reachable")
        yield
    finally:
        hidden.rename(ctx.fixture.directory)


def verify_cli(ctx: Ctx, archive: Path, *extra: str) -> Invocation:
    inv = cli(ctx, ["verify", str(archive), *extra])
    inv.require_driver_ok()
    return inv


def verify_payload(inv: Invocation) -> dict[str, Any]:
    require(inv.rc == 0, f"verify exit {inv.rc}: {inv.text[-300:]}")
    try:
        payload = json.loads((inv.result or {}).get("stdout", ""))
    except json.JSONDecodeError as exc:
        raise StepFailure(f"verify printed no JSON result: {exc}") from exc
    require(isinstance(payload, dict), "verify result is not an object")
    return payload


def case_recipient_control(ctx: Ctx) -> dict[str, Any]:
    """The genuine accepted case package must verify with the source unreachable.

    Every case anchor and tamper control depends on this: if a genuine package
    is refused too, then refusing a tampered one proves nothing.
    """
    need_verify(ctx)
    rec = case_accept(ctx)
    with source_unreachable(ctx):
        inv = verify_cli(ctx, rec.zip_path, "--require-case", CASE_REF)
    payload = verify_payload(inv)
    binding = (payload.get("case") or {}).get("binding") or {}
    for key in ("contract_sha256", "catalogue_sha256"):
        require(
            isinstance(binding.get(key), str) and len(binding[key]) == 64,
            f"verifier reports no {key}",
        )
    ctx.hashes["case_contract_sha256"] = binding["contract_sha256"]
    ctx.hashes["case_verified_catalogue_sha256"] = binding["catalogue_sha256"]
    return {"verified_keys": sorted(payload)}


def case_recipient_root(ctx: Ctx) -> dict[str, Any]:
    return cached(ctx, "case-recipient-control", lambda: case_recipient_control(ctx))


def need_recipient_control(ctx: Ctx) -> None:
    """Pending, not fail, while the genuine package itself does not verify."""
    try:
        case_recipient_root(ctx)
    except StepFailure as exc:
        raise StepPending(
            "the genuine case package must verify first (step verify-case-anchor)",
            "blocked: " + str(exc)[:160],
        ) from exc


PROFILE_ID = "hylogger"
#: How a recipient holding ONLY the ZIP verifies a case package: the profile is
#: named explicitly (there is no manifest to read it from) and the expected case
#: is the anchor.
ZIP_ONLY_FLAGS = ("--profile", PROFILE_ID, "--require-case", CASE_REF)


def case_zip_only_control(ctx: Ctx) -> dict[str, Any]:
    """A genuine accepted case ZIP, alone in a directory with no manifest.json,
    must verify with an explicit profile and the expected-case anchor.

    Every tamper and stripped-package refusal below uses exactly these flags
    and this kind of location; without this paired control a refusal could be
    nothing more than "cannot pick a profile".
    """
    need_verify(ctx)
    rec = case_accept(ctx)
    folder = ctx.fresh("zip-only-control")
    folder.mkdir(parents=True, exist_ok=True)
    alone = folder / "release.zip"
    shutil.copy2(rec.zip_path, alone)
    harness_require(
        not (folder / "manifest.json").exists(), "the control directory has a manifest"
    )
    inv = verify_cli(ctx, alone, *ZIP_ONLY_FLAGS)
    payload = verify_payload(inv)
    binding = (payload.get("case") or {}).get("binding") or {}
    require(
        isinstance(binding.get("contract_sha256"), str),
        "the verifier reported no contract digest for the lone ZIP",
    )
    return {"contract_sha256": binding["contract_sha256"]}


def case_zip_only_root(ctx: Ctx) -> dict[str, Any]:
    return cached(ctx, "case-zip-only-control", lambda: case_zip_only_control(ctx))


def need_zip_only_control(ctx: Ctx) -> None:
    """Pending, not fail, while a genuine lone ZIP does not itself verify."""
    try:
        case_zip_only_root(ctx)
    except StepFailure as exc:
        raise StepPending(
            "a genuine lone case ZIP must verify with an explicit --profile first "
            "(step case-recipient-zip-only-verifies)",
            "blocked: " + str(exc)[:160],
        ) from exc


def scenario_case_controls(ctx: Ctx) -> Scenario:
    sc = Scenario(
        "case-controls",
        "Case controls: manifest, seed, correction, handoffs, evidence, recipient",
    )
    fx = ctx.fixture

    def manifest_pin() -> dict[str, Any]:
        need_case(ctx)
        declared = ctx.caps.get("manifest_cases", [])
        entry = next(
            (
                c
                for c in declared
                if c.get("id") == CASE_ID and c.get("version") == CASE_VERSION
            ),
            None,
        )
        require(
            entry is not None, f"manifest does not declare {CASE_REF}: {declared!r}"
        )
        catalogue = load_json(ctx.install.root / "profiles" / "hylogger" / "cases.json")
        digest = sha256_canonical(catalogue)
        require(
            entry.get("catalogue_sha256") == digest,
            f"manifest pins {entry.get('catalogue_sha256')!r} but the installed "
            f"catalogue digests to {digest!r}",
        )
        require(
            CASE_ID in catalogue.get("cases", {}), "installed catalogue lacks the case"
        )
        ctx.hashes["case_catalogue_sha256"] = digest
        return {
            "detail": "manifest pins the installed catalogue digest",
            "catalogue_sha256": digest,
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-manifest-pins-catalogue",
            "Manifest case declaration pins the installed catalogue",
            manifest_pin,
        )
    )

    def catalogue_tamper() -> dict[str, Any]:
        need_case(ctx)
        case_accept(ctx)  # the same route must work untampered, or this proves nothing
        tampered = ctx.fresh("tampered-install")
        for package in ("engine", "models", "profiles"):
            shutil.copytree(
                ctx.install.root / package,
                tampered / package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        path = tampered / "profiles" / "hylogger" / "cases.json"
        catalogue = load_json(path)
        # A free-text title change cannot trip any content validator, so only
        # the manifest's catalogue pin can catch it.
        case = catalogue["cases"][CASE_ID]
        harness_require(
            isinstance(case.get("title"), str), "catalogue case has no title"
        )
        case["title"] = case["title"] + " (tampered)"
        path.write_text(json.dumps(catalogue, indent=2, sort_keys=True) + "\n")
        draft = ctx.fresh("draft-tampered")
        argv = [
            "prepare",
            str(fx.csv),
            "--profile",
            "hylogger",
            "--case",
            CASE_REF,
            "--request",
            REQUEST_TEXT,
            "--max-attempts",
            "2",
            "--out",
            str(draft),
            *PREPARE_ARGS_TAIL,
        ]
        model = {"kind": "roles", **case_roles()}
        prep = invoke(
            ctx,
            {"mode": "cli", "argv": argv, "stdin": "", "model": model},
            root=tampered,
        )
        prep.require_driver_ok()
        check_mock_ok(prep)
        refused_at, text = "prepare", prep.text
        if prep.rc in (0, None):
            require(
                (draft / "spec.json").is_file(), "prepare succeeded but wrote no spec"
            )
            out = ctx.fresh("out-tampered")
            run_argv = [
                "run",
                str(fx.csv),
                "--profile",
                "hylogger",
                "--max-attempts",
                "2",
                "--out",
                str(out),
                "--spec",
                str(draft / "spec.json"),
            ]
            run_inv = invoke(
                ctx,
                {
                    "mode": "cli",
                    "argv": run_argv,
                    "stdin": decision_input("accept"),
                    "model": model,
                },
                root=tampered,
            )
            run_inv.require_driver_ok()
            check_mock_ok(run_inv)
            rec = collect_run("tampered", run_inv, out)
            require(
                run_inv.rc not in (0, None),
                "a run on a tampered installed catalogue was not refused",
            )
            require(not rec.receipts(), "generation started on a tampered catalogue")
            require(not rec.zips, "a release exists from a tampered catalogue")
            refused_at, text = "run", run_inv.text
        lowered = text.lower()
        require(
            "catalogue" in lowered or "pinned" in lowered or "sha" in lowered,
            f"the refusal does not cite the catalogue pin: {text[-200:]}",
        )
        return {
            "detail": f"tampered catalogue refused at {refused_at}; nothing generated or released",
            "refused_at": refused_at,
            "refusal": redact(text[-200:], hide=[ctx.work]),
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-catalogue-tamper-refused",
            "Tampered installed catalogue is refused pre-model",
            catalogue_tamper,
        )
    )

    def reroll_refused() -> dict[str, Any]:
        need_case(ctx)
        # The known-good control must release first, or a refusal proves nothing.
        case_accept(ctx)
        seed = REPAIR_CONTROL_SEED
        roles = case_roles(strategy=JOINT, seed=seed)
        pre, draft = case_prepare(ctx, fx.csv, fx.csv_sha256, roles=roles)
        pre.require_driver_ok()
        require(pre.rc == 0, f"case prepare exit {pre.rc}: {pre.text[-300:]}")
        out = ctx.fresh("out-reroll")
        inv = invoke(
            ctx,
            {
                "mode": "execute_override",
                "override": "case_repair_reroll_seed",
                "source": str(fx.csv),
                "spec": str(draft / "spec.json"),
                "out": str(out),
                "max_attempts": 2,
                "stdin": decision_input("reject"),
                "model": {"kind": "roles", **roles},
            },
        )
        inv.require_driver_ok()
        check_mock_ok(inv)
        rec = collect_run("reroll", inv, out)
        if not first_attempt_failed(rec, "case.inclusion_fraction"):
            raise StepPending(
                "the pinned repair control (joint strategy, fixed seed) failing "
                "case.inclusion_fraction on its first attempt",
                "control not exercised: the first attempt did not fail inclusion, "
                "so no repair ran and no seed change could be refused",
            )
        require(
            inv.rc not in (0, None), "a repair that rerolled the seed was not refused"
        )
        require(not rec.zips, "a release exists after a refused seed reroll")
        engine_errors = [
            json.dumps(e.get("details"), default=str).lower()
            for e in rec.events()
            if e.get("stage") == "engine" and e.get("status") == "error"
        ]
        require(
            any(
                "seed" in t and ("reroll" in t or "differs" in t) for t in engine_errors
            ),
            f"no engine error event cites the changed seed: {engine_errors}",
        )
        seeds = {r.get("seed") for r in rec.receipts()}
        require(seeds <= {seed}, f"an attempt ran with another seed: {seeds}")
        return {
            "detail": "engine refused a repair that changed the seed; no release",
            "fixed_seed": seed,
            "control": CONTROLS["repair_control"]["label"],
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-seed-reroll-refused",
            "Engine refuses a changed seed in a case run",
            reroll_refused,
        )
    )

    def correction() -> dict[str, Any]:
        need_case(ctx)
        # The known-good control must release first, or this proves nothing.
        case_accept(ctx)
        seed = REPAIR_CONTROL_SEED
        rec = case_execute(
            ctx, "correction", roles=case_roles(strategy=JOINT, seed=seed)
        )
        attempts = case_attempts(rec)
        if not first_attempt_failed(rec, "case.inclusion_fraction"):
            raise StepPending(
                "the pinned repair control (joint strategy, fixed seed) failing "
                "case.inclusion_fraction on its first attempt",
                "control not exercised: the first attempt did not fail inclusion, "
                "so there was nothing to correct",
            )
        require(
            len(attempts) == 2,
            f"expected the failed attempt plus one repair, got {len(attempts)}",
        )
        require(
            {a["seed"] for a in attempts} == {seed},
            f"seed changed: {[a['seed'] for a in attempts]}",
        )
        repair_events = [e for e in rec.events() if e.get("stage") == "case_repair"]
        require(repair_events, "no case_repair event recorded")
        require(
            all(e.get("status") == "ok" for e in repair_events),
            "the supported correction was refused",
        )
        loop = [e for e in rec.events() if e.get("stage") == "generate_validate_repair"]
        strategies = [
            (e.get("details") or {}).get("plan", {}).get("strategy") for e in loop
        ]
        artifacts = {e.get("artifact_sha256") for e in loop}
        reported = (repair_events[-1].get("details") or {}).get("strategy")
        require(
            reported == STRATEGY and strategies[-1:] == [STRATEGY],
            f"the correction did not move to {STRATEGY}: case_repair reported "
            f"{reported!r}; attempt strategies {strategies}; "
            + (
                "every attempt produced the identical artifact"
                if len(artifacts) == 1
                else "the attempts produced different artifacts"
            ),
        )
        require(strategies[:1] == [JOINT], f"attempt 1 strategy was {strategies[:1]}")
        # The label alone is not enough: the corrected attempt must actually stop
        # failing the check the correction exists to fix.
        require(
            "case.inclusion_fraction" not in attempts[1]["failed"],
            "the corrected attempt still fails case.inclusion_fraction: the "
            f"strategy label changed but the behaviour did not ({attempts[1]['failed']})",
        )
        calls = rec.inv.role_calls
        require(
            set(calls) <= {"plan", "assess", "research", "review"}
            and all(n <= 1 for n in calls.values()),
            f"repair consulted a model: {calls}",
        )
        return {
            "detail": "first attempt failed inclusion; repair moved joint to balanced with the original seed and no repair model call",
            "seed": seed,
            "attempts": attempts,
            "role_calls": calls,
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-supported-correction-same-seed",
            "Supported strategy correction keeps the original seed",
            correction,
        )
    )

    def handoffs_control() -> dict[str, Any]:
        rec = case_run(ctx, "reject")
        states = stage_status(rec)
        missing = [s for s in CASE_AI_STAGES if "ok" not in states.get(s, [])]
        require(not missing, f"AI stages not recorded ok: {missing}; saw {states}")
        calls = rec.inv.role_calls
        require(
            calls.get("assess") == 1
            and calls.get("research") == 1
            and calls.get("review") == 1,
            f"unexpected role calls: {calls}",
        )
        require(rec.inv.canary_hits == 0, "a source row reached an AI prompt")
        return {
            "detail": "checked handoffs recorded for all three roles; no source row in any prompt",
            "role_calls": calls,
        }

    def handoffs_valid() -> dict[str, Any]:
        need_case(ctx, ENGINE_AI_DEP)
        case_accept(ctx)  # pending, not fail, while the case route itself is broken
        return cached(ctx, "handoffs-control", handoffs_control)

    sc.steps.append(
        run_step(
            ctx,
            "case-ai-handoffs-accepted",
            "Valid AI handoffs are recorded and carry no source rows",
            handoffs_valid,
        )
    )

    reached_before = {
        "case_source_assess": "source_intake",
        "case_research": "case_source_assess",
        "case_candidate_review": "case_research",
    }

    def handoff_refused(
        role: str, stage: str, mutation: Mapping[str, Any], invented: str
    ) -> dict[str, Any]:
        need_case(ctx, ENGINE_AI_DEP)
        case_accept(ctx)
        try:
            cached(ctx, "handoffs-control", handoffs_control)
        except StepFailure as exc:
            raise StepPending(
                "the unmodified handoffs must be accepted first (step case-ai-handoffs-accepted)",
                "blocked: " + str(exc)[:160],
            ) from exc
        rec = case_execute(
            ctx,
            f"handoff-{role}",
            decision="accept",
            roles=case_roles(seed=KNOWN_GOOD_CASE_SEED, mutate={role: mutation}),
        )
        states = stage_status(rec)
        before = reached_before[stage]
        require(
            "ok" in states.get(before, []),
            f"the run never reached {stage} ({before}: {states.get(before)}), so this proves nothing",
        )
        require(
            "ok" not in states.get(stage, []),
            f"{stage} recorded ok for a tampered handoff: {states.get(stage)}",
        )
        require(
            rec.inv.rc not in (0, None) or states.get(stage),
            "the tampered handoff left no trace: neither a refusal nor a recorded non-ok stage",
        )
        if rec.zips:
            with zipfile.ZipFile(rec.zips[0]) as archive:
                leaked = [
                    n
                    for n in archive.namelist()
                    if invented.encode() in archive.read(n)
                ]
            require(
                not leaked, f"tampered handoff content reached the release: {leaked}"
            )
        return {
            "detail": f"{stage}: reached, tampered handoff not presented as verified",
            "exit": rec.inv.rc,
            "stage_states": states.get(stage, []),
        }

    handoffs = (
        (
            "research",
            "case_research",
            {"cited_evidence_ids": [INVENTED_EVIDENCE_ID], "passage_citations": []},
            INVENTED_EVIDENCE_ID,
        ),
        ("assess", "case_source_assess", {"source_sha256": "0" * 64}, "0" * 64),
        (
            "review",
            "case_candidate_review",
            {
                "code_verdict_acknowledged": "fail",
                "operator_recommendation": "recommend_reject",
            },
            "recommend_reject",
        ),
    )
    for role, stage, mutation, invented in handoffs:
        sc.steps.append(
            run_step(
                ctx,
                f"case-handoff-{role}-refused",
                f"Tampered {role} handoff is not presented as verified",
                lambda r=role, s=stage, m=mutation, i=invented: handoff_refused(
                    r, s, m, i
                ),
            )
        )

    def accept_root() -> dict[str, Any]:
        need_case(ctx)
        rec = case_accept_raw(ctx)
        return {
            "detail": "accepted case run exited 0 and left one release.zip",
            "attempts": len(rec.receipts()),
        }

    sc.steps.append(
        run_step(
            ctx,
            CASE_ROOT_STEP,
            "Accepted case run completes and releases once",
            accept_root,
        )
    )

    def quality_failure_blocks_release() -> dict[str, Any]:
        need_case(ctx)
        case_accept(ctx)  # the known-good control releases; this one must not
        rec = case_execute(
            ctx,
            "quality-failure",
            decision="accept",
            roles=case_roles(seed=KNOWN_QUALITY_FAILURE_SEED),
            max_attempts=1,
        )
        attempts = case_attempts(rec)
        case_failed = [
            c
            for c in (attempts[0]["failed"] if attempts else [])
            if c.startswith("case.")
        ]
        if not case_failed:
            raise StepPending(
                "the pinned quality-failure control (fixed seed) failing a real case check",
                "control not exercised: the pinned plan no longer fails a case check, "
                "so nothing here shows a quality failure staying a failure",
            )
        require(rec.inv.rc == 1, f"expected a failed run (exit 1), got {rec.inv.rc}")
        require(not rec.zips, "a release exists although a case check failed")
        require(
            rec.decision() is None, "the human gate was reached despite a failing check"
        )
        require(
            stage_status(rec).get("human_review") == ["skipped"],
            f"the gate was not skipped: {stage_status(rec).get('human_review')}",
        )
        return {
            "detail": "a real case-check failure blocked the gate and any release, despite supplied 'accept' input",
            "failed_case_checks": case_failed,
            "fixed_seed": KNOWN_QUALITY_FAILURE_SEED,
            "control": CONTROLS["known_quality_failure"]["label"],
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-quality-failure-blocks-release",
            "A real case-check failure blocks the gate and release",
            quality_failure_blocks_release,
        )
    )

    def frozen_case() -> dict[str, Any]:
        rec = case_accept(ctx)
        with zipfile.ZipFile(rec.zip_path) as archive:
            members = {n: archive.read(n) for n in archive.namelist()}
        bound = check_acceptance_binding(rec, members)
        wanted = set(ctx.caps.get("case_member_names", []))
        require(wanted, "profile exposes no CASE_MEMBER_NAMES")
        require(
            wanted <= set(bound["members"]),
            f"case members not frozen before the decision: {sorted(wanted - set(bound['members']))}",
        )
        advisory = (
            members.get("evidence-report.md", b"").decode("utf-8", "replace").lower()
        )
        require(
            "advisory" in advisory,
            "evidence report does not separate advisory AI output from code checks",
        )
        ctx.hashes["case_release_zip_sha256"] = file_digest(rec.zip_path)
        ctx.hashes["case_decision_bound_digest"] = bound["bound"]
        return {
            "detail": "case evidence frozen before the decision and released byte-identical",
            "members": bound["members"],
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-frozen-evidence-bound",
            "Case evidence is frozen, bound by the decision and advisory-separated",
            frozen_case,
        )
    )

    def recipient_source_free() -> dict[str, Any]:
        need_recipient_control(ctx)
        return {
            "detail": "recipient verified the case package with the source unreachable",
            "control": "shared with verify-case-anchor",
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-recipient-verifies-source-free",
            "Recipient verifies the case package without the source",
            recipient_source_free,
        )
    )

    def recipient_zip_only() -> dict[str, Any]:
        # The recipient situation: only the ZIP, no adjacent manifest.json.
        info = case_zip_only_root(ctx)
        return {
            "detail": "a genuine case ZIP alone in a directory verifies with an explicit profile and the expected-case anchor",
            "contract_sha256": info["contract_sha256"],
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-recipient-zip-only-verifies",
            "A lone case ZIP verifies with an explicit profile and the case anchor",
            recipient_zip_only,
        )
    )

    def recipient_anchors() -> dict[str, Any]:
        need_verify(ctx)
        need_recipient_control(ctx)
        rec = case_accept(ctx)
        contract = ctx.hashes.get("case_contract_sha256")
        declared = ctx.caps.get("manifest_cases", [{}])[0].get("catalogue_sha256")
        require(
            contract and declared,
            "anchors unavailable: the source-free verification step did not pass",
        )
        ok = verify_cli(
            ctx,
            rec.zip_path,
            "--require-case",
            CASE_REF,
            "--expect-contract-sha256",
            contract,
            "--expect-catalogue-sha256",
            declared,
        )
        require(ok.rc == 0, f"correct anchors refused: {ok.text[-200:]}")
        wrong = {
            "wrong-contract": [
                "--require-case",
                CASE_REF,
                "--expect-contract-sha256",
                "0" * 64,
            ],
            "wrong-catalogue": [
                "--require-case",
                CASE_REF,
                "--expect-catalogue-sha256",
                "0" * 64,
            ],
            "wrong-case-id": ["--require-case", f"other_case@{CASE_VERSION}"],
            "wrong-version": ["--require-case", f"{CASE_ID}@{CASE_VERSION + 1}"],
        }
        outcomes = {}
        for label, args in wrong.items():
            inv = verify_cli(ctx, rec.zip_path, *args)
            require(inv.rc not in (0, None), f"{label}: a wrong anchor was accepted")
            outcomes[label] = "refused"
        return {
            "detail": "correct external anchors verify; four wrong anchors are refused",
            "outcomes": outcomes,
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-recipient-anchors",
            "External contract, catalogue, case id and version anchors bind",
            recipient_anchors,
        )
    )

    def recipient_source_rederivation() -> dict[str, Any]:
        need_verify(ctx)
        need_recipient_control(ctx)
        rec = case_accept(ctx)
        good = verify_cli(
            ctx, rec.zip_path, "--require-case", CASE_REF, "--source", str(fx.csv)
        )
        require(
            good.rc == 0,
            f"verification with the true source refused: {good.text[-200:]}",
        )
        other = verify_cli(
            ctx,
            rec.zip_path,
            "--require-case",
            CASE_REF,
            "--source",
            str(fx.unsuitable_csv),
        )
        require(
            other.rc not in (0, None),
            "a different source was accepted for source re-derivation",
        )
        return {
            "detail": "optional source re-derivation accepts the true source and refuses another"
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-recipient-source-rederivation",
            "Optional source re-derivation binds the true source",
            recipient_source_rederivation,
        )
    )

    def stripped_lone_zip() -> dict[str, Any]:
        # The case release carries no manifest.json, so there is nothing in it to
        # rewrite. A stripped, re-cut lone ZIP is detectable only through the
        # expected-case anchor. The paired genuine control (same flags, same
        # kind of location) must verify first, or a refusal proves nothing.
        need_verify(ctx)
        need_zip_only_control(ctx)
        rec = case_accept(ctx)
        with zipfile.ZipFile(rec.zip_path) as archive:
            members = {n: archive.read(n) for n in archive.namelist()}
        names = set(ctx.caps.get("case_member_names", []))
        harness_require(names, "profile exposes no CASE_MEMBER_NAMES")
        dest = ctx.fresh("case-stripped-lone") / "release.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        recut_zip(members, {n: None for n in names}, dest)
        anchored = verify_cli(ctx, dest, *ZIP_ONLY_FLAGS)
        require(
            anchored.rc not in (0, None),
            "a stripped lone ZIP passed with an explicit profile and the expected-case anchor",
        )
        bare = verify_cli(ctx, dest, "--profile", PROFILE_ID)
        ctx.observations.append(
            {
                "id": "stripped-case-without-anchor",
                "observed": "accepted" if bare.rc == 0 else "refused",
                "meaning": "a stripped, re-cut lone ZIP is only detectable with --require-case or another external anchor",
            }
        )
        return {
            "detail": "stripped lone ZIP refused with the expected-case anchor",
            "without_anchor": "accepted" if bare.rc == 0 else "refused",
            "tampered_zip_sha256": file_digest(dest),
        }

    sc.steps.append(
        run_step(
            ctx,
            "case-stripped-lone-zip-needs-the-anchor",
            "A stripped lone ZIP needs and meets the expected-case anchor",
            stripped_lone_zip,
        )
    )
    return sc


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

LIMITATIONS = (
    "Model responses are scripted mocks (ENGINEERING-TEST-MOCK). Nothing here "
    "measures any real model, and no provider, paid or network call is made.",
    "The source is an invented fixture. Its sidecar licence field exists only "
    "to satisfy source-rights intake and implies no real licence or origin.",
    "Automated gate input is an engineering test, not owner approval. The "
    "engine records decided_by as human for any piped input, so the label is "
    "carried only by the note.",
    "The wheel is built from the working tree, including uncommitted changes "
    "from other workers; revision.tree_digest covers their contents. A run is "
    "valid only if the commit and tree digest are identical at the start, "
    "after the build and at the end; otherwise it is inconclusive.",
    "The role mock reads identity facts (case id, hashes, code verdict) from "
    "the real prompts so a valid handoff exists, and changes one field for "
    "each negative control. It validates and decides nothing.",
    "The case controls use three fixed, disclosed seeds (fixtures.controls). "
    "The known-good seed was first observed to pass in exploratory runs and is "
    "pinned, not derived by the harness; the harness never searches for a seed "
    "or tunes a threshold, so a quality failure stays a failure.",
    "The seed-reroll refusal swaps one profile capability for a misbehaving "
    "one inside the real engine; it proves the engine guard, not that the "
    "shipped repair is the only way a seed could change.",
    "Network blocking is an in-process socket guard in the driver, not an "
    "operating-system sandbox.",
    "Tamper edits use the engine's own packaging helpers from the repository "
    "tree; the verifier under test runs from the installed layout.",
    "A re-cut package with no external anchor is only as strong as its "
    "internal checksums; the measured outcome is listed under observations.",
    "This harness is not Test 1, not the Gemini/Claude comparison and not a "
    "Muse or Test 2 run; it proves no scientific, privacy or licensing claim.",
)


#: Every scenario that strict mode requires to pass. A scenario that did not
#: run at all counts as skipped, which strict mode also refuses.
REQUIRED_SCENARIO_IDS = (
    "prerequisites",
    "request-preparation",
    "generation-checks-repair",
    "operator-decision",
    "recipient-verification",
    "tamper-and-missing-evidence",
    "offline-replay",
    "case-controls",
)


def summarize(scenarios: Sequence[Scenario]) -> dict[str, Any]:
    steps = [s for sc in scenarios for s in sc.steps]
    counts = {PASS: 0, FAIL: 0, PENDING: 0}
    faults = {FAULT_PRODUCT: 0, FAULT_HARNESS: 0}
    for step in steps:
        counts[step.status] = counts.get(step.status, 0) + 1
        if step.status == FAIL:
            # An unclassified failure is treated as a product failure.
            key = FAULT_HARNESS if step.fault == FAULT_HARNESS else FAULT_PRODUCT
            faults[key] += 1
    overall = combine([sc.status for sc in scenarios]) if scenarios else PENDING
    return {
        "overall": overall,
        "step_counts": counts,
        "scenario_count": len(scenarios),
        "product_failures": faults[FAULT_PRODUCT],
        "harness_errors": faults[FAULT_HARNESS],
    }


def verdict_kind(summary: Mapping[str, Any], *, leak: bool = False) -> str:
    """Name what the result means, so a harness error is not read as a product defect."""
    if summary["overall"] == INCONCLUSIVE:
        return "inconclusive"
    if leak:
        return "report_hygiene_failure"
    if summary.get("product_failures", 0):
        return "product_failure"
    if summary.get("harness_errors", 0):
        return "harness_error"
    if summary["overall"] == PENDING:
        return "incomplete"
    return "pass" if summary["overall"] == PASS else "fail"


#: Product findings that stay OPEN until the implementation workers fix them and
#: a strict run on the pinned revision shows the matching step passing. The
#: harness does not soften, skip or special-case these steps.
OPEN_FINDINGS = (
    {
        "id": "F1",
        "title": "Recipient verification expects manifest.json inside a ZIP that does not contain it",
        "scenario": "recipient-verification",
        "step": "verify-case-anchor",
    },
    {
        "id": "F2",
        "title": "Integrated repair reports success but leaves the joint strategy unchanged",
        "scenario": "case-controls",
        "step": "case-supported-correction-same-seed",
    },
    {
        "id": "F3",
        "title": "Unknown-case preparation calls the model before refusing",
        "scenario": "request-preparation",
        "step": "case-prepare-unknown-case-zero-calls",
    },
)


def findings_state(scenarios: Sequence[Scenario]) -> list[dict[str, Any]]:
    """Where each open finding stands, read from its own step's real result."""
    steps = {(sc.id, st.id): st for sc in scenarios for st in sc.steps}
    states = []
    for finding in OPEN_FINDINGS:
        step = steps.get((finding["scenario"], finding["step"]))
        if step is None:
            state = "unverified: the step did not run"
            status = "not-run"
        elif step.status == FAIL:
            state = "open: reproduced"
            status = FAIL
        elif step.status == PASS:
            state = "not reproduced: candidate fix, confirm on the pinned revision"
            status = PASS
        else:
            state = "unverified: blocked"
            status = step.status
        states.append({**finding, "step_status": status, "state": state})
    return states


def strict_evaluation(report: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    """Strict gate: every required scenario must exist and pass, with no
    failed, skipped or pending step, on a stable revision."""
    reasons: list[str] = []
    by_id = {sc["id"]: sc for sc in report["scenarios"]}
    for sid in REQUIRED_SCENARIO_IDS:
        scenario = by_id.get(sid)
        if scenario is None:
            reasons.append(f"required scenario {sid} was skipped (it did not run)")
            continue
        if scenario["status"] != PASS:
            reasons.append(f"required scenario {sid} is {scenario['status']}")
        for step in scenario["steps"]:
            if step["status"] != PASS:
                fault = f" ({step.get('fault')} fault)" if step.get("fault") else ""
                reasons.append(f"  step {sid}/{step['id']} is {step['status']}{fault}")
    summary = report["summary"]
    if summary["overall"] == INCONCLUSIVE:
        reasons.append("the run is inconclusive: the revision moved during it")
    if report.get("hygiene", {}).get("credential_shaped_text_found"):
        reasons.append("the report contains credential-shaped text")
    if summary["overall"] != PASS and not reasons:
        reasons.append(f"overall result is {summary['overall']}")
    return {
        "enabled": enabled,
        "passed": not reasons,
        "required_scenarios": list(REQUIRED_SCENARIO_IDS),
        "reasons": reasons[:200],
    }


def pending_dependencies(scenarios: Sequence[Scenario]) -> list[str]:
    seen: list[str] = []
    for sc in scenarios:
        for step in sc.steps:
            if (
                step.status == PENDING
                and step.dependency
                and step.dependency not in seen
            ):
                seen.append(step.dependency)
    return seen


def build_report(
    *,
    scenarios: Sequence[Scenario],
    ctx: Ctx | None,
    revision: Mapping[str, Any],
    layout: Mapping[str, Any],
    started: str,
    elapsed_s: float,
    stability: Mapping[str, Any] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    hashes: dict[str, str] = dict(ctx.hashes) if ctx else {}
    if ctx:
        hashes.update(
            fixture_csv_sha256=ctx.fixture.csv_sha256,
            fixture_sidecar_sha256=ctx.fixture.sidecar_sha256,
            fixture_unsuitable_csv_sha256=ctx.fixture.unsuitable_sha256,
        )
        if ctx.install.wheel_sha256:
            hashes["wheel_sha256"] = ctx.install.wheel_sha256
    hashes["harness_script_sha256"] = file_digest(Path(__file__))
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "purpose": "engineering acceptance test; not owner approval",
        "started_at": started,
        "elapsed_s": round(elapsed_s, 3),
        "revision": dict(revision),
        "layout": dict(layout),
        "environment": {
            "controller_python": sys.version.split()[0],
            "driver_python": ctx.caps.get("interpreter") if ctx else None,
            "platform": sys.platform,
            "policy": env_policy(),
        },
        "fixtures": {
            "label": FIXTURE_LABEL,
            "mock_model_label": MOCK_LABEL,
            "gate_note": ENGINEERING_NOTE,
            "case": CASE_REF,
            "controls": CONTROLS,
        },
        "findings": findings_state(scenarios),
        "capabilities": ctx.caps if ctx else {},
        "model_calls_total": ctx.model_calls_total if ctx else 0,
        "summary": summarize(scenarios),
        "scenarios": [sc.to_dict() for sc in scenarios],
        "pending_dependencies": pending_dependencies(scenarios),
        "observations": list(ctx.observations) if ctx else [],
        "hashes": hashes,
        "limitations": list(LIMITATIONS),
    }
    stable = True if stability is None else bool(stability.get("stable"))
    report["stability"] = (
        dict(stability)
        if stability is not None
        else {"stable": True, "note": "stability was not tracked for this report"}
    )
    if not stable:
        # A moved commit or working tree means the evidence mixes revisions:
        # keep what was observed, but no verdict is valid.
        report["summary"]["observed_overall"] = report["summary"]["overall"]
        report["summary"]["overall"] = INCONCLUSIVE
    text = json.dumps(report, sort_keys=True)
    leak = contains_secret(text)
    report["hygiene"] = {"credential_shaped_text_found": leak}
    if leak:
        report["summary"]["overall"] = FAIL
    report["summary"]["verdict_kind"] = verdict_kind(report["summary"], leak=leak)
    report["strict"] = strict_evaluation(report, enabled=strict)
    return report


def blocked_scenarios(reason: str, dependency: str) -> list[Scenario]:
    """Remaining scenarios pending while a prerequisite is failing."""
    names = (
        ("request-preparation", "Request preparation and checked AI handoffs"),
        ("generation-checks-repair", "Generation, practical checks and bounded repair"),
        ("operator-decision", "Operator rejection and exact-file acceptance"),
        (
            "recipient-verification",
            "Downloaded-package verification without the source",
        ),
        (
            "tamper-and-missing-evidence",
            "Tampered, missing and stripped packages are refused",
        ),
        ("offline-replay", "Offline replay yields identical data with no model calls"),
        (
            "case-controls",
            "Case controls: manifest, seed, correction, handoffs, evidence, recipient",
        ),
    )
    return [
        Scenario(
            sid,
            title,
            [
                Step(
                    f"{sid}-blocked",
                    "Scenario could not start",
                    PENDING,
                    reason,
                    dependency,
                )
            ],
        )
        for sid, title in names
    ]


SCENARIOS: tuple[Callable[[Ctx], Scenario], ...] = (
    scenario_request_preparation,
    scenario_generation,
    scenario_operator,
    scenario_recipient,
    scenario_tamper,
    scenario_replay,
    scenario_case_controls,
)


def run_acceptance(
    layout: str = "wheel",
    keep_work: Path | None = None,
    python: str | None = None,
    *,
    strict: bool = False,
    expect_head: str | None = None,
    expect_tree_digest: str | None = None,
) -> dict[str, Any]:
    """Run every scenario and return the machine-readable report.

    The commit and a content digest of the working tree are recorded at the
    start, after the wheel build and at the end. If either differs (or git
    cannot say), the run is ``inconclusive``. ``expect_head`` and
    ``expect_tree_digest`` pin the revision the owner declared ready: a
    different starting revision is inconclusive before anything runs.
    """
    interpreter_path = python or sys.executable
    started = datetime.now(UTC).isoformat(timespec="seconds")
    clock = time.monotonic()
    start_snapshot = tree_snapshot("start")
    snapshots = [start_snapshot]
    revision = revision_info(start_snapshot)
    expected = {"git_head": expect_head, "tree_digest": expect_tree_digest}
    mismatch = [
        f"{key}: expected {want}, found {start_snapshot[key]}"
        for key, want in expected.items()
        if want and start_snapshot[key] != want
    ]

    def stability() -> dict[str, Any]:
        snapshots.append(tree_snapshot("end"))
        result = compare_snapshots(snapshots)
        result["expected"] = expected
        result["expected_mismatch"] = mismatch
        if mismatch:
            result["stable"] = False
        return result

    if mismatch:
        return build_report(
            scenarios=blocked_scenarios(
                "blocked: the starting revision is not the one declared ready",
                "the integrated revision the owner named (commit and tree digest)",
            ),
            ctx=None,
            revision=revision,
            layout={"requested": layout, "error": "revision mismatch"},
            started=started,
            elapsed_s=time.monotonic() - clock,
            stability=stability(),
            strict=strict,
        )
    work_root = (
        Path(os.path.abspath(keep_work))
        if keep_work
        else Path(tempfile.mkdtemp(prefix="ruloom-acceptance-"))
    )
    work_root.mkdir(parents=True, exist_ok=True)
    ctx: Ctx | None = None
    try:
        try:
            install = build_install(work_root, layout, interpreter_path)
        except InstallError as exc:
            reason = redact(str(exc), hide=[work_root])
            failed = Scenario(
                "prerequisites",
                "Layout and interpreter can run the real workflow",
                [
                    Step(
                        "layout-prepared",
                        "Wheel (or tree) layout prepared",
                        FAIL,
                        reason,
                    )
                ],
            )
            scenarios = [
                failed,
                *blocked_scenarios(
                    "blocked: the requested layout could not be prepared",
                    "a runnable layout (wheel build with hatchling, or --layout tree)",
                ),
            ]
            return build_report(
                scenarios=scenarios,
                ctx=None,
                revision=revision,
                layout={"requested": layout, "error": reason},
                started=started,
                elapsed_s=time.monotonic() - clock,
                stability=stability(),
                strict=strict,
            )
        snapshots.append(tree_snapshot("after-build"))
        ctx = Ctx(
            work=work_root,
            install=install,
            fixture=build_fixture(work_root),
            python=interpreter_path,
        )
        ctx.caps = probe_capabilities(ctx)
        gate = scenario_prerequisites(ctx)
        if gate.status == PASS:
            scenarios = [gate, *[factory(ctx) for factory in SCENARIOS]]
        else:
            scenarios = [
                gate,
                *blocked_scenarios(
                    "blocked: prerequisites failed; see the prerequisites scenario",
                    INTERPRETER_DEP,
                ),
            ]
        layout_info = {
            "requested": layout,
            "kind": install.kind,
            "wheel": install.wheel_name,
            "console_script": install.console_script,
            "strength": "installed wheel outside the repository"
            if install.kind == "wheel"
            else "source tree (weaker: no packaging evidence)",
        }
        return build_report(
            scenarios=scenarios,
            ctx=ctx,
            revision=revision,
            layout=layout_info,
            started=started,
            elapsed_s=time.monotonic() - clock,
            stability=stability(),
            strict=strict,
        )
    finally:
        if keep_work is None:
            shutil.rmtree(work_root, ignore_errors=True)


def exit_code(report: Mapping[str, Any]) -> int:
    """Map a report to a process exit code.

    0 pass; 1 product failure (or any strict failure); 3 pending only;
    5 inconclusive (the revision moved); 7 harness errors only.
    """
    summary = report["summary"]
    overall = summary["overall"]
    harness_only = summary.get("verdict_kind") == "harness_error"
    if overall == INCONCLUSIVE:
        return EXIT_INCONCLUSIVE
    strict = report.get("strict") or {}
    if strict.get("enabled"):
        if strict.get("passed"):
            return EXIT_PASS
        return EXIT_HARNESS_ERROR if harness_only else EXIT_FAIL
    if overall == PASS:
        return EXIT_PASS
    if overall == PENDING:
        return EXIT_PENDING
    return EXIT_HARNESS_ERROR if harness_only else EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="full_workflow_acceptance",
        description="Opt-in full-workflow acceptance harness (engineering test).",
    )
    parser.add_argument(
        "--run", action="store_true", help=f"opt in (or set {OPT_IN_ENV}=1)"
    )
    parser.add_argument(
        "--print-revision",
        action="store_true",
        help="print the current commit and working-tree digest (for --expect-head "
        "and --expect-tree-digest) and exit; reads git only, runs nothing",
    )
    parser.add_argument("--layout", choices=("wheel", "tree"), default="wheel")
    parser.add_argument(
        "--report", default=None, help="write the JSON report here (must not exist)"
    )
    parser.add_argument(
        "--keep-work", default=None, help="keep the work directory here"
    )
    parser.add_argument(
        "--python",
        default=None,
        help="interpreter for the driver and wheel build (default: this one); "
        "relative paths are taken from the repository root, e.g. .venv/bin/python",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=f"final gate: fail unless every required scenario passes (or set {STRICT_ENV}=1)",
    )
    parser.add_argument(
        "--report-dir",
        default=None,
        help="write each report to a new timestamped file here and append to "
        "index.jsonl; earlier reports are never overwritten",
    )
    parser.add_argument(
        "--expect-head",
        default=None,
        help="the commit declared ready; a different start is inconclusive",
    )
    parser.add_argument(
        "--expect-tree-digest",
        default=None,
        help="the working-tree digest declared ready; a different start is inconclusive",
    )
    return parser


def resolve_python(value: str | None) -> str | None:
    """Absolute interpreter path without following venv symlinks."""
    if value is None:
        return None
    path = Path(value)
    return os.path.abspath(path if path.is_absolute() else ROOT / path)


def write_preserved_report(directory: Path, report: Mapping[str, Any]) -> Path:
    """Write a report to a NEW file and append it to ``index.jsonl``.

    Files are created exclusively and never overwritten, so every earlier
    report survives; the index is append-only.
    """
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    revision = report.get("revision", {})
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = (
        f"report-{stamp}-{str(revision.get('git_head', ''))[:8] or 'nohead'}-"
        f"{str(revision.get('tree_digest', ''))[:8] or 'notree'}"
    )
    for attempt in range(1000):
        path = directory / (
            f"{base}.json" if attempt == 0 else f"{base}-{attempt}.json"
        )
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            continue
        break
    else:
        raise OSError("could not create a unique report file name")
    summary = report["summary"]
    entry = {
        "file": path.name,
        "report_sha256": sha256_bytes(text.encode()),
        "started_at": report.get("started_at"),
        "overall": summary["overall"],
        "observed_overall": summary.get("observed_overall"),
        "verdict_kind": summary.get("verdict_kind"),
        "strict_passed": (report.get("strict") or {}).get("passed"),
        "stable": (report.get("stability") or {}).get("stable"),
        "git_head": revision.get("git_head"),
        "tree_digest": revision.get("tree_digest"),
    }
    with open(directory / "index.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.print_revision:
        snapshot = tree_snapshot("print-revision")
        print(json.dumps(public_snapshot(snapshot), indent=2, sort_keys=True))
        return EXIT_PASS if snapshot["available"] else EXIT_FAIL
    if not (args.run or os.environ.get(OPT_IN_ENV) == "1"):
        print(
            f"not run: this harness is opt-in. Pass --run or set {OPT_IN_ENV}=1.",
            file=sys.stderr,
        )
        return EXIT_NOT_OPTED_IN
    report_path = Path(args.report) if args.report else None
    if report_path is not None and report_path.exists():
        print(
            f"error: {report_path} already exists; refusing to overwrite",
            file=sys.stderr,
        )
        return 2
    strict = bool(args.strict or os.environ.get(STRICT_ENV) == "1")
    report = run_acceptance(
        args.layout,
        Path(args.keep_work) if args.keep_work else None,
        resolve_python(args.python),
        strict=strict,
        expect_head=args.expect_head,
        expect_tree_digest=args.expect_tree_digest,
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    written: list[Path] = []
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "x", encoding="utf-8") as handle:
            handle.write(text)
        written.append(report_path)
    if args.report_dir:
        written.append(write_preserved_report(Path(args.report_dir), report))
    summary = report["summary"]
    print(
        f"overall={summary['overall']} kind={summary.get('verdict_kind')} "
        f"steps={summary['step_counts']} product_failures={summary['product_failures']} "
        f"harness_errors={summary['harness_errors']} "
        f"stable={report['stability'].get('stable')}"
    )
    for sc in report["scenarios"]:
        print(f"  [{sc['status']}] {sc['id']}")
        for step in sc["steps"]:
            marker = f" ({step['fault']} fault)" if step.get("fault") else ""
            print(f"      {step['status']:<7} {step['id']}{marker}")
    print("open findings:")
    for finding in report["findings"]:
        print(f"    {finding['id']} {finding['state']} ({finding['step']})")
    if report["strict"]["enabled"]:
        verdict = "PASSED" if report["strict"]["passed"] else "FAILED"
        print(f"strict gate: {verdict}")
        for reason in report["strict"]["reasons"][:40]:
            print(f"    {reason}")
    if not written:
        sys.stdout.write(text)
    for path in written:
        print(f"report: {path}")
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
