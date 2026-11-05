# Development timeline

Sources: the Datacore task records for project `ruloom` (1–4 November 2026; see
[coverage](coverage-and-redaction.md)) and `git log` on `main`, which has 105 commits from
1 to 5 November. Work on 5 November is not in the exported Datacore task records; it is
described from the run and pull-request evidence named below.

## 1 November — engine, HyLogger profile and governance (47 tasks, 30 commits)

- The Muse Spark 1.3 contributor worker built the first loops (intake, statistics,
  calibration, generator, validation, bounded repair). The Muse Spark 1.3 contributor
  reviewer checked each one. One example: loop 3's generator review reproduced
  byte-identical output twice before approving.
- Governance tasks RP-01 to RP-09 added force-pass rules, the GenerationSpec, the
  evaluation harness and durable dispatch. Three dispatches failed and were re-dispatched
  (RP-02, RP-07, RP-08). RP-09 (durable dispatch, leases, separate publication) was
  blocked and escalated.

## 2 November — trust repairs, hardening and live critics (53 tasks, 25 commits)

- The Muse Spark worker and reviewer pair ran repair series CP-01 to CP-06, DP-01 to DP-03,
  GP-01 to GP-03, HP-01 to HP-05 and IP-01 to IP-04. These covered publication trust,
  depth/TIR fidelity, download retry, the aggregate gate, restricted model invocation,
  request-to-run binding, idempotent retry, model identity integrity, wheel builds,
  permission policy, attempt bounds and repair authorisation.
- A read-only CI audit task was blocked and escalated, then completed in a second task.
- The IP-04 spawn-gate review returned **rework requested**. The first rework task was
  blocked and escalated; a retry closed the evasion holes.
- Live critic receipts: the local Muse Glimmer 30B text critic and a Muse Spark 1.3 vision
  critic on a synthetic PNG ([receipt](../../quality/critic-receipts-gp01.md)).

## 3 November — recovery hardening, request drafts and model families (17 tasks, 17 commits)

- A re-review approved the IP-04 evasion fixes. The worker/reviewer pair completed JP-01,
  JP-02, KP-01, KP-02 and LP-01 to LP-03: new-arrival refusal, checksum finalisation and
  recovery, quarantine negatives, single csv_schema, plan replay and README counts.
- Codex front door: offline and local-only policies, bounded request drafts, readable
  provenance, and an explicit AGY adapter tested on a frozen synthetic case with Claude Opus
  5.5 high (completed after an escalation).

## 4 November — research-led scalar workflow (5 tasks, 21 commits)

- Codex front door: work on request-time capability accuracy, which was blocked and
  escalated. A native Claude Sonnet review of a frozen revision is still `in_progress` in the
  records.
- AGY Gemini 3.8 Flash high: the bounded research-led scalar workflow and the parallel
  completion task. Both are still `in_progress` in the export.

## 5 November — vision route, Azure transport, diagnostics and the final test (12 commits)

This day is not in the exported task records. Sources are the pull requests and run
evidence.

- PRs #30–#32 (Codex manager, AGY Gemini authors, native Sonnet reviews): vision
  engine/profile, the managed Azure Glimmer route, and strict Azure wire-schema fields. A
  bounded mini smoke failed twice, at Azure schema admission and then at local prose
  screening and Glimmer response validation.
- [PR #33](https://github.com/david3xu/ruloom/pull/33) (Claude Code, Opus 5.5): safe model
  failure reason codes and a published prose-screen contract. An independent Claude
  subagent review failed round 1 (a digest-adjacent screen weakening) and passed round 2.
- Final user test, Comparison 1 at `63b35c0`: Ruloom refused the full request. The narrowed
  run failed at case research and at Glimmer validation. The new reason codes identified the
  Glimmer cause: empty `annotations`/`audio` fields and a `reasoning` field.
- [PR #35](https://github.com/david3xu/ruloom/pull/35) (Claude Code): fixed both failures,
  with an independent review PASS.
- Comparison 2 at `3216c5c`: every live role succeeded, and both releases verified. See the
  [final test record](../../final-test-record.md).
