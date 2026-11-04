# Combined scalar workflow validation

Updated: 2026-11-05

## Scope and revision

The bounded HyLogger scalar teaching workflow has passed combined engineering
acceptance. Reviewed code revision: `6af6ee2a436275df2771bc8c09b6a937504e1cf6`.
Protected PR28 rebased it onto main as
`7011efaba377654c3614350936a42c064a1b9b4c`; the Git trees are identical.
PR27's helper merge was independently reviewed separately. No original source,
threshold, catalogue or golden was changed for these controls.

[Operator guide](../research-workflow-user-guide.md) covers the implemented
route and [acceptance instructions](../full-workflow-acceptance.md) explain
how to rerun the installed-wheel checks. Review summaries and required CI are
public in [PR27](https://github.com/david3xu/ruloom/pull/27) and
[PR28](https://github.com/david3xu/ruloom/pull/28). Raw worker transcripts,
private inputs and local credential configuration are excluded.

## Observed checks

| Evidence | Root result | Independent native Sonnet result |
|---|---|---|
| Source-free full suite | 1079 passed, 15 skipped | 1079 passed, 15 skipped |
| Ruff lint and format | Clean, 185 files | Clean, 185 files |
| Force-pass gate | 43 enforced checks passed | 43 enforced checks passed |
| Pinned strict installed-wheel acceptance | 62 passed, zero failed/pending | 62 passed, zero failed/pending |
| Real execution, report recut, publication and CLI negative controls | Passed | Passed on invented sources |
| Existing HD012 fixed-plan accept/replay/reject and source rederivation | Passed with zero model calls | Original source outside review scope |

The 15 skipped tests require source data or explicit opt-in. Four governance
stubs remain planned and unexecuted, so they are excluded from the 43 enforced
passes. The initial root full-suite attempt could not bind ephemeral local
HTTP test ports inside the macOS tool sandbox. The unchanged elevated rerun
passed; no product check was weakened.

The strict run pinned both the code commit and the clean working-tree digest
`4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`.
The root report's SHA-256 is `799a712c25bea65f1d8f80ef0bc7270130195346bd0a86674258fc48d298a60d`.
Raw local evidence stays in ignored run storage and Datacore project `ruloom`.
A final release audit must pin the actual main revision again after any
subsequent merge; this document's observations apply to the revision above.

## Controls and boundaries

The installed-wheel fixture is invented and its AI-role responses are scripted
mocks. These tests establish no live-model, Azure or AGY output quality. Its
seeds are fixed and disclosed:
9 is a release-positive control first observed in earlier exploration, 8 is a
quality-failure control, and 1 exercises same-seed repair. No seed search or
threshold tuning occurs. A repair correcting inclusion while still failing
another check demonstrates correction, not release success. The root manager's real-source control reuses the existing HD012 seed42,
10000-row balanced plan unchanged. Earlier failed controls remain recorded in
[request-capability evidence](request-capabilities-2026-11-04.md); they have not
been relabelled or erased.

Native Sonnet's exact-revision verdict is PASS. It independently confirmed
case-report recut refusal through ZIP-only CLI, default consumer and real
publication paths, source suitability before request-model creation, exact
row feasibility before drafts, final OSError cleanup, strict version syntax,
explicit profile handling, same seed/target, mandatory checked AI handoffs,
recorded offline unavailability and decision-bound evidence.

## Remaining limits

- ZIP-only checks cannot independently prove origin, `spec_digest` or the plan
  strategy. Preserve a trusted adjacent manifest or external whole-ZIP digest,
  and use `--require-case` when expecting a case.
- Legacy non-case report rows retain their earlier narrower verification.
  Case-selected report results are recomputed and checked.
- Deterministic exercise verification does not approve prose purpose, science,
  privacy, licensing or AI endorsement. A human reviews purpose and evidence.
  Rights/context may be self-attested and keyword screens are incomplete.
- `prepare` leaves a threshold-override refusal to run intake. The case run
  refuses overrides before model calls. Version grammar also rejects leading
  zeroes. Hosted defaults require explicit authorized role bindings.
- Already balanced repair may repeat bytes within the caller's attempt bound.
- This evidence covers the scalar workflow. It does not establish a complete
  multimodal case, deployment operations, the frozen Gemini-versus-Claude
  Test 1 comparison, Test 2, or a human scientific acceptance.
