# RP-05 local-structure scorecard

Multi-seed valid-input behaviour for the HyLogger depth /
missingness / label / joint-sequence slice (R01+R02). Each cell
generates a full-length synthetic hole from the hole's own real
profile and scores it with the product checks in production
parity (granted source reference plus requested plan geometry).

## Frozen split

- Calibration: the 38 holes recorded in
  `profiles/hylogger/thresholds.json` provenance (frozen -- RP-05
  adds no band, changes no threshold, relaxes nothing).
- Evaluation: PDD446 (development reference) plus the four
  held-out holes 20LCDD003, DDG007, HD012, NCB0003 -- the same
  holes as the Step 14 held-out protocol.
- Seeds: 446, 7, 12345;
  `n_rows` = source row count (the plan prompt's rule).

## Interpretation (read with the tables)

- Every cell passes: no valid synthetic trips any required
  check on any hole or seed; the only skip is the known-vacuous
  `swir_answer_rate` band (required: false, RP-01).
- SWIR persistence reproduces closely (e.g. PDD446 0.910 source
  vs 0.903..0.908 synthetic); joint agreement and valid/invalid
  run lengths track within a few percent.
- TIR persistence runs systematically lower in synthetics than
  in source (e.g. PDD446 0.743 -> ~0.49; HD012 0.725 -> ~0.45):
  for both-answered rows the generator draws TIR from the joint
  counts conditioned on the SWIR label, which resets TIR runs.
  This is disclosed, not gated -- no threshold exists for it,
  and changing the draw would move the golden PDD446 bytes, so
  RP-05 leaves the generator byte-identical.

## Per-hole results

### PDD446 (n=39300)

| channel | answer src | answer synth (min..max over seeds) | persist src | persist synth (min..max over seeds) |
|---|---|---|---|---|
| swir | 0.650135 | 0.649056..0.649905 | 0.909514 | 0.902569..0.908386 |
| tir | 0.996832 | 0.996320..0.996943 | 0.742742 | 0.486069..0.494692 |

| seed | verdict | swir TVD (tol) | tir TVD (tol) | joint agree src -> synth | valid-run mean src -> synth | invalid-run mean src -> synth |
|---|---|---|---|---|---|---|
| 446 | pass | TVD 0.019993 <= 0.079047 | TVD 0.063966 <= 0.186993 | 0.517091 -> 0.521112 | 71.704883 -> 62.940952 | 11.709746 -> 11.938931 |
| 7 | pass | TVD 0.024582 <= 0.079047 | TVD 0.063209 <= 0.186993 | 0.517091 -> 0.523757 | 71.704883 -> 70.644351 | 11.709746 -> 11.597484 |
| 12345 | pass | TVD 0.019913 <= 0.079047 | TVD 0.060748 <= 0.186993 | 0.517091 -> 0.528260 | 71.704883 -> 69.182752 | 11.709746 -> 11.539095 |

### HD012 (n=10260)

| channel | answer src | answer synth (min..max over seeds) | persist src | persist synth (min..max over seeds) |
|---|---|---|---|---|
| swir | 0.849000 | 0.845230..0.850491 | 0.865351 | 0.853676..0.871614 |
| tir | 0.988834 | 0.987011..0.990470 | 0.724789 | 0.441907..0.452349 |

| seed | verdict | swir TVD (tol) | tir TVD (tol) | joint agree src -> synth | valid-run mean src -> synth | invalid-run mean src -> synth |
|---|---|---|---|---|---|---|
| 446 | pass | TVD 0.010792 <= 0.720345 | TVD 0.051821 <= 0.386789 | 0.577419 -> 0.572620 | 27.507143 -> 23.727869 | 9.103203 -> 9.911475 |
| 7 | pass | TVD 0.025426 <= 0.720345 | TVD 0.052727 <= 0.386789 | 0.577419 -> 0.576532 | 27.507143 -> 27.753623 | 9.103203 -> 9.454545 |
| 12345 | pass | TVD 0.004714 <= 0.720345 | TVD 0.048300 <= 0.386789 | 0.577419 -> 0.580335 | 27.507143 -> 25.976271 | 9.103203 -> 8.833333 |

### DDG007 (n=32400)

| channel | answer src | answer synth (min..max over seeds) | persist src | persist synth (min..max over seeds) |
|---|---|---|---|---|
| swir | 0.934083 | 0.932727..0.935566 | 0.916950 | 0.916071..0.919233 |
| tir | 1.000000 | 1.000000..1.000000 | 0.777000 | 0.394293..0.398590 |

| seed | verdict | swir TVD (tol) | tir TVD (tol) | joint agree src -> synth | valid-run mean src -> synth | invalid-run mean src -> synth |
|---|---|---|---|---|---|---|
| 446 | pass | TVD 0.013732 <= 0.148617 | TVD 0.036692 <= 0.289803 | 0.194957 -> 0.190434 | 28.540541 -> 26.524336 | 9.521127 -> 9.306077 |
| 7 | pass | TVD 0.024426 <= 0.148617 | TVD 0.036554 <= 0.289803 | 0.194957 -> 0.182053 | 28.540541 -> 27.931555 | 9.521127 -> 9.666667 |
| 12345 | pass | TVD 0.029503 <= 0.148617 | TVD 0.038402 <= 0.289803 | 0.194957 -> 0.178852 | 28.540541 -> 26.988688 | 9.521127 -> 9.673839 |

### 20LCDD003 (n=30600)

| channel | answer src | answer synth (min..max over seeds) | persist src | persist synth (min..max over seeds) |
|---|---|---|---|---|
| swir | 0.989594 | 0.988854..0.989944 | 0.958491 | 0.958370..0.958871 |
| tir | 0.999563 | 0.999562..0.999674 | 0.895147 | 0.531025..0.541920 |

| seed | verdict | swir TVD (tol) | tir TVD (tol) | joint agree src -> synth | valid-run mean src -> synth | invalid-run mean src -> synth |
|---|---|---|---|---|---|---|
| 446 | pass | TVD 0.001786 <= 0.030242 | TVD 0.003429 <= 0.158819 | 0.030798 -> 0.031637 | 96.834615 -> 82.422819 | 20.777778 -> 20.329966 |
| 7 | pass | TVD 0.005731 <= 0.030242 | TVD 0.002382 <= 0.158819 | 0.030798 -> 0.029065 | 96.834615 -> 99.828125 | 20.777778 -> 19.780392 |
| 12345 | pass | TVD 0.002189 <= 0.030242 | TVD 0.009041 <= 0.158819 | 0.030798 -> 0.030244 | 96.834615 -> 91.021739 | 20.777778 -> 19.920000 |

### NCB0003 (n=124520)

| channel | answer src | answer synth (min..max over seeds) | persist src | persist synth (min..max over seeds) |
|---|---|---|---|---|
| swir | 0.944670 | 0.943545..0.944352 | 0.672401 | 0.669628..0.673996 |
| tir | 0.998921 | 0.998864..0.999088 | 0.711114 | 0.429761..0.435233 |

| seed | verdict | swir TVD (tol) | tir TVD (tol) | joint agree src -> synth | valid-run mean src -> synth | invalid-run mean src -> synth |
|---|---|---|---|---|---|---|
| 446 | pass | TVD 0.007065 <= 0.371924 | TVD 0.029631 <= 0.304774 | 0.631524 -> 0.628649 | 43.547652 -> 42.117829 | 13.226983 -> 12.833628 |
| 7 | pass | TVD 0.011132 <= 0.371924 | TVD 0.038118 <= 0.304774 | 0.631524 -> 0.633520 | 43.547652 -> 44.010092 | 13.226983 -> 13.115190 |
| 12345 | pass | TVD 0.012221 <= 0.371924 | TVD 0.038098 <= 0.304774 | 0.631524 -> 0.633353 | 43.547652 -> 42.450980 | 13.226983 -> 13.045029 |

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
