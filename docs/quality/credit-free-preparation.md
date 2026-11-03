# Credit-free preparation receipt

Observed 3 November 2026, branch `codex/credit-free-product`, based on
`22da6583637eb9333601eb501b1257dc9b48b6ee`. Implementation and self-review
by Codex (`codex-front-door`), personally. No independent reviewer or live
provider comparison is claimed.

## Verification performed

- `python -m pytest -q`: 730 passed, 14 source-dependent tests skipped.
- `python scripts/force_pass.py`: exit 0; 46 outcomes pass, comprising
  42 executed rules and four explicitly planned stubs. Planned tool
  confinement remains unimplemented; this change does not close it.
- `ruff check .` and `ruff format --check .`: pass.
- `git diff --check`: pass.

These commands ran in a source-free clone with the existing Python 3.12
environment. HTTP fixtures were literal-loopback stub servers. No real
model was started, no hosted credits were consumed, and no real-data
quality evaluation was performed. Test accept/reject inputs are automated
fixtures, not human scientific approval.

## Important counterexamples

The mandatory call-policy checker executes factory tripwires proving no
hosted client is created under offline or local-only denial. Replay failures
retain failed checks, stop before model repair and produce no release.
Optional critics remain visibly unavailable; required critics still fail.
The local transport checker refuses redirect and proxy escape paths.

The request checker validates all response fields, refuses unsupported
settings, prevents directory overwrite, binds source bytes to the draft,
runs a prepared fixture offline, and refuses a source changed afterwards.
An exact row target cannot silently become another row count. The report
checker distinguishes a saved plan from successful runtime inference and
shows unavailable advisory review. Semantics still need human review: a
schema cannot prove that a model understood an arbitrary natural-language
request correctly.

## Attribution and handoff

Earlier runtime Meta evidence remains in the dated
[GP-01 receipts](critic-receipts-gp01.md). This extension makes no new
performance claim about those models. Its code and automated tests were
written by Codex; deterministic generation and human acceptance keep their
existing roles. Git author labels alone are not evidence of a model call.

Datacore project `ruloom`, ledger `ruloom-credit-free-product-20261103`, task
`RULOOM-CREDIT-FREE-01` links the owner-authorised decision
`01a1a19c-336c-70cb-81e1-dabfecc49254` and implementation record
`01a1a1bd-42cb-7707-9bbf-e51c006ddf7a`. The final commit and verification
are recorded in Datacore after committing. Records stay separate from raw
source data. Nothing has been uploaded as a competition submission.

Next: the owner defines and authorises the real independent tasks and model
budgets. See [the product workflow](../credit-free-product.md) for that
comparison boundary. Local planning, request interpretation quality and
repair effectiveness remain unverified until then.
