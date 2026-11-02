"""Installed-wheel CLI test (GP-03 G7 hygiene).

Proves the ``ruloom`` CLI works from an installed layout outside the
repo: the test builds a real wheel from the working tree with the
stdlib only (no network, no build backend), extracts it into an
isolated directory (what ``pip install --target`` lays down), and runs
both ``ruloom --help`` through a generated console-script shim and one
minimal fake-model ``run`` through the installed ``engine.cli`` -- each
with the working directory outside the repo and the repo root nowhere
on ``sys.path``. The driver asserts the imported ``engine`` package
lives under the install dir, so a stale editable install or repo-root
leak fails the test instead of silently passing.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_S = 300

COVERAGE: list[tuple[str, str, str]] = [
    ("1", "A", "X"),
    ("1", "B", ""),
    ("1", "", "Y"),
    ("1", "A", "Y"),
    ("0", "", ""),
    ("1", "B", "X"),
    ("0", "", ""),
    ("1", "A", "X"),
]


def _project_meta() -> tuple[str, str, list[str]]:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    project = data["project"]
    packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    return str(project["name"]), str(project["version"]), list(packages)


def _build_wheel(wheel_path: Path) -> None:
    """Write a valid pure-Python wheel from the working tree (stdlib only).

    Layout, ``dist-info`` metadata, entry points, and ``RECORD`` hashes
    follow the wheel format so any installer accepts the artifact; only
    the declared wheel packages ship (never ``data/`` or ``runs/``).
    """
    name, version, packages = _project_meta()
    dist_info = f"{name}-{version}.dist-info"
    records: list[tuple[str, str, int]] = []

    def add(archive: zipfile.ZipFile, arc: str, payload: bytes) -> None:
        archive.writestr(arc, payload)
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
        records.append((arc, digest.rstrip(b"=").decode("ascii"), len(payload)))

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for package in packages:
            for path in sorted((ROOT / package).rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                arc = path.relative_to(ROOT).as_posix()
                add(archive, arc, path.read_bytes())
        add(
            archive,
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        )
        add(
            archive,
            f"{dist_info}/WHEEL",
            b"Wheel-Version: 1.0\nGenerator: ruloom-wheel-test\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        add(
            archive,
            f"{dist_info}/entry_points.txt",
            b"[console_scripts]\nruloom = engine.cli:main\n",
        )
        record_lines = "".join(
            f"{arc},{digest},{size}\n" for arc, digest, size in records
        )
        record_lines += f"{dist_info}/RECORD,,\n"
        archive.writestr(f"{dist_info}/RECORD", record_lines)


def _write_shim(bin_dir: Path) -> Path:
    """Generate the installed ``ruloom`` console script (as pip would)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "ruloom"
    shim.write_text(
        f"#!{sys.executable}\nfrom engine.cli import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build the wheel once and extract it into an isolated target dir."""
    work = tmp_path_factory.mktemp("wheel")
    wheel_path = work / "ruloom-test-py3-none-any.whl"
    _build_wheel(wheel_path)
    target = work / "install"
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()
        assert any(n.endswith(".dist-info/RECORD") for n in names)
        archive.extractall(target)
    assert (target / "engine" / "cli" / "__init__.py").is_file()
    assert (target / "models" / "base.py").is_file()
    assert (target / "profiles").is_dir()
    shim = _write_shim(target / "bin")
    return {"root": target, "shim": shim}


def _outside_env(target: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(target)
    env.pop("PYTHONSAFEPATH", None)
    return env


def test_installed_wheel_help(installed_wheel: dict[str, Path], tmp_path: Path) -> None:
    """The installed console script answers --help outside the repo."""
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        [str(installed_wheel["shim"]), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(work),
        env=_outside_env(installed_wheel["root"]),
        timeout=TIMEOUT_S,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "run" in proc.stdout


def test_installed_wheel_fake_model_run(
    installed_wheel: dict[str, Path], tmp_path: Path
) -> None:
    """One minimal fake-model run through the installed layout, no network.

    The driver replays a single queued plan through the installed
    ``engine.cli.main`` with piped ``accept`` input, mirroring the
    in-tree CLI accept path but importing ``engine`` only from the
    install dir with the working directory outside the repo.
    """
    target = installed_wheel["root"]
    work = tmp_path / "work"
    work.mkdir()
    driver = work / "driver.py"
    driver.write_text(
        "import io\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import engine\n"
        "import models\n"
        "import profiles\n"
        "from engine.cli import main\n"
        "from models.base import Completion, Usage\n"
        "from profiles.hylogger.analysis.calibration import Band, Thresholds\n"
        "print('ENGINE_FILE=' + str(Path(engine.__file__).resolve()))\n"
        "print('MODELS_FILE=' + str(Path(models.__file__).resolve()))\n"
        "print('PROFILES_FILE=' + str(Path(profiles.__file__).resolve()))\n"
        "work = Path.cwd()\n"
        "lines = ['StartDepth,Final_Mask,Grp1_sTSAS,Grp1_sjCLST']\n"
        + "\n".join(
            f"lines.append('{123.456789 + i * 0.0078},{m},{s},{t}')"
            for i, (m, s, t) in enumerate(COVERAGE)
        )
        + "\n"
        "(work / 'TOY-hole-scalars.csv').write_text('\\n'.join(lines) + '\\n')\n"
        "(work / 'TOY-hole-source.json').write_text(json.dumps({\n"
        "    'dataset_name': 'TOY-hole', 'licence': 'CC-BY-4.0',\n"
        "    'attribution': 'Toy attribution for tests.'}))\n"
        "thresholds = Thresholds(\n"
        "    bands={\n"
        "        'spacing_median_m': Band('spacing_median_m', 0.005, 0.01),\n"
        "        'repeated_depth_fraction': Band(\n"
        "            'repeated_depth_fraction', 0.0, 0.2),\n"
        "        'valid_fraction': Band('valid_fraction', 0.5, 1.0),\n"
        "        'swir_answer_rate': Band(\n"
        "            'swir_answer_rate', 0.0, 1.0, vacuous=True),\n"
        "        'tir_answer_rate': Band('tir_answer_rate', 0.9, 1.0),\n"
        "        'repeated_valid_fraction': Band(\n"
        "            'repeated_valid_fraction', 0.0, 0.05),\n"
        "        'backward_steps': Band('backward_steps', 0, 0)},\n"
        "    swir_vocabulary=frozenset({'A', 'B'}),\n"
        "    tir_vocabulary=frozenset({'X', 'Y'}))\n"
        "(work / 'thresholds.json').write_text(\n"
        "    json.dumps(thresholds.to_dict()) + '\\n')\n"
        "class FakeModel:\n"
        "    def complete(self, prompt, schema):\n"
        "        return Completion(data={'seed': 8, 'n_rows': 8},\n"
        "                        usage=Usage(model='fake', input_tokens=10,\n"
        "                                        output_tokens=5, calls=1),\n"
        "                        meta={'model': 'fake'})\n"
        "sys.stdin = io.StringIO('accept\\nWheel install check.\\n')\n"
        "code = main(['run', str(work / 'TOY-hole-scalars.csv'),\n"
        "             '--max-attempts', '1', '--out', str(work / 'out')],\n"
        "            model=FakeModel())\n"
        "print('RC=' + str(code))\n",
        encoding="utf-8",
    )
    env = _outside_env(target)
    env["RULOOM_THRESHOLDS"] = str(work / "thresholds.json")
    proc = subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(work),
        env=env,
        timeout=TIMEOUT_S,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    markers = {}
    for line in proc.stdout.splitlines():
        for key in ("ENGINE_FILE", "MODELS_FILE", "PROFILES_FILE"):
            if line.startswith(key + "="):
                markers[key] = line.split("=", 1)[1]
    assert set(markers) == {"ENGINE_FILE", "MODELS_FILE", "PROFILES_FILE"}, (
        f"driver did not report package locations:\n{proc.stdout}\n{proc.stderr}"
    )
    for key, location in markers.items():
        assert Path(location).is_relative_to(target), (
            f"installed layout leaked: {key} imported from {location}"
        )
    assert "\nRC=0" in proc.stdout or proc.stdout.strip().endswith("RC=0"), (
        f"fake-model run failed:\n{proc.stdout}\n{proc.stderr}"
    )
    runs = work / "out" / "runs"
    zips = list(runs.rglob("release.zip"))
    assert len(zips) == 1, f"expected one release.zip, got {zips}"
