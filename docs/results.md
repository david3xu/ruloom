# Step 14: held-out evaluation (measured results)

## Protocol (followed exactly; numbers below are its output)

Held-out holes are the four non-reference sources under `data/sources`
(`20LCDD003`, `DDG007`, `HD012`, `NCB0003` with `-scalars.csv` suffix).
`PDD446` is the development reference and is excluded. Calibration bands
were fitted on the `data/calibration` holes, never on these four.

Pipeline arm: for each held-out hole, the real product CLI
(`.venv/bin/ruloom run <hole> --max-attempts 3`, stdin `accept`) was run
once. Recorded from each run directory: verdict, per-group check outcomes
(constraints / fidelity / leakage as grouped below), attempts used,
artifact sha256, model usage tokens, wall time.

Baseline arm (direct-LLM adversary, same product model `muse-spark-1.3`
contributor via `models/meta_cli.py`): for each held-out hole, the model
received the SAME statistics summary the planner sees (`build_prompt`
over the hole `Profile`) plus an instruction to emit CSV rows directly.
Exactly 1000 rows were requested in one structured call (`{csv: string}`).
The returned text was parsed with the product CSV reader and scored with
the SAME check code (`profiles/hylogger/validation/checks.py`) against
the same thresholds. Requested vs parsed rows are recorded honestly per
hole; a parse failure would have been recorded as an `error` verdict with
zero parsed rows.

Asymmetry (stated, not adjusted): the pipeline generates the plan's full
`n_rows` (the planner chose the source row count on every hole: 30600,
32400, 10260, 124520), while the baseline is capped at 1000 emitted rows
because model output length is bounded. Distributional metrics are
compared; exact-count metrics are reported per arm, never pooled.

Reproducibility: pipeline = regenerate-from-plan hash match, measured per
hole by rebuilding the `Profile` from the source CSV and re-running the
seeded generator (match yes/no stated per hole). Baseline = no seed
exists, recorded as not reproducible by construction; the baseline was
never run twice to fake a hash comparison.

Grouping of check outcomes follows the manifest's own `kind` mapping
(constraint vs fidelity). Leakage has no dedicated check in the product;
the harness operationalises it as the vocabulary pair
(`swir_vocabulary` + `tir_vocabulary`): synthetic labels stay inside the
calibrated source vocabulary. That definition is stated here, so the
Leakage row stays a measured value.

Harness: `scripts/evaluate.py` (stdlib plus project imports) with
`--holes`, `--max-attempts`, `--baseline-rows`, `--out
runs/evaluation/<timestamp>/`, writing `<hole>.json` per hole plus
`summary.json`. Unit tests in `tests/test_evaluate.py` use a fake model
and fixture CSVs (no network) covering scoring aggregation, baseline CSV
parsing (good, truncated, garbage), results-table rendering from summary
JSON, and per-hole JSON writing. Evidence for this document:
`runs/evaluation/20261101T110153Z/` (per-hole JSON, `summary.json`).

## Per-hole results

`swir_answer_rate` was skipped on both arms for all four holes: the
calibrated band is the vacuous `[0.0, 1.0]`, so synthetic SWIR
missingness is unvalidated on either arm. The skip is counted in the
fidelity group below (skips do not fail the product verdict).

| Hole | Arm | Verdict | Constraints | Fidelity | Leakage | Attempts | Artifact / rows | Model tokens (server-reported) | Wall s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20LCDD003 (30600 source rows) | pipeline | pass | pass (5/5) | pass (8, 1 skip) | pass (2/2) | 1 | `89ad39198c28eecdcf83ba5abf41089fce765eb43376548f9b88472195c84877` / 30600 | 31205 (1 call) | 15.1 |
| 20LCDD003 | baseline | pass | pass (6/6) | pass (8, 1 skip) | pass (2/2) | n/a (1 call, no repair loop) | 1000 requested / 1000 parsed | 1310280 | 340.5 |
| DDG007 (32400 source rows) | pipeline | pass | pass (5/5) | pass (8, 1 skip) | pass (2/2) | 1 | `7332003fc6fdfd08401a0af86afc2a86d591023d846af91fb98ffe3a107fe760` / 32400 | 31429 (1 call) | 13.2 |
| DDG007 | baseline | pass | pass (6/6) | pass (8, 1 skip) | pass (2/2) | n/a (1 call, no repair loop) | 1000 requested / 1000 parsed | 685823 | 315.4 |
| HD012 (10260 source rows) | pipeline | pass | pass (5/5) | pass (8, 1 skip) | pass (2/2) | 1 | `20dc5978a63746128005f5f119a652b07c392df6398b154d7395c2d122672430` / 10260 | 34057 (1 call) | 17.2 |
| HD012 | baseline | pass | pass (6/6) | pass (8, 1 skip) | pass (2/2) | n/a (1 call, no repair loop) | 1000 requested / 1000 parsed | 931290 | 273.6 |
| NCB0003 (124520 source rows) | pipeline | pass | pass (5/5) | pass (8, 1 skip) | pass (2/2) | 1 | `ec5d0cd17382ee6449ccd506d8d65f9b95fa1f0caa24a8c1dfdaeef686f622e6` / 124520 | 37093 (1 call) | 17.9 |
| NCB0003 | baseline | **fail** | pass (6/6) | **fail** (`repeated_depths_masked_out`) | pass (2/2) | n/a (1 call, no repair loop) | 1000 requested / 1000 parsed | 2641749 | 657.7 |

Baseline check counts differ from pipeline counts by one constraint
check: the harness prepends a `csv_schema` check to baseline reports
(the product's own validate step does not emit one). Group outcomes are
unaffected by the count difference.

The NCB0003 baseline failure is a fidelity failure, not a parse failure:
all 1000 rows parsed cleanly, but `repeated_depths_masked_out` measured
`0.754902 outside [0.0, 0.157337]` (band position 4.80): the directly
emitted rows repeat depths the validity mask excludes. The pipeline's
seeded generator measured `0.0 in [0.0, 0.157337]` (pass) on the same
check for the same hole.

Pipeline plans (from each run's `plan.json`): seed 42 on all four holes;
`n_rows` equal to the source row count on all four holes. Reproduce-from
plan hash match: yes on all four holes (regenerated bytes identical to
the run artifact). Baseline reproducibility: no on all four holes (no
seed exists, by construction).

## Aggregate (07 section 11 shape)

| Measure | pipeline (n=4) | baseline (n=4) |
| --- | --- | --- |
| Constraints pass | 4 | 4 |
| Fidelity pass | 4 | 3 |
| Leakage pass | 4 | 4 |
| Reproducible | 4 | 0 |
| Held-out holes pass | 4 | 3 |
| Attempts used (total) | 4 | n/a (1 model call per hole, no repair loop) |
| Model tokens (total, server-reported) | 133784 | 5569142 |
| Wall time s (total) | 63.4 | 1587.2 |

The pipeline won the fidelity measure 4-3 (NCB0003 baseline failed
`repeated_depths_masked_out` as detailed above). Constraints, leakage,
and per-hole verdicts otherwise agree; reproducibility holds for the
pipeline by hash match and cannot hold for the baseline by construction.
The baseline used about 42x the model tokens and 25x the wall time,
dominated by emitting 1000 literal rows per hole through the model.

## Threats to validity

- The baseline is capped at 1000 emitted rows per hole while the
  pipeline generates the full plan row count, so count-sensitive checks
  are reported per arm and only distributional behaviour is compared.
- One run per hole per arm: run-to-run variance of either arm is not
  measured, and the single baseline sample per hole may flatter or
  punish the adversary.
- Baseline parse behaviour: all four baselines parsed fully here
  (1000/1000, no truncation, no garbage), so the parse-failure path was
  exercised only by the offline unit tests, not by a live refusal.
- Token counts are server-reported usage attached to model responses,
  not independently metered; wall time is harness-measured.
- `swir_answer_rate` is skipped on both arms (vacuous `[0.0, 1.0]`
  band), so neither arm's SWIR missingness is validated by this
  evaluation.
