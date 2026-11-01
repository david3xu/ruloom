# benchmark-v3-post-rp08-rework: versioned benchmark rerun (RP-08 rework)

Dated: 2026-11-01. Rerun of the Step 14 held-out protocol
(`docs/results.md`, untouched by this rerun) under the repaired
evaluation harness (`scripts/evaluate.py`, RP-08) plus the RP-08-rework
planner wire-schema fix. One benchmark, run once; no Step 14 and no
benchmark-v2 output was reused. (`docs/benchmark-v2-post-rp07.md`
records the round whose pipeline arm was unscorable on the live 400;
it is untouched by this rerun.)

## Version pins

- Benchmark name: `benchmark-v3-post-rp08-rework`
- Code: `1fac399` (RP-08 merged state) plus the rework tree at run
  time — the strict `provider_schema` wire fix in
  `profiles/hylogger/generation/plan.py`,
  `profiles/tabular/generation/plan.py`, and the three repair send
  sites — committed on branch `rp08/evaluation-harness` (this commit)
- GenerationSpec version: 1
- Profile manifests: hylogger v1, tabular v1
- Thresholds sha256:
  `84cb4664643288772c90d64773985b90f093f497771d2e964ae62fea55aec521`
- Held-out holes: `20LCDD003`, `DDG007`, `HD012`, `NCB0003`
  (`data/sources/<hole>-scalars.csv`); `PDD446` excluded as the
  development reference
- Protocol: pipeline arm `ruloom run <hole> --max-attempts 3`,
  baseline arm one structured `{csv}` call requesting 1000
  rows, same disclosed statistics boundary as Step 14
- Evidence (git-ignored, never committed):
  `runs/evaluation/20261101T190018Z/` — per-hole JSON,
  pipeline run directories, per-hole `baseline/` raw bytes plus
  invocation records, `summary.json`

## Headline (from saved artifacts, recompute-verified)

| Measure | pipeline (n=4) | baseline (n=4) |
| --- | --- | --- |
| Constraints pass | 4 | 4 |
| Fidelity pass | 4 | 2 |
| Leakage pass | 4 | 4 |
| Reproducible | 4 | 0 |
| Held-out holes pass | 4 | 2 |
| Unscorable holes | 0 | 0 |
| Attempts used (total) | 4 | 0 |
| Model tokens (total) | 134806 | 4633554 |
| Wall time s (total) | 80.0 | 1220.2 |

`scripts/evaluate.py --recompute --out
runs/evaluation/20261101T190018Z` exits 0: the stored
summary matches the per-hole artifacts on every headline key.

## Per-hole results

| Hole | Arm | Verdict | Constraints | Fidelity | Leakage | Rows | Tokens | Wall s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20LCDD003 | pipeline | pass (attempt 1) | pass | pass | pass | 30600 | 31159 | 18.1 |
| 20LCDD003 | baseline | fail (`repeated_depths_masked_out`) | pass | fail | pass | 1000/1000 | 682907 | 355.0 |
| DDG007 | pipeline | pass (attempt 1) | pass | pass | pass | 32400 | 31753 | 23.6 |
| DDG007 | baseline | fail (`repeated_depths_masked_out`) | pass | fail | pass | 1000/1000 | 753492 | 320.0 |
| HD012 | pipeline | pass (attempt 1) | pass | pass | pass | 10260 | 34391 | 16.4 |
| HD012 | baseline | pass | pass | pass | pass | 1000/1000 | 570335 | 152.9 |
| NCB0003 | pipeline | pass (attempt 1) | pass | pass | pass | 124520 | 37503 | 21.9 |
| NCB0003 | baseline | pass | pass | pass | pass | 1000/1000 | 2626820 | 392.3 |

Rows: pipeline = artifact row count (equals the source hole's row
count on all four holes); baseline = parsed/requested. Baseline
reproducibility is 0/4 by construction (no seed exists). Pipeline
reproducibility is 4/4 (seeded regeneration from the saved plan
reproduces the packaged bytes exactly). Token counts are
adapter-reported per-call usage summed from the saved invocation
records.

## Why the pipeline arm scores now (rework fix, not harness)

All four pipeline invocations passed the `[plan]` step that 400'd in
benchmark-v2. The product planner no longer sends `PLAN_SCHEMA`
verbatim: each plan module exposes `provider_schema()`, a strict wire
schema whose `required` covers every declared property, sent at all
five planner send sites (both `propose` functions and all three
repair paths). With bound strategies the wire schema requires
`seed`/`n_rows`/`strategy`; without bound strategies it carries
`seed`/`n_rows` only. Validation still runs on `PLAN_SCHEMA`, so the
optional-`strategy` contract is unchanged. Every saved live plan
carries `"strategy": "fitted_markov_joint"` (the manifest's single
declared strategy), each pipeline exit code is 0 with a fresh
invocation record, and no stderr tail contains a 400. Fake-model
suites cover the strictness (`provider_schema` required-covers-all
regression tests plus captured-sent-schema assertions in both
profiles' plan, repair, and capabilities tests).

## What is claimed, and what is not

- Claimed: with the wire-schema fix, the pipeline arm scores 4/4
  first-attempt passes against the same deterministic checks as Step
  14, reproducibly (4/4 regeneration match), at 134806 tokens and
  80.0 s wall; the baseline arm scores 2/4 fresh (both failures are
  the `repeated_depths_masked_out` fidelity mode), at 4633554 tokens
  and 1220.2 s wall; the summary recomputes from disk (exit 0).
- Not claimed: a general pipeline-vs-baseline superiority result.
  Both arms are single-run samples and the model is stochastic: the
  HD012 baseline failed fidelity in benchmark-v2 and passes here,
  with no product change in between, which is run-to-run variance,
  not an improvement. The pipeline-vs-baseline gap in this round
  (4/4 vs 2/4 holes, ~34x fewer tokens, ~15x faster wall) is a
  measured single-round outcome under the stated protocol, not a
  distributional claim.
