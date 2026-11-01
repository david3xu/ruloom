"""Generator tests: determinism, seed sensitivity, plan validation, checks."""

import hashlib
from pathlib import Path

import pytest

from profiles.hylogger.analysis.calibration import load_thresholds
from profiles.hylogger.validation.checks import run_checks
from profiles.hylogger.validation.dataset import read
from profiles.hylogger.generation.generate import generate
from profiles.hylogger.intake.reader import read_scalar_csv
from profiles.hylogger.analysis.statistics import Profile

REPO_ROOT = Path(__file__).resolve().parents[3]
PDD446 = REPO_ROOT / "data" / "sources" / "PDD446-scalars.csv"
THRESHOLDS = REPO_ROOT / "profiles" / "hylogger" / "thresholds.json"


def _pdd446_profile() -> Profile:
    samples = read_scalar_csv(PDD446)
    return Profile.from_samples(
        samples, "PDD446", hashlib.sha256(PDD446.read_bytes()).hexdigest()
    )


def test_determinism_same_seed_identical_bytes() -> None:
    profile = _pdd446_profile()
    plan = {"seed": 446, "n_rows": 2000}
    assert generate(profile, plan) == generate(profile, plan)


def test_different_seed_differs() -> None:
    profile = _pdd446_profile()
    first = generate(profile, {"seed": 446, "n_rows": 2000})
    second = generate(profile, {"seed": 447, "n_rows": 2000})
    assert first != second


def test_plan_validation() -> None:
    profile = _pdd446_profile()
    with pytest.raises(ValueError):
        generate(profile, {"seed": 1, "n_rows": 0})
    with pytest.raises(ValueError):
        generate(profile, {"seed": 1, "n_rows": -5})
    with pytest.raises(TypeError):
        generate(profile, {"seed": "446", "n_rows": 10})
    with pytest.raises(TypeError):
        generate(profile, {"seed": 446, "n_rows": 10.0})
    with pytest.raises(ValueError):
        generate(profile, {"seed": 446})


def test_generated_artifact_passes_checks() -> None:
    profile = _pdd446_profile()
    payload = generate(profile, {"seed": 446, "n_rows": 5000})
    samples = read(payload)
    assert len(samples) == 5000
    thresholds = load_thresholds(THRESHOLDS)
    source = read_scalar_csv(PDD446)
    report = run_checks(
        samples, thresholds, source_samples=source,
        artifact_name="generate-test", artifact_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert report.verdict == "pass", [r for r in report.results if r.status == "fail"]


def test_synthetic_png_is_byte_deterministic() -> None:
    """Same image statistics + same seed give identical PNG bytes."""
    from profiles.hylogger.imaging.generate import (
        make_fixture_png,
        synthesize,
    )
    from profiles.hylogger.imaging.statistics import compute_statistics

    payload = make_fixture_png(
        24, 12, [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
    )
    stats = compute_statistics(payload, "fixture.png").to_dict()
    first = synthesize(stats, 7)
    second = synthesize(stats, 7)
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert first[:8] == b"\x89PNG\r\n\x1a\n"


def test_synthetic_png_seed_sensitivity() -> None:
    """Different seeds give different PNG bytes (seed matters)."""
    from profiles.hylogger.imaging.generate import (
        make_fixture_png,
        synthesize,
    )
    from profiles.hylogger.imaging.statistics import compute_statistics

    payload = make_fixture_png(
        24, 12, [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
    )
    stats = compute_statistics(payload, "fixture.png").to_dict()
    assert synthesize(stats, 7) != synthesize(stats, 8)


def test_synthetic_png_matches_dimensions() -> None:
    """The synthetic image honours the reference dimensions."""
    from profiles.hylogger.imaging.codec import decode_png
    from profiles.hylogger.imaging.generate import (
        make_fixture_png,
        synthesize,
    )
    from profiles.hylogger.imaging.statistics import compute_statistics

    payload = make_fixture_png(
        20, 12, [(10, 10, 10), (120, 60, 200), (45, 200, 90), (250, 240, 230)]
    )
    stats = compute_statistics(payload, "fixture.png").to_dict()
    width, height, _rows = decode_png(synthesize(stats, 3))
    assert (width, height) == (20, 12)
