# RP-09: durable dispatch with leases/fencing, separate publication

Dispatch (who runs what, and who owns it) and publication (how an
accepted release reaches recipients) are now durable, local-only, and
separate steps. No platform code is imported: both stores are plain
JSON records under the same output root the runs live under.

## Dispatch (`engine/core/dispatch.py`)

One JSON record per submitted item under `<base>/dispatch/`:

- `submit` is idempotent: the same `idempotency_key` returns the first
  record instead of duplicating work. A key first seen under another
  authority is refused, so keys cannot be squatted across authorities.
- `claim` leases a queued item to one worker with a monotonic fencing
  token and an expiry. A live lease blocks every other claimant
  (`ClaimConflict`); an expired lease is re-dispatchable with the
  token moved on — the launcher-outage path: a dead launcher's items
  return to the queue via `claim` or the `recover` sweep, and the
  history records the recovery.
- Heartbeats, success reports, and failure reports must present the
  current token. Anything stale — a superseded worker, a cancelled
  item, an expired lease — is refused (`FencingError`), so a worker
  that lost its lease can never complete, fail, or extend work it no
  longer owns. Cancellation clears the lease and moves the token on,
  fencing off a worker still running after cancel.
- Cancellation and publication require the submitting authority; any
  other authority is refused (`AuthorityDenied`).
- Failures are bounded: each reported failure returns the item to the
  queue until `max_retries` is spent, then the item rests terminal as
  `exhausted` and is never claimed again.

Concurrency contract: within one launcher process every
read-modify-write runs under a per-store lock, so racing threads
produce exactly one claim winner (pinned by test). Every record write
is atomic (temporary file plus fsync plus rename), so a crash
mid-write leaves the previous record intact. Two live launchers
writing at once are out of scope — the recovery story covers a dead
launcher replaced by a fresh one.

## Publication (`engine/artifacts/publication.py`)

Packing still writes `release.zip` into the run directory at accept
time. Publication copies the accepted bundle into a separate
published area with its own record (`<base>/publications/`) and its
own idempotency:

- It refuses unless the run carries `release.zip`, `manifest.json`,
  and `release.sha256`; the bundle passes consumer verification; the
  dispatch item rests `succeeded` naming this run with matching
  digests; and the publishing authority owns the item.
- The record moves `in_progress` before any copy and `complete` only
  after the destination bytes and sidecar verify. A crash in between
  resumes to the same bytes on re-publication (no destination: copy
  again; matching destination: adopt it). Re-publishing a complete
  record whose destination still matches is a no-op.
- Publication never overwrites destination bytes it cannot attribute
  to its own record: unattributed foreign bytes refuse loudly, and one
  item ships exactly one release.
- `download` copies a published bundle to a chosen path and verifies
  it there, returning the attested identity (zip hash, artifact hash,
  member list). A bundle that fails verification is removed, never
  left half-fetched.

Consumer hardening shipped with this slice: corruption inside the
deflated bytes used to escape `verify_release` as a raw decoding
crash. Member reads and extraction now translate undecodable bytes
into `ReleaseVerificationError` refusals (regression test:
`test_bit_flip_inside_compressed_data_is_a_refusal`).

## Executed evidence (2026-11-01, replay planner double, no live model)

End-to-end chain submit → claim → tabular run (accept) → success
report → publish → download, executed under a fresh `/tmp` root:

- `item_id=facfc2d5743547c0877fd7e717970077`
- `run_id=34679d512c8b4a1a9b9ae63eddeb3df7`
- `artifact_sha256=fda9cf255638a5f3fb274b4d61971ff9a37702fc3f5042a145fb2fc3a4a318e4`
- reviewed `release.sha256` =
  `f7040fc94e29e702544e2c18c302750bea5030562ef299bd4841dbe208ffc1bf`
- published zip hash = downloaded zip hash = reviewed hash (equal,
  asserted in code); downloaded artifact hash = run artifact hash.
- Members: `data.csv`, `evidence-report.md`, `plan.json`,
  `provenance.md`, `run.jsonl`, `seed.txt`, `validation-report.json`.

Control cases (committed tests, no sleeps — clocks are injected):

- `tests/engine/test_dispatch.py` (15 tests): idempotent submit,
  key-across-authorities denial, single-winner claim under 8 threads,
  live-conflict vs expired-redispatch, terminal claim refusal,
  heartbeat extension and stale/wrong-worker/expired refusal, cancel
  fencing off an in-flight worker, cancel authority and state shapes,
  recovery sweep, retry exhaustion (`max_retries=1`: queued then
  exhausted) and zero-retry exhaustion, success binding, record-copy
  isolation and listing.
- `tests/engine/test_publication.py` (11 tests): publish/download
  round trip with hash equality to the reviewed `release.sha256`,
  idempotent re-publish, interrupted-copy resume to identical bytes,
  interrupted-mark adoption, foreign-authority denial, unsucceeded /
  wrong-run / changed-bytes refusal, missing and bit-flipped release
  refusal, foreign-destination refusal, one-item-one-release, download
  cleanup of unverifiable bundles, and the full chain as one test.

Gates at commit time: workspace pytest 500 passed; `force_pass.py`
12/12 PASS; clean-archive copy re-verified before commit. The PDD446
golden bytes are untouched (no profile or engine run-path file
changed behaviour: the consumer edit only widens refusal shapes).

## Feature records

- `F-ENGINE-DURABLE-DISPATCH` (rule `Q-ENGINE-DISPATCH`) and
  `F-ENGINE-SEPARATE-PUBLICATION` (rule `Q-RELEASE-PUBLICATION`)
  under `catalogue/features/engine/`.
