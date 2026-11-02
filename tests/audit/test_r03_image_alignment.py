"""Audit regression: R03 image checks prove schematic registration.

Scope is schematic-only: the scene record maps sample ids to the
deterministic strip bands our own encoder writes, with schematic
depth intervals. Real NVCL core-photo alignment is unclaimed: no
core photograph is inventoried, and nothing here measures
correspondence with photographed trays.

The shared scene record (sample id, depth interval, image id, pixel
region, orientation) drives both the table view and the image view;
all views agree. The checks prove alignment in both directions:
reversed and interleaved strips fail the ordered strip check even
though their unordered histograms match, and every registration
defect -- a missing region, an out-of-bounds band, a duplicate or
missing sample mapping, wrong row order, a wrong orientation --
fails the scene check while the reference self-check passes.
"""

from __future__ import annotations

import pytest

from profiles.hylogger.imaging.checks import run_image_checks
from profiles.hylogger.imaging.codec import encode_png
from profiles.hylogger.imaging.generate import make_fixture_png, synthesize
from profiles.hylogger.imaging.scene import (
    build_schematic_scene,
    image_view,
    table_view,
    views_agree,
)
from profiles.hylogger.imaging.statistics import compute_statistics

PALETTE = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
WIDTH, HEIGHT = 24, 12


def _reference():
    return compute_statistics(make_fixture_png(WIDTH, HEIGHT, PALETTE), "reference.png")


def _reference_png() -> bytes:
    return make_fixture_png(WIDTH, HEIGHT, PALETTE)


def _scene() -> dict:
    return build_schematic_scene(_reference().to_dict(), "reference.png").to_dict()


def test_reference_self_check_passes() -> None:
    """Control: the reference image passes its own checks."""
    report = run_image_checks(_reference(), _reference_png(), scene=_scene())
    assert report.verdict == "pass"


def test_reference_self_check_passes_without_scene() -> None:
    """Control: the derived schematic scene also passes."""
    report = run_image_checks(_reference(), _reference_png())
    assert report.verdict == "pass"


def test_reversed_strips_must_not_pass() -> None:
    """Depth strips in reverse order must not pass alignment."""
    candidate = make_fixture_png(WIDTH, HEIGHT, list(reversed(PALETTE)))
    report = run_image_checks(_reference(), candidate, scene=_scene())
    assert report.verdict == "fail"
    assert "image_strip_order" in report.failed


def test_interleaved_strips_must_not_pass() -> None:
    """Rows interleaved across all depth strips must not pass alignment."""
    rows = [[PALETTE[y % 4]] * WIDTH for y in range(HEIGHT)]
    candidate = encode_png(WIDTH, HEIGHT, rows)
    report = run_image_checks(_reference(), candidate, scene=_scene())
    assert report.verdict == "fail"
    assert "image_strip_order" in report.failed


def test_fitted_synthetic_passes_all_checks() -> None:
    """A synthetic fitted to the reference passes every check."""
    synthetic = synthesize(_reference().to_dict(), 7)
    report = run_image_checks(_reference(), synthetic, scene=_scene())
    assert report.verdict == "pass", [
        (r.name, r.status, r.detail) for r in report.results
    ]


def test_missing_region_fails_scene() -> None:
    """A zero-height band is a missing region and must fail."""
    scene = _scene()
    scene["entries"][1]["row_hi"] = scene["entries"][1]["row_lo"]
    report = run_image_checks(_reference(), _reference_png(), scene=scene)
    assert report.verdict == "fail"
    assert "image_scene_alignment" in report.failed


def test_out_of_bounds_region_fails_scene() -> None:
    """A band past the image height must fail."""
    scene = _scene()
    scene["entries"][-1]["row_hi"] = HEIGHT + 4
    report = run_image_checks(_reference(), _reference_png(), scene=scene)
    assert report.verdict == "fail"
    assert "image_scene_alignment" in report.failed


def test_duplicate_sample_mapping_fails_scene() -> None:
    """Two bands claiming the same sample must fail."""
    scene = _scene()
    scene["entries"][1]["sample_id"] = scene["entries"][0]["sample_id"]
    report = run_image_checks(_reference(), _reference_png(), scene=scene)
    assert report.verdict == "fail"
    assert "image_scene_alignment" in report.failed


def test_missing_sample_mapping_fails_scene() -> None:
    """An empty sample id is a missing mapping and must fail."""
    scene = _scene()
    scene["entries"][2]["sample_id"] = ""
    report = run_image_checks(_reference(), _reference_png(), scene=scene)
    assert report.verdict == "fail"
    assert "image_scene_alignment" in report.failed


def test_wrong_order_fails_scene() -> None:
    """Bands out of row order must fail."""
    scene = _scene()
    scene["entries"][0], scene["entries"][1] = (
        scene["entries"][1],
        scene["entries"][0],
    )
    report = run_image_checks(_reference(), _reference_png(), scene=scene)
    assert report.verdict == "fail"
    assert "image_scene_alignment" in report.failed


def test_wrong_orientation_fails_scene() -> None:
    """A non-top-down band must fail."""
    scene = _scene()
    scene["entries"][0]["orientation"] = "bottom_up"
    report = run_image_checks(_reference(), _reference_png(), scene=scene)
    assert report.verdict == "fail"
    assert "image_scene_alignment" in report.failed


def test_scene_geometry_mismatch_fails_scene() -> None:
    """A scene recorded for other dimensions must fail."""
    scene = _scene()
    scene["height"] = HEIGHT * 2
    report = run_image_checks(_reference(), _reference_png(), scene=scene)
    assert report.verdict == "fail"
    assert "image_scene_alignment" in report.failed


def test_shared_scene_drives_both_views() -> None:
    """The table view and the image view agree on every sample."""
    scene = _scene()
    assert views_agree(scene)
    table = table_view(scene)
    image = image_view(scene)
    assert [row["sample_id"] for row in table] == [row["sample_id"] for row in image]
    assert len(table) == len(scene["entries"]) == 4
    for entry, table_row, image_row in zip(scene["entries"], table, image):
        assert table_row["depth_top"] == entry["depth_top"]
        assert table_row["depth_bottom"] == entry["depth_bottom"]
        assert image_row["row_lo"] == entry["row_lo"]
        assert image_row["row_hi"] == entry["row_hi"]
        assert image_row["orientation"] == entry["orientation"]


def test_consumer_verifies_bundle_with_scene_members(tmp_path) -> None:
    """A bundle carrying the scene evidence verifies consumer-side."""
    import hashlib
    import json
    import zipfile

    from engine.artifacts.consumer import verify_release

    synthetic = synthesize(_reference().to_dict(), 7)
    image_report = run_image_checks(
        _reference(), synthetic, artifact_name="synthetic.png", scene=_scene()
    ).to_dict()
    assert image_report["verdict"] == "pass"
    payload = b"sample_id,depth_top\nsample-000,0.0\n"
    table_report = {
        "artifact_name": "data.csv",
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "verdict": "pass",
        "results": [],
    }
    members = {
        "data.csv": payload,
        "synthetic.png": synthetic,
        "image-validation.json": json.dumps(
            image_report, indent=2, sort_keys=True
        ).encode(),
        "validation-report.json": json.dumps(
            table_report, indent=2, sort_keys=True
        ).encode(),
    }
    checksums = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n"
        for name, body in sorted(members.items())
    )
    zip_path = tmp_path / "scene-bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, body in sorted(members.items()):
            archive.writestr(name, body)
        archive.writestr("checksums.sha256", checksums)
    outcome = verify_release(zip_path)
    assert outcome["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
    assert "synthetic.png" in outcome["members"]


@pytest.mark.parametrize("seed", [7, 8, 9])
def test_fitted_synthetics_pass_across_seeds(seed: int) -> None:
    """Fitted synthetics pass every check at several seeds."""
    synthetic = synthesize(_reference().to_dict(), seed)
    report = run_image_checks(_reference(), synthetic, scene=_scene())
    assert report.verdict == "pass"
