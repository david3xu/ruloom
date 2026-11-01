"""Audit counterexample fixtures: R03 image checks ignore alignment."""

from __future__ import annotations

import pytest

from profiles.hylogger.imaging.checks import run_image_checks
from profiles.hylogger.imaging.codec import encode_png
from profiles.hylogger.imaging.generate import make_fixture_png
from profiles.hylogger.imaging.statistics import compute_statistics

needs_fix = pytest.mark.xfail(
    strict=True, reason="audit R03 open: image checks do not test alignment"
)

PALETTE = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 220)]
WIDTH, HEIGHT = 24, 12


def _reference():
    return compute_statistics(
        make_fixture_png(WIDTH, HEIGHT, PALETTE), "reference.png"
    )


def _reference_png() -> bytes:
    return make_fixture_png(WIDTH, HEIGHT, PALETTE)


def test_reference_self_check_passes() -> None:
    """Control: the reference image passes its own checks."""
    report = run_image_checks(_reference(), _reference_png())
    assert report.verdict == "pass"


@needs_fix
def test_reversed_strips_must_not_pass() -> None:
    """Depth strips in reverse order must not pass alignment."""
    candidate = make_fixture_png(WIDTH, HEIGHT, list(reversed(PALETTE)))
    report = run_image_checks(_reference(), candidate)
    assert report.verdict == "fail"


@needs_fix
def test_interleaved_strips_must_not_pass() -> None:
    """Rows interleaved across all depth strips must not pass alignment."""
    rows = [[PALETTE[y % 4]] * WIDTH for y in range(HEIGHT)]
    candidate = encode_png(WIDTH, HEIGHT, rows)
    report = run_image_checks(_reference(), candidate)
    assert report.verdict == "fail"
