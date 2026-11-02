# CP-02 scorecard addendum: TIR persistence after the transition repair

Dated 2026-11-02. Addendum to `docs/quality/scorecard-rp05.md` (2026-11-01);
that file's numbers are quoted read-only below and left untouched.
Each cell generates a full-length synthetic hole from the hole's own
real profile with the repaired generator and scores it with the
product checks in full production parity.

## Repair (A04)

- Both-answered rows used to draw TIR from the joint counts
  conditioned on the SWIR label, resetting TIR runs (PDD446 source
  0.743 -> synthetic ~0.49; 20LCDD003 0.895 -> ~0.53).
- Both-answered rows now draw TIR through the TIR transition
  structure (conditioned on the previous answered TIR label); the
  joint counts survive only as the no-transition fallback.
- Transition rows with fewer than two outgoing pairs are not
  trusted (singleton self-loops absorb the chain: PDD446 GARNET,
  one self-pair, trapped 83% of seed-7 answers pre-floor); those
  draws fall back the same way. The floor of 2 mirrors
  `MIN_LABEL_COUNT_FOR_MODEL_VIEW`: one observation is not a pattern.
- One `random.Random` still drives every stochastic choice, so the
  same `(profile, seed, n_rows)` triple yields identical CSV bytes.

## Declared band

- `tir_persistence_fidelity` (required): synthetic TIR persistence
  within the source hole's own split-half delta, plus the frozen
  cross-hole p95 of 0.155469, plus a 2/sqrt(n) finite-sample
  allowance over the artifact's answered pairs.
- The p95 is the 95th percentile of within-hole split-half
  `|delta|` of TIR persistence across the 38 frozen calibration
  holes (`thresholds.json` provenance; min 0.002808, median
  0.052531, max 0.178780). Recipe: per calibration hole,
  `|persistence(first half) - persistence(second half)|` via
  `build_stats` on `data/calibration`, then the 95th percentile.
  `tests/profiles/hylogger/test_calibration.py` pins the committed
  value whenever `data/calibration` is present.

## Per-hole results

### PDD446 (n=39300)

Source TIR persistence: 0.742742; RP-05 synthetic: seed-446 0.486069, range 0.486069..0.494692.

| seed | verdict | persist synth | drift (tol) | joint agree src -> synth |
|---|---|---|---|---|
| 446 | pass | 0.741878 | TIR persistence drift 0.000864 <= 0.195709 | 0.517091 -> 0.427145 |
| 7 | pass | 0.740642 | TIR persistence drift 0.002100 <= 0.195595 | 0.517091 -> 0.427907 |
| 12345 | pass | 0.743893 | TIR persistence drift 0.001151 <= 0.195609 | 0.517091 -> 0.428951 |

### HD012 (n=10260)

Source TIR persistence: 0.724789; RP-05 synthetic: seed-446 0.441907, range 0.441907..0.452349.

| seed | verdict | persist synth | drift (tol) | joint agree src -> synth |
|---|---|---|---|---|
| 446 | pass | 0.717974 | TIR persistence drift 0.006815 <= 0.293631 | 0.577419 -> 0.289663 |
| 7 | pass | 0.714495 | TIR persistence drift 0.010294 <= 0.292884 | 0.577419 -> 0.285494 |
| 12345 | pass | 0.727042 | TIR persistence drift 0.002253 <= 0.292916 | 0.577419 -> 0.300093 |

### DDG007 (n=32400)

Source TIR persistence: 0.777000; RP-05 synthetic: seed-446 0.398590, range 0.394293..0.398590.

| seed | verdict | persist synth | drift (tol) | joint agree src -> synth |
|---|---|---|---|---|
| 446 | pass | 0.776327 | TIR persistence drift 0.000673 <= 0.173698 | 0.194957 -> 0.071591 |
| 7 | pass | 0.772595 | TIR persistence drift 0.004405 <= 0.173672 | 0.194957 -> 0.063456 |
| 12345 | pass | 0.776795 | TIR persistence drift 0.000205 <= 0.173731 | 0.194957 -> 0.063182 |

### 20LCDD003 (n=30600)

Source TIR persistence: 0.895147; RP-05 synthetic: seed-446 0.531025, range 0.531025..0.541920.

| seed | verdict | persist synth | drift (tol) | joint agree src -> synth |
|---|---|---|---|---|
| 446 | pass | 0.890324 | TIR persistence drift 0.004823 <= 0.199517 | 0.030798 -> 0.022668 |
| 7 | pass | 0.898892 | TIR persistence drift 0.003745 <= 0.199266 | 0.030798 -> 0.018625 |
| 12345 | pass | 0.895295 | TIR persistence drift 0.000148 <= 0.199375 | 0.030798 -> 0.021868 |

### NCB0003 (n=124520)

Source TIR persistence: 0.711114; RP-05 synthetic: seed-446 0.435233, range 0.429761..0.435233.

| seed | verdict | persist synth | drift (tol) | joint agree src -> synth |
|---|---|---|---|---|
| 446 | pass | 0.712441 | TIR persistence drift 0.001327 <= 0.245591 | 0.631524 -> 0.189353 |
| 7 | pass | 0.712034 | TIR persistence drift 0.000920 <= 0.245575 | 0.631524 -> 0.188075 |
| 12345 | pass | 0.706195 | TIR persistence drift 0.004919 <= 0.245598 | 0.631524 -> 0.188380 |

## Before/after TIR persistence

| hole | persist src | persist synth before (rp05) | persist synth after (min..max over seeds) |
|---|---|---|---|
| PDD446 | 0.742742 | 0.486069, 0.486069..0.494692 | 0.740642..0.743893 |
| HD012 | 0.724789 | 0.441907, 0.441907..0.452349 | 0.714495..0.727042 |
| DDG007 | 0.777000 | 0.398590, 0.394293..0.398590 | 0.772595..0.776795 |
| 20LCDD003 | 0.895147 | 0.531025, 0.531025..0.541920 | 0.890324..0.898892 |
| NCB0003 | 0.711114 | 0.435233, 0.429761..0.435233 | 0.706195..0.712441 |

## Verdict summary

| hole | seed | verdict | failed | skipped |
|---|---|---|---|---|
| PDD446 | 446 | pass | - | swir_answer_rate |
| PDD446 | 7 | pass | - | swir_answer_rate |
| PDD446 | 12345 | pass | - | swir_answer_rate |
| HD012 | 446 | pass | - | swir_answer_rate |
| HD012 | 7 | pass | - | swir_answer_rate |
| HD012 | 12345 | pass | - | swir_answer_rate |
| DDG007 | 446 | pass | - | swir_answer_rate |
| DDG007 | 7 | pass | - | swir_answer_rate |
| DDG007 | 12345 | pass | - | swir_answer_rate |
| 20LCDD003 | 446 | pass | - | swir_answer_rate |
| 20LCDD003 | 7 | pass | - | swir_answer_rate |
| 20LCDD003 | 12345 | pass | - | swir_answer_rate |
| NCB0003 | 446 | pass | - | swir_answer_rate |
| NCB0003 | 7 | pass | - | swir_answer_rate |
| NCB0003 | 12345 | pass | - | swir_answer_rate |

## Interpretation

- Every cell passes all required checks on every hole and seed,
  including the new `spacing_step_match`, `depth_span_match`
  and `tir_persistence_fidelity` checks; the only skip remains
  the known-vacuous `swir_answer_rate` band.
- Synthetic TIR persistence now sits within ~0.01 of source on
  every cell (before: 0.18..0.38 below source).
- Joint SWIR/TIR agreement is lower than under the joint draw
  (both-answered TIR no longer conditions on the SWIR label);
  it is disclosed here, not gated -- no threshold exists for it,
  exactly as in RP-05. Depth-dependent interpretation that needs
  joint co-occurrence (e.g. alteration zoning) stays unclaimed.
- Generator bytes move deliberately: the PDD446 seed-42 golden
  artifact goes from
  `be9e26d7e6640c4acc8ef35b4f9487f2c3d114142d47b5b68d21daca76989ab3`
  to `55eb1c22e212cc61523548875af5e98b3c040aecdb7d9dc7ac59d7836f58d85f`
  (same profile/seed/n_rows; verdict pass).
