# benchmark-v2-post-rp07: versioned benchmark rerun (RP-08)

Dated: 2026-11-01. Rerun of the Step 14 held-out protocol
(`docs/results.md`, untouched by this rerun) under the repaired
evaluation harness (`scripts/evaluate.py`, RP-08). One benchmark, run
once; no Step 14 output was reused.

## Version pins

- Benchmark name: `benchmark-v2-post-rp07`
- Code commit: `f1ed40d` (RP-07 merged state plus the RP-08 harness
  repair on branch `rp08/evaluation-harness`)
- GenerationSpec version: 1
- Profile manifests: hylogger v1, tabular v1
- Thresholds sha256:
  `84cb4664643288772c90d64773985b90f093f497771d2e964ae62fea55aec521`
- Held-out holes: `20LCDD003`, `DDG007`, `HD012`, `NCB0003`
  (`data/sources/<hole>-scalars.csv`); `PDD446` excluded as the
  development reference
- Protocol: pipeline arm `ruloom run <hole> --max-attempts 3`,
  baseline arm one structured `{csv: string}` call requesting 1000
  rows, same disclosed statistics boundary as Step 14
- Evidence (git-ignored, never committed):
  `runs/evaluation/20261101T180552Z-benchmark-v2/` — per-hole JSON,
  pipeline run directories, per-hole `baseline/` raw bytes plus
  invocation records, `summary.json`

## Headline (from saved artifacts, recompute-verified)

| Measure | pipeline (n=0 scored, 4 unscorable) | baseline (n=4) |
| --- | --- | --- |
| Constraints pass | 0 | 4 |
| Fidelity pass | 0 | 3 |
| Leakage pass | 0 | 4 |
| Reproducible | 0 | 0 |
| Held-out holes pass | 0 | 3 |
| Unscorable holes | 4 | 0 |
| Attempts used (total) | 0 | 0 |
| Model tokens (total) | 0 | 9075234 |
| Wall time s (total) | 0.0 | 2302.5 |

`scripts/evaluate.py --recompute --out
runs/evaluation/20261101T180552Z-benchmark-v2` exits 0: the stored
summary matches the per-hole artifacts on every headline key.

## Per-hole results

| Hole | Arm | Verdict | Constraints | Fidelity | Leakage | Rows | Tokens | Wall s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20LCDD003 | pipeline | unscorable (exit 1, no validation report) | n/a | n/a | n/a | n/a | 0 | 6.2 |
| 20LCDD003 | baseline | pass | pass | pass | pass | 1002/1000 | 1104204 | 269.0 |
| DDG007 | pipeline | unscorable (exit 1, no validation report) | n/a | n/a | n/a | n/a | 0 | 6.9 |
| DDG007 | baseline | pass | pass | pass | pass | 998/1000 | 927916 | 336.3 |
| HD012 | pipeline | unscorable (exit 1, no validation report) | n/a | n/a | n/a | n/a | 0 | 6.0 |
| HD012 | baseline | fail (`repeated_depths_masked_out`) | pass | fail | pass | 1000/1000 | 4331644 | 1124.9 |
| NCB0003 | pipeline | unscorable (exit 1, no validation report) | n/a | n/a | n/a | n/a | 0 | 7.4 |
| NCB0003 | baseline | pass | pass | pass | pass | 1000/1000 | 2711470 | 572.3 |

Rows: baseline = parsed/requested (requested vs parsed recorded
honestly; the two off-by-two parses are model output variance, not
truncation). Baseline reproducibility is 0/4 by construction (no seed
exists). Token counts are adapter-reported per-call usage summed from
the saved invocation records.

## Why the pipeline arm is unscorable (product defect, not harness)

All four pipeline invocations failed identically at the `[plan]`
step before any generation, each leaving a fresh durable error run
record and scoring nothing. The product planner sends its
`PLAN_SCHEMA` verbatim as the model output schema
(`profiles/hylogger/capabilities.py:443`,
`profiles/hylogger/generation/repair.py:177`, same shape in
`profiles/tabular/generation/plan.py:26`). RP-07 added an optional
`strategy` key to that schema
(`profiles/hylogger/generation/plan.py:28-37`: `required` is
`["seed", "n_rows"]` while `properties` also has `strategy`), and the
model API rejects non-strict schemas with:

> API error 400: 'required' is required to be supplied and to be an
> array including every key in properties. Missing 'strategy'.
> (invalid_request_error)

Fake-model suites never hit API schema validation, so RP-07 stayed
green. The harness behaved as repaired: each failure recorded
`unscorable` with exit code, stderr, invocation id, and the fresh run
dir, and no stale or Step 14 bytes were scored. Fixing the product
schema belongs to an RP-07-rework slice, not to this harness branch;
no product file was changed here.

## What is claimed, and what is not

- Claimed: the repaired harness scores only fresh-invocation
  evidence, fails empty coverage, saves every baseline invocation's
  raw bytes with identity, and its summary recomputes from disk
  (fake-model regression tests plus this live run: 4 baselines
  scored, 4 pipeline failures honestly unscorable, recompute exit 0).
- Not claimed: any pipeline-vs-baseline comparison from this round —
  with the pipeline arm at 0 scored holes there is no comparison to
  report, and none is made. Baseline numbers above are single-run
  samples (the model is stochastic); the HD012 fidelity failure mode
  (`repeated_depths_masked_out`) matches the Step 14 baseline failure
  mode on the same hole.
