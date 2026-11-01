# Image registration RP-06 (R03, schematic-only)

One shared scene record binds a tabular sample row to its image
region: each entry carries a `sample_id`, a depth interval
(`depth_top`/`depth_bottom` metres), the source image name
(`image_id`), a pixel row band (`row_lo`/`row_hi`), and an
`orientation` (`top_down` only). Code: `scene.py` builds the record
(one entry per statistics strip), validates it, and derives the two
views; `checks.py` enforces it; `capabilities.py` builds the scene at
`image_synthesize`, checks it at `image_validate`, and ships it
inside `image-statistics.json`.

## What is proven

* Ordered content: `image_strip_order` compares per-strip mean
  colours in order within a literal 32.0 band. Reversed and
  interleaved strips fail even though their unordered histograms
  match exactly (the audit violation fixture).
* Registration record: `image_scene_alignment` validates the shared
  scene (no missing region, no out-of-bounds band, no duplicate or
  missing sample mapping, strictly increasing row order, `top_down`
  orientation, exact `[0, height)` coverage) and requires the scene
  geometry to match both the reference and the candidate.
* Shared views: `table_view` (sample + depth interval) and
  `image_view` (sample + pixel band + orientation) derive from the
  same entries; `views_agree` holds. The audit suite checks both
  views name the same samples in the same order.
* Bundle: the scene ships inside the existing
  `image-statistics.json` member, so release membership is unchanged
  and `engine/artifacts/consumer.py::verify_release` still passes on
  image-enabled bundles (covered by a dedicated audit test plus the
  unchanged R04 release tests).

## What is NOT claimed

Schematic-only. The pixel bands are the deterministic strip layout
our own encoder writes; the depths are schematic intervals, not
measured core-photo alignment. Real NVCL core-photo registration is
unclaimed: no core photograph is inventoried, no pixel is fitted to a
photographed tray, and nothing here measures sub-pixel or
cross-image correspondence. The scene proves the two views agree
with each other, never that either view matches a real core
photograph.

## Evidence

* `tests/audit/test_r03_image_alignment.py` passes without xfail
  (control, reversed, interleaved, fitted synthetic at seeds 7/8/9,
  seven registration-defect rejections, shared-views agreement,
  consumer bundle verification).
* `tests/profiles/hylogger/test_scene.py` unit-covers build,
  validation, and views.
* Image end-to-end (`tests/profiles/hylogger/test_critic.py`) and
  release membership (`tests/audit/test_r04_image_release.py`) pass
  with five image check results.
* Full suite, gate, and clean-archive check pass; the PDD446 golden
  bytes reproduce unchanged.
