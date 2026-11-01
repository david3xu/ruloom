"""Unit tests for the schematic shared scene record (RP-06, R03).

The scene is schematic-only: it maps sample ids to the deterministic
strip bands our own encoder writes, with schematic depth intervals.
Real NVCL core-photo registration is unclaimed.
"""

from __future__ import annotations

import pytest

from profiles.hylogger.imaging.generate import make_fixture_png
from profiles.hylogger.imaging.scene import (
    ORIENTATION_TOP_DOWN,
    Scene,
    build_schematic_scene,
    image_view,
    table_view,
    validate_scene,
    views_agree,
)
from profiles.hylogger.imaging.statistics import compute_statistics

PALETTE = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
WIDTH, HEIGHT = 24, 12


def _stats() -> dict:
    return compute_statistics(
        make_fixture_png(WIDTH, HEIGHT, PALETTE), "reference.png"
    ).to_dict()


def test_build_covers_full_height_with_all_samples() -> None:
    scene = build_schematic_scene(_stats(), "reference.png")
    assert scene.width == WIDTH
    assert scene.height == HEIGHT
    assert len(scene.entries) == 4
    assert validate_scene(scene) == []
    assert views_agree(scene)


def test_round_trip_dict() -> None:
    scene = build_schematic_scene(_stats(), "reference.png")
    assert Scene.from_dict(scene.to_dict()) == scene


def test_custom_sample_ids_and_depths() -> None:
    scene = build_schematic_scene(
        _stats(),
        "core-a.png",
        depth_start=10.0,
        depth_step=0.5,
        sample_ids=["a", "b", "c", "d"],
    )
    assert [e.sample_id for e in scene.entries] == ["a", "b", "c", "d"]
    assert scene.entries[0].depth_top == pytest.approx(10.0)
    assert scene.entries[1].depth_top == pytest.approx(10.5)
    assert validate_scene(scene) == []


def test_sample_count_mismatch_refused() -> None:
    with pytest.raises(ValueError):
        build_schematic_scene(_stats(), "reference.png", sample_ids=["only"])


def test_empty_strips_refused() -> None:
    stats = dict(_stats())
    stats["strips"] = []
    with pytest.raises(ValueError):
        build_schematic_scene(stats, "reference.png")


def test_table_and_image_views_agree() -> None:
    scene = build_schematic_scene(_stats(), "reference.png")
    table = table_view(scene)
    image = image_view(scene)
    assert [r["sample_id"] for r in table] == [r["sample_id"] for r in image]
    assert all(r["orientation"] == ORIENTATION_TOP_DOWN for r in image)


def _mutated(**overrides) -> dict:
    scene = build_schematic_scene(_stats(), "reference.png").to_dict()
    for key, value in overrides.items():
        scene[key] = value
    return scene


def test_missing_region_rejected() -> None:
    scene = _mutated()
    scene["entries"][1]["row_hi"] = scene["entries"][1]["row_lo"]
    assert any("missing region" in e for e in validate_scene(scene))


def test_out_of_bounds_rejected() -> None:
    scene = _mutated()
    scene["entries"][-1]["row_hi"] = HEIGHT + 1
    assert any("out-of-bounds" in e for e in validate_scene(scene))


def test_duplicate_sample_rejected() -> None:
    scene = _mutated()
    scene["entries"][1]["sample_id"] = scene["entries"][0]["sample_id"]
    assert any("duplicate" in e for e in validate_scene(scene))


def test_missing_sample_rejected() -> None:
    scene = _mutated()
    scene["entries"][0]["sample_id"] = ""
    assert any("missing sample" in e for e in validate_scene(scene))


def test_wrong_order_rejected() -> None:
    scene = _mutated()
    scene["entries"][0], scene["entries"][1] = (
        scene["entries"][1],
        scene["entries"][0],
    )
    assert validate_scene(scene)


def test_wrong_orientation_rejected() -> None:
    scene = _mutated()
    scene["entries"][2]["orientation"] = "bottom_up"
    assert any("orientation" in e for e in validate_scene(scene))


def test_gap_rejected() -> None:
    scene = _mutated()
    for entry in scene["entries"]:
        entry["row_lo"] += 1
        entry["row_hi"] += 1
    scene["entries"][-1]["row_hi"] = HEIGHT
    assert any("gap" in e for e in validate_scene(scene))


def test_empty_scene_rejected() -> None:
    scene = _mutated(entries=[])
    assert validate_scene(scene)
