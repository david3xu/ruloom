# Credit-free product workflow

Owner-authorised scope, 3 November 2026. Implementation by Codex; real
Meta/Codex/Claude task comparisons are deferred to the owner. Fixture tests
do not establish model quality, scientific validity or production isolation.

## Design and acceptance

1. `run --model-policy offline --plan PATH` makes no model calls, including
   repair and optional critics. Deterministic checks still run. A failed
   check remains a failure; a required model step cannot silently pass.
2. `local-only` permits the local text adapter at a literal loopback address.
   An explicit role adapter selects local planning. No hosted fallback, proxy
   or HTTP redirect is allowed by the local-only transport. This constrains
   Ruloom's adapters, not the server's internals or arbitrary third-party code.
3. `prepare` asks a model to interpret a short request into supported settings.
   It sees the request and the profile's capability menu, not source rows.
   Source bytes are hashed locally and the draft pins that digest.
   It creates a draft for human review, never a dataset or an acceptance.
   Unsupported requirements refuse preparation; supported interpretations still
   need human review. An exact row target is enforced on every stored plan.
4. The evidence report reads persisted records to show the request, plan origin,
   actual model invocations, check failures and optional-review gaps. It must
   not label a replay as fresh inference or a planned row count as measured.
5. Preserve the existing generator, source statistics, checks, human gate,
   release hashing, publication and rights boundaries. No source-data edits.

Acceptance evidence: positive and negative fixture tests across the CLI,
engine policy, request validation, local transport and report; full suite;
aggregate gate; a commit-linked Datacore record. All checks are automated
fixtures. Live local planning and repair remain unverified until the owner
requests the real evaluation.

## Attribution

Earlier Muse contribution is documented in Git history and the dated
[model receipts](quality/critic-receipts-gp01.md). Those receipts describe
past runs, not new activity in this change. Codex implements this extension.
At runtime, model responses propose settings or advisory comments; seeded
code generates rows, deterministic checks determine eligibility, and the
human decides acceptance. Development authorship and runtime model identity
are different records.

Datacore project: `ruloom`. Ledger: `ruloom-credit-free-product-20261103`.
Decision: `01a1a19c-336c-70cb-81e1-dabfecc49254`.
Task: `RULOOM-CREDIT-FREE-01`. No worker handoff was used.

## Later comparison

The owner will choose the real tasks, providers, budgets and acceptance
criteria. Freeze the same source hash, request, thresholds and software
revision; run each provider independently in a fresh output directory; retain
requested and server-reported identities, failures, tokens, elapsed time and
human decisions. Compare delivered data and check coverage as well as speed.
Do not treat saved-plan replay or fake-model fixtures as model performance.
No such experiment has been run for this extension.

Automated preparation evidence: [verification receipt](quality/credit-free-preparation.md).
