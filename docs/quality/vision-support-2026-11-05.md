# Vision engineering evidence — 5 November 2026

Scope: generic structured image input and HyLogger's optional synthetic-image
advisory extension, based on accepted scalar main `cd451e6`. This is engineering
verification, not admission of a new scientific case or the paired model benchmark.

## Reviewed implementation

Code revision: `9a7b8bf3fcf174dcfc496c09e3422f6cba801fdf`.

- Full source-free regression: **1,215 passed, 15 skipped**.
- **43 enforced gates passed**; four planned governance stubs remain unexecuted.
- Native Claude Code **Sonnet 5.5 (`claude-sonnet-5-5`)**, requested effort high,
  independently reviewed the complete candidate and its follow-up delta: **PASS**.
  The reviewer used read-only tools and no delegation or edits.
- The preceding clean candidate `8b8ef10c11c9ae14fc674831ec05aface1dba28d`
  passed pinned strict installed-wheel scalar acceptance **62/62**. The final main
  acceptance receipt is retained separately after merge; this document does not
  pretend a later commit was tested by that earlier run.

AGY Gemini 3.8 Flash high produced initial implementation drafts and provider
rework. AGY Opus 5.5 high produced partial profile work before quota exhaustion;
an Argon rework attempt stopped before editing. Manager Codex completed fixes,
fixtures, documentation and the final audit. Independent Sonnet review used a
separate history. Assignment and contribution records are retained in Datacore.

## Two disclosed live-transport attempts

Both phases used the existing execution graph and an isolated installed wheel,
explicit `meta_cli` / Meta provider, **Muse Spark 1.3**, low effort, contributor off,
one model step and a 120-second adapter timeout. Inputs were locally constructed
fixtures: eight scalar records and a 64×32 banded image. The fixed generator seed
was **8**. Real source imagery was unavailable. No source rows, original image or
source identifiers entered the prompt or attachment.

Both phases generated the identical registered synthetic PNG:
`10163f5d457df6ddfd3bd40bcafa9841a6228d8b9b1e724d06ab006eb61731b2`
(1,929 bytes, 64×32). Each phase recorded exactly one `critic` invocation and no
other model role. No provider, seed, threshold or image was selected after seeing
an opinion.

| Phase | Result | Observed model/usage | Time |
|---|---|---|---|
| Initial smoke at `8b8ef10` | **Failed**, HTTP 400: provider rejected `uniqueItems` in the structured-output schema. No model answer or known usage was claimed. | Unknown; cost unavailable | 6.94 s |
| Separately bounded repair verification at `9a7b8bf` | **Passed**, genuine image attachment and strict structured response; all five deterministic image checks passed. | `muse-spark-1.3`; input 19,855, output 1,049, reasoning 495, cached 0, calls 1 | 15.70 s |

The repair projects unsupported `uniqueItems` out of the provider wire schema only.
The original local JSON schema and critic still reject duplicates. Controlled
Meta and Azure transport tests prove that omission cannot turn a duplicate answer
into an accepted advisory. The actual sent wire schema is retained with the smoke.
Provider schema limits are described in the
[Meta structured-output guide](https://dev.meta.ai/docs/structured-output).

The successful opinion described flat continuous bands, intentional colour steps,
no visible within-strip breaks or clipping, and limited detail at this resolution.
It explicitly deferred teaching suitability to a human and could not assess loss
of original framing. These observations demonstrate the bounded advisory workflow,
not validated defect-detection performance or geological conclusions.

**Monetary cost was not reported.** The retained CLI export contains token usage
but no monetary cost fields. Receipts mark cost unavailable, not zero; they do not
infer an invoice from token counts.

Both live phases ended with explicit **automated engineering rejection** at the
operator gate. No owner approval, human scientific acceptance or accepted live
release is claimed. The first failure, its redacted local session export, the
repair plan, receipts and output artifacts are preserved separately.

## Installed recipient checks and preserved scope

The repaired wheel SHA-256 was
`9bab588e2d03d619f1467520aca50979b60e63198004e227215486257499e988`.
It was installed in an isolated environment outside the checkout. Dependencies
were reused without processing editable-repository `.pth` files; this was not a
clean network installation.

A separate installed-wheel image packaging/recipient fixture used explicitly
**injected** responses and an automated engineering acceptance note. The installed
CLI verified its valid image bundle and refused a substituted advisory digest even
when internal checksums were re-cut. This fixture made **zero genuine model calls**
and does not stand in for live model evidence or human approval.

Scalar source data, thresholds, goldens, case catalogue and benchmark were
preserved. Azure Spark remains text-only. Azure Glimmer has image protocol support
but no deployment/SKU and no live validation. Muse was selected explicitly; no
provider switched automatically.

Local evidence index: `runs/vision-manager-20261105/README.md`. It includes failed
and inconclusive engineering attempts, source preservation, reviews, CI and the
final pinned acceptance receipt. These private run files are not package assets.
The paired Muse-versus-Claude benchmark remains pending the owner's instruction.
No mineral identity, grade, geological validity, privacy, anonymity or licensing
rights are established by this bounded test.
