# Final user test record — 5 November 2026

The shared prompt and the original plan are in [final-test.md](final-test.md). This page
records what happened. The independent evaluation of the delivered packages is **in
progress**, and its scores will be added here when it finishes. No winner is declared before
then.

## Frozen test material

The test material was frozen at 01:25 UTC, before either side started. The frozen
manifest's SHA-256 is `a16ff3fb6e8756677263291522d50f10ec714989d68804bebbf6d00960a393ba`.

| File | SHA-256 |
|---|---|
| `prompt.txt` | `6aa169433cdf1487c621ae4c1822a5695fa09c60432a414e459288bd7595acb8` |
| `requirements.md` (R1–R21: 1 fictional hole, 200 depth records, 4 samples, 1 picture with 4 regions, 4 curves, formats, units, links, source-fidelity tolerances) | `633d2a7f06bff5148c1502d233f204ff04b84b6ffbde4ddcb15424b198c432de` |
| `evaluation-criteria.md` | `419148a1ea3db66b44d683c57652de203593467ea27c4b973284a60a11df6a1a` |
| `limits.json` | `29958517da543580c2fd43dffee774d5bdfd83a472e891ee0c14bffcfbe33c54` |
| HD012 scalar CSV (NVCL WA, CC BY 4.0) | `8d9afb0120f9a82182f4689109a6c97c96bcc869a555ba77fdece0f98b4cb1bf` |
| HD012 source manifest | `b6c4bb12872a849cbf7cf4db89bed0de39ac0f59757fde05ae4b36bbfb3ffac6` |
| Invented 64×32 picture fixture | `262af3029b71de00cbbbed5af1ba22c24167d16ff0886b455f24bf685d89b5c5` |

The source has no collar coordinates, no core photographs and no reflectance curves. Both
sides were told this.

**Limits set before the start:**
- Ruloom: 45 minutes, at most 8 Azure calls and 60,000 known tokens, no retries.
- Baseline: 45 minutes, a USD 15 budget, no retries.
- Neither side had live web access.

## Baseline: native Claude Code

- **Session:** model `claude-opus-5-5`, run in a clean folder outside the Ruloom workspace.
- **Isolation:** MCP servers and web tools were disabled, and Ruloom paths were denied. The
  audit found 19 tool calls and none touched a Ruloom path. This is permission containment,
  not OS isolation.
- **Result:** one ZIP, `synthetic-core-teaching-dataset.zip` (SHA-256 `a6924c2d…`, 15
  files), produced in 344 s over 20 turns.
- **Cost:** USD 1.23, as reported by the Claude CLI.
- **Self-report:** the session says R1–R21 all pass and that it simulated the GUI steps with
  scripts. The independent evaluation will check that claim.

## Ruloom with Muse models

Both comparisons used the installed wheel outside the source tree, with
`--model-policy azure-only`. Spark ran as `muse-spark-1.3` with effort `low` on the Azure
OpenAI v1 route. The critic ran as `meta-models--muse-glimmer-30b` with effort `none` on the
managed route.

### Comparison 1 — revision `63b35c0` (scored)

1. **Full request** (prompt plus requirements sheet, 3,710 characters). `prepare` refused it,
   making 1 Spark call (5,134 tokens, 10.3 s). It listed as unsupported:
   - hole and sample IDs and the links between them;
   - picture regions, curves and QGIS coordinates;
   - the custom tolerances;
   - any output other than the scalar CSV.

   It also listed the README guide and the ZIP delivery, although Ruloom does produce an
   evidence report and a release ZIP. **No spec and no data were created.**
2. **Narrowed request.** This was a human intervention, written down before it ran, that
   kept only the supported subset: 200 depth records whose spacing, inclusion and blank
   labels follow the source.
   - `prepare` succeeded.
   - The scalar case run **failed at case research**. The researcher listed the
     source-derived requirements as unsupported because "admitted passages contain no depth
     spacing…".
   - The image run generated rows and a picture, and all deterministic checks passed. The
     **Glimmer review was refused by the adapter.** Its reason code was
     `unsupported_message_fields`: the response carried `annotations` and `audio` set to
     null, plus a `reasoning` field. Its finish reason was `stop`, the model matched and
     usage was valid.
   - The run was then stopped at the human gate because of a test-driver defect: the gate
     prompt was buffered and never seen. **There was no release.**
3. **Zero-call packaging** (labelled as a workaround). The same image journey was replayed
   with `--model-policy offline`, and the critic was recorded as unavailable. The release
   verifies in a fresh folder, with exit 0.

**Totals:** 5 Azure calls (4 Spark calls that returned, 1 Glimmer call that was refused),
12,230 known Spark tokens, and Glimmer usage reported but not charged.

### Fixes between the comparisons

[PR #35](https://github.com/david3xu/ruloom/pull/35) made two changes:
- empty `annotations` and `audio` fields are now admitted, and `reasoning` is discarded
  unread;
- the case-research prompt now states the case scope.

No threshold, golden, catalogue, identity, schema, usage or budget check changed. The PR had
an independent review PASS and green CI. The frozen material and the narrowed request text
are unchanged. The only test-driver change is unbuffered child output.

### Comparison 2 — revision `3216c5c` (clearly labelled new comparison)

1. **Full request.** Refused again at `prepare` (1 Spark call, 5,048 tokens, 8.4 s). The
   linked lesson package is still not supported.
2. **Narrowed request.** Ran end to end on live models: 5 calls, 15,015 known tokens, and no
   unknown usage.

| Stage | Model returned | Result |
|---|---|---|
| planner (`prepare`) | `muse-spark-1.3` | plan: 200 rows, seed 12345, `fitted_markov_balanced`; no unsupported items |
| source_assessor | `muse-spark-1.3` | ok |
| case_researcher | `muse-spark-1.3` | ok |
| generation and checks | code | 42 checks: 41 pass, 1 recorded skip (`swir_answer_rate`, by design); verdict pass |
| case_reviewer | `muse-spark-1.3` | ok |
| human gate | automated engineering operator | accept (verdict pass) |
| scalar release | — | `36c8c2e9…`; recipient `verify --require-case` passes with `case verified: true` |
| image route: picture and checks | code | 5 image checks pass |
| critic | `meta-models--muse-glimmer-30b` | **status ok, provider-reported usage** (1,122 in, 2,004 out); four advisory observations, no defects noted |
| image release | — | `8e7d4b20…`; recipient `verify` exit 0 (integrity verified; no case members, so `not case-checked`) |

The scalar and image releases contain byte-identical `data.csv` files, because they use the
same plan and seed.

**Controls (zero model calls):**
- an operator **reject** created no release;
- a tampered `data.csv` was refused by `verify`, because it no longer matched its recorded
  checksum;
- an image folder without a decodable PNG was refused.

One earlier tamper attempt edited nothing, and its log is kept labelled as not a test.

**Human interventions, all recorded:**
- the narrowed request;
- the image spec derivation (the scalar case plus `--images` is refused by design);
- the operator gate decisions.

## Findings to carry forward

- Ruloom cannot yet produce the requested linked package (hole location, sample IDs,
  picture regions, curves, QGIS and Fiji files in one ZIP). It refuses the request instead of
  imitating it.
- The scalar and image routes deliver two separate ZIPs, not one student package.
- Live release ZIPs include the Azure subscription ID and endpoint host in `run.jsonl`. This
  should be redacted before releases are shared.
- The reviewer's non-blocking notes on PR #35 still apply:
  - duplicate-key rejection applies only on the vision path;
  - there is no admit test on the vision path;
  - the research wording could accept requested spacing values. Checks still use the
    contract.
- Cost: Spark per-token prices are not reported by the transport. The managed Glimmer
  deployment bills four A100 accelerator-hours while it runs, regardless of calls.
  Startup-credit coverage is unconfirmed, and no zero-cost claim is made.

## What this test shows, and what it does not

- **Shown:** the bounded scalar teaching case and the separate synthetic-picture route ran on
  live Muse models through every Ruloom stage (Comparison 2). Ruloom refused the unsupported
  parts openly, and its releases verify outside the workspace.
- **Not shown:**
  - that Ruloom meets the full lesson prompt;
  - teaching usefulness, as judged by a person;
  - scientific validity;
  - a winner, until the independent evaluation is recorded here.

Local evidence (not in Git): `runs/fresh-user-e2e-20261105/` on the test machine. It holds
the receipts, logs, both comparisons' packages, the baseline stream and tool audit, and the
evaluator output.
