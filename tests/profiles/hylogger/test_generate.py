"""Generator tests: determinism, seed sensitivity, plan validation, checks."""

import hashlib
import math
from pathlib import Path

import pytest

from profiles.hylogger.analysis.calibration import load_thresholds
from profiles.hylogger.analysis.statistics import Profile
from profiles.hylogger.generation.generate import (
    _balanced_flags,
    _positive_partition,
    _supported,
    generate,
    generate_samples,
)
from profiles.hylogger.intake.reader import Sample, read_scalar_csv
from profiles.hylogger.validation.checks import run_checks
from profiles.hylogger.validation.dataset import read

REPO_ROOT = Path(__file__).resolve().parents[3]
PDD446 = REPO_ROOT / "data" / "sources" / "PDD446-scalars.csv"
THRESHOLDS = REPO_ROOT / "profiles" / "hylogger" / "thresholds.json"

requires_source_data = pytest.mark.skipif(
    not PDD446.is_file(),
    reason="requires git-ignored NVCL source data under data/ (absent in clean checkout)",
)


def _pdd446_profile() -> Profile:
    samples = read_scalar_csv(PDD446)
    return Profile.from_samples(
        samples, "PDD446", hashlib.sha256(PDD446.read_bytes()).hexdigest()
    )


@requires_source_data
def test_determinism_same_seed_identical_bytes() -> None:
    profile = _pdd446_profile()
    plan = {"seed": 446, "n_rows": 2000}
    assert generate(profile, plan) == generate(profile, plan)


@requires_source_data
def test_different_seed_differs() -> None:
    profile = _pdd446_profile()
    first = generate(profile, {"seed": 446, "n_rows": 2000})
    second = generate(profile, {"seed": 447, "n_rows": 2000})
    assert first != second


def _trap_profile() -> Profile:
    """Profile with a GARNET-shaped singleton self-loop trap.

    TIR label ``Z`` closes the fixture with a single ``Z -> Z`` pair
    (one outgoing pair, back to itself), mirroring PDD446's singleton
    GARNET row. Trusting that row as a rule would absorb the chain:
    entering ``Z`` once would answer ``Z`` forever.
    """
    samples = []
    for i in range(300):
        if i in (298, 299):
            tir: str | None = "Z"
        else:
            tir = ("A", "B")[i % 2]
        samples.append(
            Sample(
                depth_m=i * 0.005,
                valid=True,
                swir_group="CHLORITE",
                tir_group=tir,
            )
        )
    profile = Profile.from_samples(samples, "trap", "0" * 64)
    assert profile.stats["tir"]["transitions"].get("Z") == {"Z": 1}
    return profile


def test_singleton_transition_row_is_not_trusted() -> None:
    """A one-pair transition row must not become an absorbing state."""
    profile = _trap_profile()
    assert _supported({"Z": {"Z": 1}}) == {}
    for seed in (7, 446, 12345):
        synth = generate_samples(profile, seed, 2000)
        tir = [s.tir_group for s in synth if s.valid]
        assert tir, "expected answered TIR labels"
        assert sum(1 for label in tir if label == "Z") / len(tir) < 0.2
        first = generate(profile, {"seed": seed, "n_rows": 2000})
        second = generate(profile, {"seed": seed, "n_rows": 2000})
        assert first == second


@requires_source_data
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


@requires_source_data
def test_generated_artifact_passes_checks() -> None:
    profile = _pdd446_profile()
    payload = generate(profile, {"seed": 446, "n_rows": 5000})
    samples = read(payload)
    assert len(samples) == 5000
    thresholds = load_thresholds(THRESHOLDS)
    source = read_scalar_csv(PDD446)
    report = run_checks(
        samples,
        thresholds,
        source_samples=source,
        artifact_name="generate-test",
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
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


def _synthetic_hole_profile(
    n_samples: int = 100,
    valid_fraction: float = 0.7,
    depth_start: float = 100.0,
    depth_step: float = 0.01,
) -> Profile:
    """Offline synthetic profile with known forward spacing and validity."""
    samples = []
    cutoff = int(round(10 * valid_fraction))
    for i in range(n_samples):
        valid = (i % 10) < cutoff
        swir = "CHLORITE" if valid and (i % 2 == 0) else None
        tir = "QUARTZ" if valid and (i % 3 == 0) else None
        samples.append(
            Sample(
                depth_m=round(depth_start + i * depth_step, 4),
                valid=valid,
                swir_group=swir,
                tir_group=tir,
            )
        )
    return Profile.from_samples(samples, "synth-fixture", "0" * 64)


def test_legacy_exact_bytes_pinned() -> None:
    """Legacy fitted_markov_joint output bytes remain pinned and byte-identical."""
    profile = _trap_profile()
    plan_with_strategy = {"seed": 7, "n_rows": 200, "strategy": "fitted_markov_joint"}
    plan_without_strategy = {"seed": 7, "n_rows": 200}
    payload1 = generate(profile, plan_with_strategy)
    payload2 = generate(profile, plan_without_strategy)
    assert payload1 == payload2
    assert (
        hashlib.sha256(payload1).hexdigest()
        == "0e0464d6f67c09036d34518593be4364378703818fe82365b56c199b653a58b9"
    )


def test_strategy_dispatch() -> None:
    """Both strategies dispatch cleanly; balanced hits exact integer valid count."""
    profile = _synthetic_hole_profile(n_samples=100, valid_fraction=0.7)
    vf = profile.stats["validity"]["valid_fraction"]
    expected_valid = int(round(100 * vf))

    samples_joint = generate_samples(
        profile, seed=42, n_rows=100, strategy="fitted_markov_joint"
    )
    samples_balanced = generate_samples(
        profile, seed=42, n_rows=100, strategy="fitted_markov_balanced"
    )

    assert len(samples_joint) == 100
    assert len(samples_balanced) == 100
    assert sum(s.valid for s in samples_balanced) == expected_valid
    # The two algorithms use different PRNG draw sequences and flag structures
    assert [s.valid for s in samples_joint] != [s.valid for s in samples_balanced]


def test_deterministic_balanced_output() -> None:
    """Same (profile, seed, n_rows, balanced) produces identical CSV bytes."""
    profile = _synthetic_hole_profile(n_samples=80, valid_fraction=0.6)
    plan1 = {"seed": 10, "n_rows": 80, "strategy": "fitted_markov_balanced"}
    plan2 = {"seed": 11, "n_rows": 80, "strategy": "fitted_markov_balanced"}

    out1a = generate(profile, plan1)
    out1b = generate(profile, plan1)
    out2 = generate(profile, plan2)

    assert out1a == out1b
    assert out1a != out2


def test_exact_valid_target_across_seeds_and_counts() -> None:
    """Balanced strategy preserves exact rounded target across seeds and row counts."""
    profile = _synthetic_hole_profile(n_samples=120, valid_fraction=0.7)
    vf = float(profile.stats["validity"]["valid_fraction"])

    for seed in (1, 42, 123, 9999):
        for n_rows in (17, 33, 100, 250):
            expected = int(round(n_rows * vf))
            samples = generate_samples(
                profile, seed=seed, n_rows=n_rows, strategy="fitted_markov_balanced"
            )
            assert len(samples) == n_rows
            assert sum(s.valid for s in samples) == expected


def test_balanced_all_valid_and_all_invalid() -> None:
    """Extreme validity targets (all valid and all invalid) work without error."""
    # All valid:
    trap = _trap_profile()  # valid_fraction == 1.0
    valid_samples = generate_samples(
        trap, seed=7, n_rows=50, strategy="fitted_markov_balanced"
    )
    assert len(valid_samples) == 50
    assert all(s.valid for s in valid_samples)

    # All invalid:
    inv_samples = [
        Sample(
            depth_m=round(10.0 + i * 0.01, 3),
            valid=False,
            swir_group=None,
            tir_group=None,
        )
        for i in range(40)
    ]
    inv_profile = Profile.from_samples(inv_samples, "inv-prof", "0" * 64)
    out_samples = generate_samples(
        inv_profile, seed=7, n_rows=40, strategy="fitted_markov_balanced"
    )
    assert len(out_samples) == 40
    assert not any(s.valid for s in out_samples)
    assert all(s.swir_group is None and s.tir_group is None for s in out_samples)


def test_mixed_mean_and_run_validation() -> None:
    """_positive_partition and _balanced_flags validate bounds and allocations."""
    import random

    rng = random.Random(42)

    # Partition validation
    with pytest.raises(ValueError, match="cannot partition"):
        _positive_partition(rng, total=5, count=0)
    with pytest.raises(ValueError, match="cannot partition"):
        _positive_partition(rng, total=4, count=5)
    with pytest.raises(ValueError, match="cannot partition"):
        _positive_partition(rng, total=0, count=1)

    parts = _positive_partition(rng, total=25, count=6)
    assert len(parts) == 6
    assert sum(parts) == 25
    assert all(p >= 1 for p in parts)

    # Flag validation
    with pytest.raises(ValueError, match="valid_fraction must be a finite float"):
        _balanced_flags(rng, 10, -0.1, 5.0, 5.0)
    with pytest.raises(ValueError, match="valid_fraction must be a finite float"):
        _balanced_flags(rng, 10, 1.5, 5.0, 5.0)
    with pytest.raises(ValueError, match="valid_fraction must be a finite float"):
        _balanced_flags(rng, 10, float("nan"), 5.0, 5.0)

    # Fallback to k=1 when means are None or invalid
    flags_none = _balanced_flags(rng, 10, 0.6, None, None)
    assert len(flags_none) == 10
    assert sum(flags_none) == 6

    # Single available mean
    flags_single = _balanced_flags(rng, 20, 0.7, 5.0, None)
    assert len(flags_single) == 20
    assert sum(flags_single) == 14


def test_unknown_strategy_refusal() -> None:
    """Unknown or invalid strategy names are refused with ValueError."""
    profile = _synthetic_hole_profile(n_samples=50, valid_fraction=0.8)

    with pytest.raises(ValueError, match="unknown generation strategy"):
        generate_samples(profile, seed=1, n_rows=10, strategy="unsupported_strategy")

    with pytest.raises(ValueError, match="unknown generation strategy"):
        generate(profile, {"seed": 1, "n_rows": 10, "strategy": "bogus"})

    with pytest.raises(ValueError, match="must be a non-empty string"):
        generate(profile, {"seed": 1, "n_rows": 10, "strategy": ""})

    with pytest.raises(ValueError, match="must be a non-empty string"):
        generate(profile, {"seed": 1, "n_rows": 10, "strategy": 42})


def test_ordered_depths_and_masked_repeats_balanced() -> None:
    """Balanced generator maintains non-decreasing finite depths with masked repeats."""
    profile = _synthetic_hole_profile(n_samples=80, valid_fraction=0.5)
    samples = generate_samples(
        profile, seed=99, n_rows=80, strategy="fitted_markov_balanced"
    )

    depths = [s.depth_m for s in samples]
    assert all(math.isfinite(d) for d in depths)
    assert all(b >= a for a, b in zip(depths[:-1], depths[1:]))

    for i in range(1, len(samples)):
        if depths[i] == depths[i - 1]:
            # Step repeats only when both previous and current rows are invalid (masked)
            assert not samples[i - 1].valid
            assert not samples[i].valid


@pytest.mark.parametrize(
    "n_rows,vf,expected", [(1, 0.5, 0), (1, 0.6, 1), (5, 0.5, 2), (7, 0.5, 4)]
)
def test_balanced_small_targets_and_ties(n_rows, vf, expected) -> None:
    """Small targets retain Python's documented half-to-even rounding."""
    import random

    flags = _balanced_flags(random.Random(42), n_rows, vf, 2.0, 2.0)
    assert len(flags) == n_rows
    assert sum(flags) == expected


@pytest.mark.parametrize("vf", [float("inf"), float("-inf"), True, "0.7"])
def test_balanced_rejects_invalid_fraction_types(vf) -> None:
    import random

    with pytest.raises(ValueError, match="valid_fraction"):
        _balanced_flags(random.Random(42), 10, vf, 2.0, 2.0)


def test_public_path_valid_fraction_bool_and_non_numeric_refused() -> None:
    """Public generate_samples refuses bool/non-numeric without float conversion."""
    profile = _synthetic_hole_profile(n_samples=50, valid_fraction=0.8)

    profile.stats["validity"]["valid_fraction"] = True
    with pytest.raises(ValueError, match="valid_fraction must be a finite float"):
        generate_samples(profile, seed=1, n_rows=20, strategy="fitted_markov_balanced")

    profile.stats["validity"]["valid_fraction"] = "0.75"
    with pytest.raises(ValueError, match="valid_fraction must be a finite float"):
        generate_samples(profile, seed=1, n_rows=20, strategy="fitted_markov_balanced")


def test_tiny_run_mean_bounded_safely() -> None:
    """Tiny positive run-means (< 1.0) bound safely without division overflow."""
    profile = _synthetic_hole_profile(n_samples=50, valid_fraction=0.7)
    profile.stats["validity"]["valid_runs"]["mean"] = 1e-300
    profile.stats["validity"]["invalid_runs"]["mean"] = 1e-300
    samples = generate_samples(
        profile, seed=42, n_rows=20, strategy="fitted_markov_balanced"
    )
    assert len(samples) == 20
    assert sum(s.valid for s in samples) == 14
