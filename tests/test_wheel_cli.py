"""Installed-wheel CLI test (HP-04 wheel backend build).

Proves the ``ruloom`` CLI works from an installed layout outside the
repo: the test builds a real wheel from the working tree with the
project's own PEP 517 backend (``hatchling.build:build_wheel`` -- the
same hook ``pip wheel --no-build-isolation`` calls, so the artifact,
``dist-info`` metadata, entry points, and ``RECORD`` are backend-made,
never hand-rolled), installs it by extracting the wheel into an
isolated directory (the same bytes ``pip install --target`` lays
down), generates the ``ruloom`` console script from the wheel's own
``dist-info/entry_points.txt`` (so the exercised entry point is the
declared ``[project.scripts]`` wiring, not a hardcoded string), and
runs both ``ruloom --help`` through that script and one minimal
fake-model ``run`` through the installed ``engine.cli`` -- each with
the working directory outside the repo and the repo root nowhere on
``sys.path``. The driver asserts every imported package lives under
the install dir, so a stale editable install or repo-root leak fails
the test instead of silently passing.

Offline-safe: the build runs with no build isolation and no index
access, and the install/run touch no network. The one requirement is
the ``hatchling`` backend importable in the test environment (CI
installs it beside pytest; the ``dev`` dependency group declares it).
"""

from __future__ import annotations

import configparser
import os
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

try:
    from hatchling.build import build_wheel
except ImportError as exc:  # fail closed with the fix, not a bare ModuleNotFound
    raise ImportError(
        "tests/test_wheel_cli.py needs the hatchling build backend "
        "in the test environment: pip install hatchling "
        "(CI installs it beside pytest; the dev group declares it)"
    ) from exc

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


def _project_meta() -> tuple[str, str]:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    project = data["project"]
    return str(project["name"]), str(project["version"])


def _build_wheel(dist_dir: Path) -> Path:
    """Build a real wheel with the hatchling backend; return its path.

    Runs in-process with no isolation and no index access, from the repo
    root (the backend reads ``pyproject.toml`` from the cwd). The
    returned artifact is asserted to be the single backend-named wheel
    for this project/version, with a ``RECORD``, no stray top-level
    trees (never ``data/``, ``runs/``, or ``tests/``), and the declared
    ``ruloom`` console script in its entry points.
    """
    previous_cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        wheel_name = build_wheel(str(dist_dir))
    finally:
        os.chdir(previous_cwd)
    wheels = sorted(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    wheel_path = wheels[0]
    assert wheel_path.name == wheel_name, f"{wheel_path.name} != {wheel_name}"
    name, version = _project_meta()
    assert wheel_name.startswith(f"{name}-{version}-"), wheel_name
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()
        assert any(n.endswith(".dist-info/RECORD") for n in names), names
        stray = [
            n
            for n in names
            if n.startswith("data/")
            or n.startswith("runs/")
            or n.startswith("tests/")
            or "/runs/" in n
        ]
        assert not stray, f"wheel ships out-of-package trees: {stray}"
    return wheel_path


def _console_target(wheel_path: Path) -> tuple[str, str]:
    """Resolve the ``ruloom`` console script to (module, attr).

    Read from the backend-built ``dist-info/entry_points.txt`` so the
    smoke test exercises the declared ``[project.scripts]`` wiring.
    """
    with zipfile.ZipFile(wheel_path) as archive:
        entry_points = next(
            n for n in archive.namelist() if n.endswith("entry_points.txt")
        )
        text = archive.read(entry_points).decode("utf-8")
    parser = configparser.ConfigParser()
    parser.read_string(text)
    target = parser.get("console_scripts", "ruloom")
    module, _, attr = target.replace(":", ".").rpartition(".")
    assert module and attr, f"unparseable entry point: {target!r}"
    assert (module, attr) == ("engine.cli", "main"), target
    return module, attr


def _write_shim(bin_dir: Path, module: str, attr: str) -> Path:
    """Generate the installed ``ruloom`` console script (as pip would)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "ruloom"
    shim.write_text(
        f"#!{sys.executable}\nfrom {module} import {attr}\n"
        f"raise SystemExit({attr}())\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build the wheel once and extract it into an isolated target dir."""
    work = tmp_path_factory.mktemp("wheel")
    wheel_path = _build_wheel(work / "dist")
    target = work / "install"
    with zipfile.ZipFile(wheel_path) as archive:
        archive.extractall(target)
    assert (target / "engine" / "cli" / "__init__.py").is_file()
    assert (target / "models" / "base.py").is_file()
    assert (target / "profiles").is_dir()
    module, attr = _console_target(wheel_path)
    shim = _write_shim(target / "bin", module, attr)
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


def test_installed_wheel_request_context(installed_wheel, tmp_path):
    """The new request module and profile briefs ship in the real wheel."""
    work = tmp_path / "request-work"
    work.mkdir()
    driver = work / "request.py"
    driver.write_text(
        "import json\nfrom pathlib import Path\n"
        "import engine.cli.request_context as context\n"
        "from engine.cli import main\n"
        "from models.base import Completion, Usage\n"
        "class Fixture:\n"
        " def complete(self, prompt, schema):\n"
        "  assert 'private-wheel-canary' not in prompt\n"
        "  assert 'release_members' in prompt\n"
        "  return Completion({'summary':'Wheel fixture only.', 'unsupported':[],"
        "'n_rows':20,'seed':8,'outputs':['scalar'],"
        "'strategy':'fitted_gaussian_disclosed'},Usage(model='fixture-only'))\n"
        "Path('source.csv').write_text('id,value\\nprivate-wheel-canary,1\\n')\n"
        "assert main(['prepare','source.csv','--profile','tabular','--request',"
        "'Create 20 rows.','--max-attempts','1','--out','draft',"
        "'--role-adapter','planner=glimmer_local'],model=Fixture()) == 0\n"
        "r=json.loads(Path('draft/request.json').read_text())\n"
        "assert r['capability_context']['profile_brief']\n"
        "assert r['capability_context_sha256']\n"
        "print('CONTEXT_FILE='+str(Path(context.__file__).resolve()))\n"
    )
    proc = subprocess.run(
        [sys.executable, str(driver)],
        cwd=work,
        env=_outside_env(installed_wheel["root"]),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    location = next(
        line.split("=", 1)[1]
        for line in proc.stdout.splitlines()
        if line.startswith("CONTEXT_FILE=")
    )
    assert Path(location).is_relative_to(installed_wheel["root"])


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
