# Model, worker and review evidence

## Worker identities as recorded in Datacore (1–4 November 2026)

| Recorded identity | Tasks assigned | Event-source count |
|---|---|---|
| `muse-worker@muse-spark-1.3-contributor` | 62 | 179 |
| `muse-review@muse-spark-1.3-contributor` | 53 | 173 |
| `codex-front-door` | 3 | 17 |
| `agy@gemini-3.8-flash-high` | 3 | 0 |
| `claude-review@sonnet` | 1 | 0 |

Coordination events also came from `coordinator-main` (144), `host-worker-bridge` (135) and
`muse-front-door` (126). Of the 52 recorded review verdicts, 51 are `approved` and 1 is
`rework_requested` (IP-04, 2 November); the other 70 tasks carry no review verdict.

These identities are what Datacore logged for each task. They show which worker seat was
assigned and reported completion. They are not a per-call audit of model usage: the export
holds no token counts for this work.

## Verified live model calls

These calls are confirmed from product run logs, which record the returned model identity
and the provider-reported usage.

| Date | Run | Role | Model returned | Outcome |
|---|---|---|---|---|
| 2 Nov | PDD446 local critic | local_critic | Muse Glimmer 30B (local GGUF) | advisory note recorded ([receipt](../../quality/critic-receipts-gp01.md)) |
| 2 Nov | synthetic PNG | vision critic | `muse-spark-1.3` (hosted Muse CLI) | note recorded (same receipt) |
| 5 Nov | final test C1, full request | planner | `muse-spark-1.3` (Azure) | refused, with unsupported list; 5,134 tokens |
| 5 Nov | C1 narrowed | planner, source_assessor, case_researcher | `muse-spark-1.3` (Azure) | research answer listed source-derived items as unsupported, so the run failed |
| 5 Nov | C1 narrowed image | critic | `meta-models--muse-glimmer-30b` (Azure managed) | response refused by adapter: `unsupported_message_fields` (annotations, audio, reasoning) |
| 5 Nov | final test C2, full request | planner | `muse-spark-1.3` (Azure) | refused; 5,048 tokens |
| 5 Nov | C2 narrowed | planner, source_assessor, case_researcher, case_reviewer | `muse-spark-1.3` (Azure) | all ok; scalar verdict pass |
| 5 Nov | C2 narrowed image | critic | `meta-models--muse-glimmer-30b` (Azure managed) | status ok, provider-reported usage 1,122 in / 2,004 out |

The earlier 5 November mini smoke (7 Azure calls, all failed or stopped) is retained in local
evidence and summarised in the [timeline](timeline.md).

## Independent reviews of the 5 November changes

| Change | Reviewer | Result |
|---|---|---|
| PR #30–#32 | native Claude Sonnet 5.5 (separate history, read-only), recorded by the Codex manager | PASS for each reviewed head |
| PR #33 | fresh Claude Code subagent, read-only, separate context | round 1 FAIL (digest-adjacent screen weakening), round 2 PASS |
| PR #35 | fresh Claude Code subagent, read-only, separate context | PASS, with 4 non-blocking follow-ups |

No author approved their own change. Merges used the protected rebase flow with an
exact-head guard and green CI.
