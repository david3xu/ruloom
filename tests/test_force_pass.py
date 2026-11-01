"""Tests for the executable force-pass gate (scripts/force_pass.py).

Real-tree checks must pass; tmp copies with planted violations must fail:
a cross-boundary import, a simulated tracked data/ path, and a module
missing from the docs.
"""

import importlib.util
import subprocess
import sys
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


# --- real tree: everything passes -------------------------------------------


def test_real_tree_doc1() -> None:
    ok, _ = gate.check_doc1(REPO_ROOT)
    assert ok


def test_real_tree_doc2() -> None:
    ok, _ = gate.check_doc2(REPO_ROOT)
    assert ok


def test_real_tree_doc4() -> None:
    ok, _ = gate.check_doc4(REPO_ROOT)
    assert ok


def test_real_tree_code2() -> None:
    ok, _ = gate.check_code2(REPO_ROOT)
    assert ok


def test_real_tree_code3() -> None:
    ok, _ = gate.check_code3(REPO_ROOT)
    assert ok


def test_real_tree_code4() -> None:
    ok, _ = gate.check_code4(REPO_ROOT)
    assert ok


def test_real_tree_code5() -> None:
    ok, _ = gate.check_code5(REPO_ROOT)
    assert ok


def test_real_tree_code6() -> None:
    ok, _ = gate.check_code6(REPO_ROOT)
    assert ok


def test_real_tree_code7() -> None:
    ok, _ = gate.check_code7(REPO_ROOT)
    assert ok


def test_real_tree_code8() -> None:
    ok, _ = gate.check_code8(REPO_ROOT)
    assert ok


def test_real_tree_code9() -> None:
    ok, evidence = gate.check_code9(REPO_ROOT)
    assert ok, evidence


def test_real_tree_code10() -> None:
    ok, evidence = gate.check_code10(REPO_ROOT)
    assert ok, evidence


def test_script_entry_point_passes() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/force_pass.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 12
    assert all("PASS" in line for line in lines)


# --- planted violations must fail --------------------------------------------


def test_planted_boundary_import_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "profiles/hylogger/evil.py",
        "from models.meta_cli import MuseCLIModel\n",
    )
    ok, evidence = gate.check_code2(tmp_path)
    assert not ok
    assert "evil.py" in evidence


def test_planted_engine_boundary_import_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "engine/evil.py",
        "from models.meta_cli import MuseCLIModel\n",
    )
    _write(tmp_path, "engine/cli/__init__.py", "VALUE = 1\n")
    ok, _ = gate.check_code2(tmp_path)
    assert not ok


def test_allowed_models_base_import_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "profiles/hylogger/generation/plan.py",
        "from models.base import StructuredModel\n",
    )
    _write(
        tmp_path,
        "profiles/tabular/generation/repair.py",
        "from models.base import StructuredModel\n",
    )
    _write(tmp_path, "profiles/__init__.py", "")
    ok, _ = gate.check_code2(tmp_path)
    assert ok


def test_flat_generation_import_still_fails(tmp_path: Path) -> None:
    # models.base is allowed only in generation/plan.py|repair.py, so the
    # same import directly under the profile root must fail.
    _write(
        tmp_path,
        "profiles/hylogger/plan.py",
        "from models.base import StructuredModel\n",
    )
    _write(tmp_path, "profiles/__init__.py", "")
    ok, _ = gate.check_code2(tmp_path)
    assert not ok


def test_planted_tracked_data_path_fails() -> None:
    ok, evidence = gate.check_code5(
        REPO_ROOT, tracked=["engine/cli.py", "data/sources/PDD446-scalars.csv"]
    )
    assert not ok
    assert "data/" in evidence


def test_planted_missing_gitignore_rule_fails(tmp_path: Path) -> None:
    _write(tmp_path, ".gitignore", "__pycache__/\n")
    ok, _ = gate.check_code5(tmp_path, tracked=["engine/cli.py"])
    assert not ok


def test_planted_missing_doc_entry_fails(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Ruloom\n\nNo modules named here.\n")
    _write(tmp_path, "profiles/hylogger/profile.json", "{}\n")
    _write(tmp_path, "profiles/hylogger/generation/fram.py", "VALUE = 1\n")
    ok, evidence = gate.check_doc2(tmp_path)
    assert not ok
    assert "fram" in evidence


def test_planted_missing_layout_path_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        "## Layout\n\n| Path | What |\n|---|---|\n| `nope/` | missing |\n",
    )
    ok, _ = gate.check_doc1(tmp_path)
    assert not ok


def test_planted_unmeasured_claim_fails(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Ruloom\n\nResults TODO: measure later.\n")
    ok, _ = gate.check_doc4(tmp_path)
    assert not ok


def test_planted_sha256_helper_fails(tmp_path: Path) -> None:
    _write(tmp_path, "engine/artifacts/hashing.py", "def sha256_bytes(p): ...\n")
    _write(
        tmp_path,
        "profiles/hylogger/evil.py",
        "import hashlib\n\ndef sha256_hex(payload):\n    return hashlib.sha256(payload).hexdigest()\n",
    )
    ok, evidence = gate.check_code3(tmp_path)
    assert not ok
    assert "evil.py" in evidence


def test_plain_hashlib_call_is_not_a_helper(tmp_path: Path) -> None:
    # One-line stdlib digest uses are uses, not helper definitions.
    _write(tmp_path, "engine/artifacts/hashing.py", "def sha256_bytes(p): ...\n")
    _write(
        tmp_path,
        "profiles/hylogger/ok.py",
        "import hashlib\n\nDIGEST = hashlib.sha256(b'x').hexdigest()\n",
    )
    ok, _ = gate.check_code3(tmp_path)
    assert ok


def test_planted_missing_mirror_test_fails(tmp_path: Path) -> None:
    _write(tmp_path, "profiles/hylogger/profile.json", "{}\n")
    _write(tmp_path, "profiles/hylogger/generation/fram.py", "VALUE = 1\n")
    _write(tmp_path, "tests/profiles/hylogger/test_other.py", "def test_x(): assert True\n")
    ok, evidence = gate.check_code4(tmp_path)
    assert not ok
    assert "test_fram.py" in evidence


def test_planted_flat_feature_module_fails(tmp_path: Path) -> None:
    _write(tmp_path, "engine/__init__.py", "")
    _write(tmp_path, "engine/extra.py", "VALUE = 1\n")
    _write(tmp_path, "profiles/hylogger/profile.json", "{}\n")
    _write(tmp_path, "profiles/hylogger/__init__.py", "")
    _write(tmp_path, "profiles/hylogger/capabilities.py", "")
    _write(tmp_path, "profiles/hylogger/stray.py", "VALUE = 1\n")
    ok, evidence = gate.check_code8(tmp_path)
    assert not ok
    assert "engine/extra.py" in evidence
    assert "profiles/hylogger/stray.py" in evidence


def test_planted_engine_domain_word_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "engine/evil.py",
        'KIND = "hylogger"\n',
    )
    ok, evidence = gate.check_code6(tmp_path)
    assert not ok
    assert "evil.py" in evidence


def test_engine_comment_naming_rule_passes(tmp_path: Path) -> None:
    # Comments may name the rule itself; only code carries the ban.
    _write(
        tmp_path,
        "engine/ok.py",
        "# FP-CODE-6: this comment names the rule, not the domain\nVALUE = 1\n",
    )
    ok, _ = gate.check_code6(tmp_path)
    assert ok


def test_planted_fourth_node_kind_fails(tmp_path: Path) -> None:
    import json as _json

    _write(
        tmp_path,
        "profiles/fake/profile.json",
        _json.dumps(
            {
                "id": "fake",
                "version": 1,
                "engine_api": 1,
                "source": {},
                "capabilities": {"a": {"impl": "fake.a"}},
                "graph": [{"branch": "a"}],
                "checks": [
                    {
                        "id": "c",
                        "kind": "constraint",
                        "threshold": {"source": "literal"},
                    }
                ],
            }
        ),
    )
    ok, evidence = gate.check_code7(tmp_path)
    assert not ok
    assert "step/loop/gate" in evidence


def test_planted_loop_default_fails(tmp_path: Path) -> None:
    import json as _json

    _write(
        tmp_path,
        "profiles/fake/profile.json",
        _json.dumps(
            {
                "id": "fake",
                "version": 1,
                "engine_api": 1,
                "source": {},
                "capabilities": {
                    "a": {"impl": "fake.a"},
                    "b": {"impl": "fake.b"},
                },
                "graph": [
                    {"loop": ["a"], "on_fail": "b", "max_attempts": 3},
                ],
                "checks": [
                    {
                        "id": "c",
                        "kind": "constraint",
                        "threshold": {"source": "literal"},
                    }
                ],
            }
        ),
    )
    ok, evidence = gate.check_code7(tmp_path)
    assert not ok
    assert "no default" in evidence


def _manifest_payload(
    roles: dict | None = None,
    *,
    profile_id: str = "fake",
    budget: dict | None = {"max_tokens_per_run": 1000},
) -> dict:
    models: dict = {"roles": roles or {"planner": {"model": "m"}}}
    if budget is not None:
        models["budget"] = budget
    return {
        "id": profile_id,
        "version": 1,
        "engine_api": 1,
        "source": {},
        "capabilities": {"a": {"impl": "fake.a"}},
        "graph": [{"step": "a"}],
        "checks": [
            {"id": "c", "kind": "constraint", "threshold": {"source": "literal"}}
        ],
        "models": models,
    }


def test_planted_open_weight_on_hosted_adapter_fails(tmp_path: Path) -> None:
    import json as _json

    _write(
        tmp_path,
        "profiles/fake/profile.json",
        _json.dumps(
            _manifest_payload(
                {
                    "local_critic": {
                        "model": "m",
                        "adapter": "meta_cli",
                        "requires": {"open_weight": True},
                    }
                }
            )
        ),
    )
    ok, evidence = gate.check_code9(tmp_path)
    assert not ok
    assert "local_critic" in evidence


def test_planted_open_weight_on_local_adapter_passes(tmp_path: Path) -> None:
    import json as _json

    _write(
        tmp_path,
        "profiles/fake/profile.json",
        _json.dumps(
            _manifest_payload(
                {
                    "local_critic": {
                        "model": "m",
                        "adapter": "glimmer_local",
                        "requires": {"open_weight": True},
                    }
                }
            )
        ),
    )
    ok, _ = gate.check_code9(tmp_path)
    assert ok


def test_planted_missing_budget_fails(tmp_path: Path) -> None:
    import json as _json

    _write(
        tmp_path,
        "profiles/hylogger/profile.json",
        _json.dumps(_manifest_payload(budget=None, profile_id="hylogger")),
    )
    ok, evidence = gate.check_code10(tmp_path)
    assert not ok
    assert "max_tokens_per_run" in evidence


def test_planted_missing_reporter_fails(tmp_path: Path) -> None:
    import json as _json

    _write(
        tmp_path,
        "profiles/hylogger/profile.json",
        _json.dumps(_manifest_payload(profile_id="hylogger")),
    )
    ok, evidence = gate.check_code10(tmp_path)
    assert not ok
    assert "reporter" in evidence


def test_planted_enabled_reporter_fails(tmp_path: Path) -> None:
    import json as _json

    payload = _manifest_payload(profile_id="hylogger")
    payload["models"]["roles"]["reporter"] = {
        "model": "muse-wonder-1.0",
        "adapter": "meta_cli",
        "enabled": True,
    }
    _write(tmp_path, "profiles/hylogger/profile.json", _json.dumps(payload))
    ok, evidence = gate.check_code10(tmp_path)
    assert not ok
    assert "reporter" in evidence


def test_planted_engine_max_attempts_default_fails(tmp_path: Path) -> None:
    import json as _json

    _write(
        tmp_path,
        "profiles/fake/profile.json",
        _json.dumps(
            {
                "id": "fake",
                "version": 1,
                "engine_api": 1,
                "source": {},
                "capabilities": {
                    "a": {"impl": "fake.a"},
                    "b": {"impl": "fake.b"},
                },
                "graph": [
                    {
                        "loop": ["a"],
                        "on_fail": "b",
                        "max_attempts": "max_attempts",
                    },
                ],
                "checks": [
                    {
                        "id": "c",
                        "kind": "constraint",
                        "threshold": {"source": "literal"},
                    }
                ],
            }
        ),
    )
    _write(
        tmp_path,
        "engine/evil.py",
        "def run(max_attempts=3):\n    return max_attempts\n",
    )
    ok, evidence = gate.check_code7(tmp_path)
    assert not ok
    assert "default" in evidence
