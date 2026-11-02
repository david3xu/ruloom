# RP-09: durable dispatch with leases/fencing, separate publication

Dispatch (who runs what, and who owns it) and publication (how an
accepted release reaches recipients) are now durable, local-only, and
separate steps. No platform code is imported: both stores are plain
JSON records under the same output root the runs live under. There is
no hosted service here: dispatch and publication are local library
calls the CLI makes on its own machine, not a server that accepts or
publishes on anyone's behalf.

## Dispatch (`engine/core/dispatch.py`)

One JSON record per submitted item under `<base>/dispatch/`:

- `submit` is idempotent: the same `idempotency_key` returns the first
  record instead of duplicating work, but only when the key, the
  canonical request identity, and the enforced-fields binding digest
  all match the binding persisted at first submission. A key first
  seen under another authority is refused, so keys cannot be squatted
  across authorities, and the same key with a different request, a
  changed row/attempt bound, or a changed spec overlay is refused
  instead of returning another request's item. An unkeyed submit whose canonical identity matches another
  owner's item is refused as well, so a keyless resubmission can never
  return or shadow a foreign job. An explicit `item_id` is create-only
  and collision-safe: the first submit creates the item, and a later
  submit naming the same id returns the existing record unchanged only
  under the same authority and request. A changed request or a foreign
  authority is refused, so no resubmission rewrites another owner's
  history or resets its fencing epoch.
- Every entry point taking an item id validates a narrow canonical
  shape (letters, digits, dash, underscore; at most 64 characters)
  before any I/O, and record paths are verified to stay inside the
  store's own items directory. Separators, absolute paths, dot
  components, and overlong ids are refused.
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
- Retries are bounded: each reported failure and each expired lease
  (via the expired-claim reclaim or the `recover` sweep) consumes one
  retry from `max_retries`, while the launch count persists separately
  in `attempts`. Until the budget is spent the item returns to the
  queue; then it rests terminal as `exhausted` — expiry exhaustion
  records `retries_exhausted_by_expiry` — and is never claimed again,
  so repeated crashes cannot trigger unbounded generation cost. The
  token still moves on for every successful re-dispatch, and stale
  tokens stay fenced off.

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
own idempotency. The worker's success report is evidence, not
authority: it names a run, and publication resolves that run's
persisted acceptance record before any byte is copied:

- It refuses unless the run carries `release.zip` with a matching
  detached `release.sha256` (the hash is verified, not merely
  required); a human `decision.json` verdict of `accept` binding the
  frozen evidence digest; a `frozen-manifest.json` whose digest matches
  that binding; and a `manifest.json` whose members, sizes, hashes,
  and artifact digest match the frozen evidence member for member.
- The bundle must pass consumer verification, which now checks the
  manifest's recorded member hashes and sizes (not only member names)
  and requires the report verdict to read exactly `pass`. A changed
  report with re-cut internal checksums still fails against the stale
  external manifest.
- The shipped validation report must read `pass` and cover every
  persisted required check exactly once; a single selected attempt's
  receipt must record `pass` and bind the accepted artifact.
- The dispatch item must rest `succeeded` under the publishing
  authority, naming this run with reported digests equal to the
  persisted accepted ones (an all-zero artifact claim refuses instead
  of shipping different bytes) and a requested profile equal to the
  run's own.
- Request-to-run binding (DP-01, audit C01): the dispatch request's
  bound identity (profile, pinned source-content digest, pinned
  normalised spec digest, persisted at submit; unknown fields,
  malformed pins/bounds, and conflicting row/attempt bounds refuse at
  submit) must equal the identity the run's acceptance record binds
  and the identity the run's own record delivers. Profile-name
  equality alone never suffices: a request for source B naming source
  A's run refuses, as does a run whose bytes changed after submit or
  whose executed row/attempt bound exceeds the declared bound.
- Single-snapshot publication (DP-01, audit C02): the run ZIP is read
  once into an immutable snapshot staged through a private temp file;
  consumer verification, acceptance resolution, and the copy all run
  against that snapshot, and the returned identity derives from it
  alone. A stability guard re-checks the run file after acceptance:
  bytes swapped in after the gate refuse (corrupt or well-formed),
  the item is never marked complete, and the published digest never
  differs from the accepted one. The snapshot, destination, and
  persisted accepted digests must all agree before the complete mark.
- The record moves `in_progress` before any copy and `complete` only
  after the destination bytes and sidecar verify. A crash in between
  resumes to the same bytes on re-publication (no destination: copy
  again; matching destination: adopt it). Re-publishing a complete
  record whose destination still matches is a no-op.
- Once the record is complete the destination must keep attesting the
  recorded bytes (HP-02, audit A02): a missing, corrupt, or foreign
  destination refuses re-publication instead of being silently
  re-copied. Recovery goes through the authorised `repair` operation,
  which re-runs every acceptance guard, restores exactly the recorded
  bytes (never a new release), preserves any replaced bytes beside
  the destination under a digest-derived name, and appends the
  authorising decision (who, when, why, what was found) to the
  record.
- Publication never overwrites destination bytes it cannot attribute
  to its own record: unattributed foreign bytes refuse loudly, and one
  item ships exactly one release. Publication item ids take the same
  narrow canonical shape as dispatch ids, and destinations are
  verified to stay inside the published area.
- `download` verifies the new bytes in a unique temporary sibling
  inside the destination directory and commits atomically via rename,
  returning the attested identity (zip hash, artifact hash, member
  list). Replace policy: a verified download overwrites the
  destination; an unverified one never touches it, so a good file
  already at the destination survives byte-identical. A same
  source/destination path verifies in place without deletion. No
  half-fetched destination is ever left behind.

Consumer hardening: corruption inside the deflated bytes used to
escape `verify_release` as a raw decoding crash. Member reads and
extraction now translate undecodable bytes into
`ReleaseVerificationError` refusals (regression test:
`test_bit_flip_inside_compressed_data_is_a_refusal`). Trust repair
(CP-01) added the manifest hash/size binding, the `pass`-only report
verdict, and the persisted-acceptance resolution above.

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

- `tests/engine/test_dispatch.py` (47 tests): idempotent submit,
  key-across-authorities denial, single-winner claim under 8 threads,
  live-conflict vs expired-redispatch (with retry budget to spend),
  terminal claim refusal, heartbeat extension and stale/wrong-worker/
  expired refusal, cancel fencing off an in-flight worker, cancel
  authority and state shapes, recovery sweep (with retry budget to
  spend), retry exhaustion (`max_retries=1`: queued then exhausted)
  and zero-retry exhaustion, success binding, record-copy isolation
  and listing, trust repair (CP-01): explicit-id resubmission returning
  the immutable record, changed-request and foreign-authority refusal
  with history and fencing kept, keyed collision refusal, and
  traversal/shape id refusal at every entry point with nothing written
  outside the store, plus atomic-retry repair (CP-03 A06):
  expired-claim reclaim and recovery sweep each consume one retry with
  `attempts` persisted separately, `retries_exhausted_by_expiry`
  exhaustion, four-cycle bound, and fencing preserved, plus
  request-binding repair (DP-01 C01): unknown request fields refused,
  malformed pins/bounds and conflicting row/attempt bounds refused,
  canonical request identity persisted (pinned and unpinned shapes), plus keyed
  retry binding (DP-02 C03): same key with identical request returns
  the stored record unchanged, same key with a different request
  refuses, key equivalence compares the enforced request binding
  (unenforced source-label renames still collapse), and an unkeyed
  duplicate of a foreign owner's item refuses, plus row-limit binding
  on retry (HP-01 A01): same key with a changed `max_rows`, a changed
  attempt bound, or a changed/added spec overlay refuses with history
  and fencing kept.
- `tests/engine/test_publication.py` (31 tests): publish/download
  round trip with hash equality to the reviewed `release.sha256`,
  idempotent re-publish, interrupted-copy resume to identical bytes,
  interrupted-mark adoption, foreign-authority denial, unsucceeded /
  wrong-run / changed-bytes refusal, missing and bit-flipped release
  refusal, foreign-destination refusal, one-item-one-release, download
  cleanup of unverifiable bundles, the full chain as one test, trust
  repair (CP-01): altered-report-with-stale-manifest refusal at both
  consumer and publication, reported-digest binding refusal (the
  all-zero artifact claim), detached-hash requirement and mismatch
  refusal, accepted-decision binding refusal, request-profile binding
  refusal, required-coverage recording and unsatisfied-coverage
  refusal, and traversal id refusal, plus atomic-download repair
  (CP-03 A05): invalid download preserves an existing good destination
  byte-identical, interrupted commit preserves the destination with no
  stale tmp, and same source/destination verifies in place without
  deletion, plus request-binding and snapshot repair (DP-01 C01+C02):
  wrong-source, tighter-row-bound, and changed-source requests refused
  with nothing shipped, and post-acceptance ZIP swaps (corrupt and
  well-formed variants) refused with the item never marked complete,
  plus safe recovery on republish (HP-02 A02): re-publication refuses
  a missing or unexpected destination once complete with bytes and
  record untouched, and authorised repair restores the recorded bytes
  (preserving replaced bytes beside the destination and appending the
  decision) while refusing without a reason or under a foreign
  authority.
- `tests/engine/test_consumer.py` (14 tests): the producer-side
  guarantees above, plus trust repair (CP-01): stale-manifest hash
  refusal, recorded-size mismatch refusal, and non-`pass` verdict
  refusal.

Gates at commit time: workspace pytest 500 passed; `force_pass.py`
12/12 PASS; clean-archive copy re-verified before commit. The PDD446
golden bytes are untouched (no profile or engine run-path file
changed behaviour: the consumer edit only widens refusal shapes).

## Feature records

- `F-ENGINE-DURABLE-DISPATCH` (rule `Q-ENGINE-DISPATCH`) and
  `F-ENGINE-SEPARATE-PUBLICATION` (rule `Q-RELEASE-PUBLICATION`)
  under `catalogue/features/engine/`.
