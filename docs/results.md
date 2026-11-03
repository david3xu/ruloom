# Step 14 held-out evaluation: measured results

## Protocol

Held-out holes: `20LCDD003`, `DDG007`, `HD012`, `NCB0003`
(`data/sources/<hole>-scalars.csv`). `PDD446` is the development reference
and is excluded. Calibration bands in `profiles/hylogger/thresholds.json`
were fitted on the 38 `data/calibration` holes, never on these four.

Pipeline arm: for each held-out hole, the real product CLI
(`.venv/bin/ruloom run <hole> --max-attempts 3`, stdin `accept`) was run
once. Recorded from its run directory: verdict, per-group check outcomes,
attempts used, artifact sha256, model usage tokens (input + output +
reasoning as the adapters report), wall time.

Baseline arm (direct-LLM adversary, same product model `muse-spark-1.3`
contributor via `models/meta_cli.py`): for each held-out hole the model
received the SAME statistics summary the planner sees plus an instruction
to emit CSV rows directly; one structured call `{csv: string}` requesting
exactly 1000 rows. Rows were parsed with the product CSV reader and scored
with the SAME check code (`profiles/hylogger/validation/checks.py`)
against the same thresholds. Requested vs parsed rows are recorded
honestly; a refused or unparsable answer scores verdict `error` with 0
parsed rows.

Asymmetry: the pipeline generates the plan's full `n_rows` while the
baseline is capped at 1000 emitted rows for output limits. Distributional
metrics are compared; exact-count metrics are reported per arm, never
pooled.

Reproducibility: pipeline = regenerate-from-plan hash match, measured per
hole by rebuilding the profile from the source CSV and re-running the
seeded generator. Baseline = no seed exists, recorded as not reproducible
by construction (the baseline was not run twice to fake a comparison).

Check grouping follows the manifest's own `kind` mapping (constraint vs
fidelity; skips permitted, matching product verdict semantics). Leakage
has no dedicated product check; the harness operationalises it as the
vocabulary pair (`swir_vocabulary`, `tir_vocabulary`): synthetic labels
stay inside the calibrated source vocabulary. That pair also counts in
constraints under its manifest kind.

Harness: `scripts/evaluate.py` (standard library plus project imports)
with `--holes`, `--max-attempts`, `--baseline-rows`, `--out`; writes
per-hole JSON plus `summary.json`. Unit tests in `tests/test_evaluate.py`
use a fake model and fixture CSVs. Evidence:
`runs/evaluation/20261101T104500Z/` (per-hole JSON, run directories,
`summary.json`). One run per hole per arm.

## Per-hole results

| Hole | Arm | Verdict | Constraints | Fidelity | Leakage | Reproducible | Attempts | Rows | Tokens | Wall s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20LCDD003 | pipeline | pass | pass | pass | pass | yes | 1 | 30600 | 31184 | 22.0 |
| 20LCDD003 | baseline | fail | pass | fail | pass | no | n/a | 1000/1000 | 1392212 | 609.8 |
| DDG007 | pipeline | pass | pass | pass | pass | yes | 1 | 32400 | 32466 | 19.8 |
| DDG007 | baseline | fail | pass | fail | pass | no | n/a | 1000/1000 | 220246 | 203.1 |
| HD012 | pipeline | pass | pass | pass | pass | yes | 1 | 10260 | 34239 | 19.2 |
| HD012 | baseline | fail | pass | fail | pass | no | n/a | 1000/1000 | 3804933 | 872.2 |
| NCB0003 | pipeline | pass | pass | pass | pass | yes | 1 | 124520 | 37320 | 21.2 |
| NCB0003 | baseline | pass | pass | pass | pass | no | n/a | 1000/1000 | 948946 | 254.8 |

Rows: pipeline = artifact rows (plan `n_rows` equalled source rows on all
four holes, seed 42 each time); baseline = parsed/requested. Attempts:
pipeline attempts used; baseline makes one model call (`n/a`). Baseline
fidelity failures: `repeated_depths_masked_out` on 20LCDD003, DDG007,
HD012. `swir_answer_rate` was skipped on both arms for all four holes:
the calibrated band is the vacuous `[0.0, 1.0]`, so synthetic SWIR
missingness is unvalidated on either arm (the skip sits in the fidelity
group; skips do not fail the product verdict). Baseline and
pipeline reports both carry the `csv_schema` check first: the
product's own validate step records it in `run_checks`, and the
harness mirrors that same step; group outcomes are unaffected.
Pipeline artifact sha256 per hole: 20LCDD003 `89ad39198c28…`, DDG007
`7332003fc6fd…`, HD012 `20dc5978a637…`, NCB0003 `ec5d0cd17382…` (full
hashes in the per-hole JSON).

## Aggregate (07 section 11 shape)

Held-out holes: 20LCDD003, DDG007, HD012, NCB0003.

| Measure | pipeline (n=4) | baseline (n=4) |
| --- | --- | --- |
| Constraints pass | 4 | 4 |
| Fidelity pass | 4 | 1 |
| Leakage pass | 4 | 4 |
| Reproducible | 4 | 0 |
| Held-out holes pass | 4 | 1 |
| Attempts used (total) | 4 | 0 |
| Model tokens (total) | 135209 | 6366337 |
| Wall time s (total) | 82.2 | 1939.9 |

The pipeline does not beat the baseline on NCB0003: both arms pass every
group there. Everywhere else the pipeline passes and the baseline fails
fidelity. The baseline used about 47x the model tokens and about 24x the
wall time for 1000 rows per hole against full-hole pipeline artifacts.

## Independent repeat

The protocol was executed a second time on a shared branch (evidence
`runs/evaluation/20261101T110153Z/summary.json`, verified against its
stored JSON, not rerun here). Pipeline: 4/4 pass every group,
reproducible 4/4, 133784 tokens, 63.4 s wall, with bit-identical artifact
hashes to this run on all four holes. Baseline: 3/4 pass (fidelity 3/4;
the NCB0003 sample failed `repeated_depths_masked_out` while the other
three passed), 5569142 tokens, 1587.2 s wall. The pipeline result is
stable across repeats down to the byte; the baseline fidelity result
moves run to run, which is why the tables above report this run only.

## Threats to validity

1000-row cap: the baseline emits 1000 rows per hole while pipeline
artifacts hold 10260–124520 rows; count-sensitive checks
(`repeated_depths_masked_out`, vocabulary coverage) behave differently at
different row counts, so the fidelity gap is conditional on this cap.
Single run per hole per arm in the tables above: model calls are
stochastic, and the independent repeat confirms baseline fidelity varies
(1/4 here, 3/4 there) while pipeline verdicts and hashes did not move.
Baseline parse behaviour: all four baseline answers parsed cleanly
(1000/1000, no truncation, no refusal), so the parse-failure path is
covered by unit tests only, not by a live failure. Token `calls` counts
are adapter-reported run.usage aggregates, not independently audited per
call. Reproducibility for the pipeline covers the generator only (plan
fixed); planner nondeterminism across runs was not measured.
