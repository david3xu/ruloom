"""Statistics tests: summary correctness and the source-data boundary.

The boundary test proves ``Profile.to_json()`` for PDD446 carries no
source depth value: no float-typed number in the JSON equals any source
depth, and no raw source depth cell string appears in the JSON text.
"""

import hashlib
import json
from pathlib import Path

import pytest

from profiles.hylogger.validation.dataset import read_csv_bytes, to_csv_bytes
from profiles.hylogger.intake.reader import Sample, read_scalar_csv
from profiles.hylogger.analysis.statistics import Profile, build_stats

REPO_ROOT = Path(__file__).resolve().parents[3]
PDD446 = REPO_ROOT / "data" / "sources" / "PDD446-scalars.csv"
PDD446_MANIFEST = REPO_ROOT / "data" / "sources" / "PDD446-source.json"

requires_source_data = pytest.mark.skipif(
    not PDD446.is_file(),
    reason="requires git-ignored NVCL source data under data/ (absent in clean checkout)",
)


def _toy_samples() -> list[Sample]:
    return [
        Sample(depth_m=10.0, valid=True, swir_group="A", tir_group="X"),
        Sample(depth_m=10.5, valid=True, swir_group="A", tir_group="Y"),
        Sample(depth_m=11.0, valid=False, swir_group="B", tir_group="X"),
        Sample(depth_m=11.0, valid=True, swir_group=None, tir_group="X"),
        Sample(depth_m=12.0, valid=True, swir_group="B", tir_group=None),
        Sample(depth_m=11.5, valid=True, swir_group="B", tir_group="X"),
    ]


def test_geometry() -> None:
    stats = build_stats(_toy_samples())
    assert stats["n_samples"] == 6
    geo = stats["geometry"]
    assert geo["depth_start_m"] == 10.0
    assert geo["depth_end_m"] == 11.5
    assert geo["depth_span_m"] == 1.5
    assert geo["n_repeated_steps"] == 1
    assert geo["n_backward_steps"] == 1
    spacing = geo["spacing_m"]
    assert spacing["count_forward"] == 3
    assert spacing["p50"] == 0.5
    assert spacing["p75"] == 0.75
    assert spacing["p90"] == 0.9


def test_validity() -> None:
    validity = build_stats(_toy_samples())["validity"]
    assert (validity["n_valid"], validity["n_invalid"]) == (5, 1)
    assert validity["valid_fraction"] == round(5 / 6, 6)
    assert validity["valid_runs"] == {"count": 2, "max": 3, "mean": 2.5}
    assert validity["invalid_runs"] == {"count": 1, "max": 1, "mean": 1.0}


def test_channel_marginals_and_transitions() -> None:
    stats = build_stats(_toy_samples())
    swir = stats["swir"]
    assert swir["marginals"] == {"A": 2, "B": 2}
    assert swir["marginal_fractions"] == {"A": 0.5, "B": 0.5}
    assert swir["answer_rate"] == 0.8
    assert swir["persistence"] == 1.0
    assert swir["transitions"] == {"A": {"A": 1}, "B": {"B": 1}}

    tir = stats["tir"]
    assert tir["marginals"] == {"X": 3, "Y": 1}
    assert tir["answer_rate"] == 0.8
    assert tir["persistence"] == 0.0
    assert tir["transitions"] == {"X": {"Y": 1}, "Y": {"X": 1}}


def test_joint_and_agreement() -> None:
    joint = build_stats(_toy_samples())["joint"]
    assert joint["n_both_answered"] == 3
    assert joint["joint_counts"] == {"A": {"X": 1, "Y": 1}, "B": {"X": 1}}
    assert joint["agreement"] == 0.0
    assert joint["coverage_valid"] == {
        "both_answered": 3,
        "swir_only": 1,
        "tir_only": 1,
        "neither_answered": 0,
    }


def test_empty_profile_serializes() -> None:
    profile = Profile.from_samples([], source_name="EMPTY", source_sha256="0" * 64)
    parsed = json.loads(profile.to_json())
    assert parsed["stats"]["n_samples"] == 0
    assert parsed["stats"]["validity"]["valid_fraction"] is None


def test_to_json_carries_source_identity() -> None:
    profile = Profile.from_samples(
        _toy_samples(), source_name="HOLE", source_sha256="abc123"
    )
    parsed = json.loads(profile.to_json())
    assert parsed["source_name"] == "HOLE"
    assert parsed["source_sha256"] == "abc123"


def _json_floats(node: object) -> list[float]:
    found: list[float] = []
    if isinstance(node, float):
        found.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_json_floats(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_json_floats(value))
    return found


@requires_source_data
def test_pdd446_profile_has_no_source_depth_value() -> None:
    samples = read_scalar_csv(PDD446)
    source_depths = {s.depth_m for s in samples}
    assert len(source_depths) > 1000  # sanity: real depth series present

    sha256 = hashlib.sha256(PDD446.read_bytes()).hexdigest()
    manifest = json.loads(PDD446_MANIFEST.read_text())
    assert sha256 == manifest["sha256"]

    profile = Profile.from_samples(samples, source_name="PDD446", source_sha256=sha256)
    text = profile.to_json()
    assert f'"source_sha256": "{sha256}"' in text

    parsed = json.loads(text)
    # The spec sanctions three rounded aggregates (start/end/span). Drop
    # those keys by location -- not by value -- so every other JSON float
    # must avoid the source depth series. Rounding still hides the raw
    # cell text (299.903 vs "299.90298").
    geometry = parsed["stats"]["geometry"]
    for key in ("depth_start_m", "depth_end_m", "depth_span_m"):
        geometry.pop(key, None)
    for value in _json_floats(parsed):
        assert value not in source_depths, f"depth leak: {value!r}"

    # No per-sample depth list may be embedded in the profile.
    def _lists(node: object) -> list[list]:
        found = [node] if isinstance(node, list) else []
        children: list[object] = []
        if isinstance(node, dict):
            children = list(node.values())
        elif isinstance(node, list):
            children = list(node)
        for child in children:
            found.extend(_lists(child))
        return found

    for lst in _lists(parsed):
        assert len(lst) != len(samples), "embedded per-sample series"

    raw_cells = {line.split(",")[0] for line in PDD446.read_text().splitlines()[1:]}
    distinctive = [cell for i, cell in enumerate(sorted(raw_cells)) if i % 37 == 0][
        :500
    ]
    assert len(distinctive) > 100
    for cell in distinctive:
        assert cell not in text, f"raw depth cell leaks: {cell!r}"


@requires_source_data
def test_pdd446_summary() -> None:
    samples = read_scalar_csv(PDD446)
    profile = Profile.from_samples(
        samples,
        source_name="PDD446",
        source_sha256=hashlib.sha256(PDD446.read_bytes()).hexdigest(),
    )
    stats = profile.stats
    print(
        "\nPDD446:",
        "samples =", stats["n_samples"],
        "valid_fraction =", stats["validity"]["valid_fraction"],
        "spacing_median =", stats["geometry"]["spacing_m"]["p50"],
    )
    assert stats["n_samples"] == 39300
    assert 0.0 < stats["validity"]["valid_fraction"] < 1.0
    assert stats["geometry"]["spacing_m"]["p50"] is not None


def test_profile_survives_synthetic_csv_round_trip() -> None:
    samples = _toy_samples()
    replayed = read_csv_bytes(to_csv_bytes(samples))
    assert build_stats(replayed) == build_stats(samples)


def test_image_statistics_from_fixture_png() -> None:
    """Image statistics are code-only: histograms, means, texture, strips."""
    from profiles.hylogger.imaging.generate import make_fixture_png
    from profiles.hylogger.imaging.statistics import (
        N_BINS,
        N_STRIPS,
        compute_statistics,
    )

    payload = make_fixture_png(
        16, 8, [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
    )
    stats = compute_statistics(payload, "fixture.png")
    assert (stats.width, stats.height) == (16, 8)
    for channel in ("r", "g", "b"):
        assert len(stats.histograms[channel]) == N_BINS
        assert sum(stats.histograms[channel]) == 16 * 8
    assert len(stats.strips) == N_STRIPS
    assert stats.strips[0]["mean_r"] == 200.0
    assert stats.strips[0]["mean_g"] == 30.0
    # Solid-colour strips carry no within-strip texture.
    assert stats.texture_h == {"r": 0.0, "g": 0.0, "b": 0.0}
    assert stats.texture_v == {"r": 0.0, "g": 0.0, "b": 0.0}
    assert stats.sha256 == hashlib.sha256(payload).hexdigest()


def test_image_statistics_reject_jpeg_without_decoder() -> None:
    """JPEG has no stdlib decoder: statistics raise instead of guessing."""
    from profiles.hylogger.imaging.statistics import compute_statistics

    with pytest.raises(ValueError):
        compute_statistics(b"\xff\xd8\xff\xe0fake", "photo.jpg", "image/jpeg")


def test_image_statistics_survive_dict_round_trip() -> None:
    from profiles.hylogger.imaging.generate import make_fixture_png
    from profiles.hylogger.imaging.statistics import (
        ImageStats,
        compute_statistics,
        summarize_for_prompt,
    )

    payload = make_fixture_png(
        8, 8, [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]
    )
    stats = compute_statistics(payload, "fixture.png")
    replayed = ImageStats.from_dict(stats.to_dict())
    assert replayed.to_dict() == stats.to_dict()
    summary = summarize_for_prompt(stats)
    assert summary["width"] == 8
    assert len(summary["strips"]) == 4
