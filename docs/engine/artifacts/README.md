# Engine artifacts: the reviewed frozen release

A release ships exactly the bytes the human reviewed -- never the
mutable working files as they happen to look when packaging runs.

## Freeze at the gate

When the run reaches the human gate (the artifact step has run and the
verdict passes, before the review prompt is shown), the engine
snapshots every release candidate declared by the profile:

* each frozen member is read once into engine memory, with a byte copy
  under `runs/<run-id>/frozen/`;
* the candidate manifest -- path, size, sha256, and producing attempt
  per member -- is recorded to `frozen-manifest.json`;
* the manifest digest becomes the evidence digest, which the human
  decision binds (`decision.json` carries `frozen_manifest_sha256`).

Mutating or deleting a working file during the review prompt cannot
change the released bytes: packaging never opens working files again.

## Manifest-driven membership

The member list comes from the profile's declared `release` block
(`profiles/<id>/profile.json`), never from a hard-coded file list.
Members name a flat file, whether they are required, an optional
`when` condition, and where their bytes come from:

* `frozen` (the default): snapshotted bytes as above;
* `engine-log`: serialised by the engine from its own in-memory events
  at pack time (`run.jsonl`, whose last released event is the human
  review). It is never read from working files.
* `engine-report`: rendered by the engine at pack time from stored
  records (`evidence-report.md`, which cites the decision and the
  frozen digest, both of which postdate the snapshot). Like
  `engine-log`, it is never read from working files; unlike the frozen
  members, it is a view over the frozen evidence plus the decision,
  never new evidence itself.

The imaging members (`synthetic.png`, `image-statistics.json`,
`image-validation.json`) carry `when: images_present`, so they ship
exactly when the imaging branch produced a selected synthetic image
for this run and attempt, and are absent otherwise. Acceptance still
requires a passing reviewed report: a failing verdict never reaches
the gate, so nothing about membership can rescue it. Manifests that
declare no release block (test-only shapes) fall back to the
historical member list; packaging still refuses any gap.

## Atomic release

Each run owns its release location: `runs/<run-id>/` holds
`decision.json`, `manifest.json` (the complete release manifest with
the evidence digest, the final digest, every member hash, and the
persisted acceptance record -- the gate verdict plus the required
checks publication binds), `release.zip`, and the detached
`release.sha256` hash. The ZIP is
built from the frozen snapshot in a temporary file, verified, and
renamed into place; any failure mid-packaging leaves no partial
`release.zip` (and no manifest or hash), the decision stays recorded,
and a rerun can complete. Identical frozen inputs always yield
identical ZIP bytes: member order, timestamps, permissions, and
compression are fixed.

## Consumer verification

`engine/artifacts/consumer.py` re-establishes every guarantee from
the bundle alone: entry names must be flat (no absolute paths, no
parent escapes), every declared member must be present with no extras,
every member must match `checksums.sha256`, every `.json` member must
parse, the validation report must carry its required fields with its
verdict reading exactly `pass`, and the report's artifact hash must
resolve to the shipped payload bytes (an older substituted report
fails even when the checksums were re-cut around it). When the
manifest records member hashes and sizes, the shipped bytes must
match those too: a changed report with re-cut internal checksums
still fails against the stale external record, because the external
manifest is authority over names, not just a member list. Any
violation raises loudly; a return means every check passed.

## Rights evidence and output terms

Source identity, licence, and attribution are recorded as separate
fields -- never folded into a single claim -- and the release carries
the verification outcome with them. Intake checks the measured source
hash against the sibling `*-source.json` registry record: a mismatch
fails loudly before any model call, while a missing record (or one
recording no hash) leaves the source unverified without stopping the
run. The release `provenance.md` renders the outcome in words, and the
release manifest carries it as a machine-readable `rights` block
(`source_sha256`, `source_licence`, `source_attribution`,
`manifest_name`, `manifest_sha256`, `source_hash_verified`,
`output_terms`).

Output terms resolve from that evidence only
(`engine/artifacts/package.py: resolve_output_terms`): the synthetic
output is released under the granted term exactly when the source hash
verified *and* the verified sibling record carries an explicit
`output_terms` grant. A recorded source licence alone is never such a
grant. Every other release -- unverified source, unconfirmed licence,
or a verified record with no explicit grant -- states `NOT CLEARED`
explicitly and carries no public-dedication claim. There is no default
dedication in `build_provenance`: without an explicit, evidence-backed
`output_terms` value, the no-clearance statement renders. The release
manifest `rights` block records the grant evidence itself as the field
name (`output_terms`) plus the recorded value.

## Separate publication

Packing writes `release.zip` into the run directory at accept time;
publication (`engine/artifacts/publication.py`) is the step after
that, copying the accepted bundle into a separate published area
with its own durable record (`<base>/publications/`). The worker's
success report is evidence, not authority: it names a run, and
publication resolves that run's persisted acceptance record -- the
detached `release.sha256` binding the shipped bytes, a human
`decision.json` verdict of `accept` binding the frozen evidence
digest, a `frozen-manifest.json` matching that digest, a
`manifest.json` whose members, sizes, hashes, and artifact digest
match the frozen evidence member for member, a shipped validation
report reading `pass` and covering every persisted required check,
and a single selected attempt whose receipt records `pass` for the
artifact. The reported digests must equal the persisted ones (a
worker claim of an all-zero artifact digest refuses instead of
shipping different bytes), and a requested profile must equal the
run's own. It ships only a `succeeded` dispatch item's reviewed bytes
under the owning authority. Request-to-run binding (DP-01): the
request's bound identity (profile, pinned source-content digest,
pinned normalised spec digest) must equal the acceptance-bound and
delivered identities, and a declared row/attempt bound must cover the
run's executed bound -- profile-name equality alone never suffices. The
record moves `in_progress` before
any copy and `complete` only after the destination verifies, so an
interrupted publication resumes to identical bytes and re-publishing
is a no-op; unattributed destination bytes and second releases for
one item are refused. Once complete, a destination that stops
attesting the recorded bytes refuses re-publication -- recovery goes
through the authorised `repair` operation, which preserves any
replaced bytes beside the destination and appends the authorising
decision to the record. Verify and copy operate on one captured
immutable byte snapshot: the run file is never re-read for shippable
bytes, a post-acceptance change refuses at the stability guard, and
the snapshot, destination, and persisted accepted digests must agree
before the complete mark. `download` verifies the new bytes in a unique
temporary sibling and commits atomically: a verified download
overwrites the destination, an unverified one never touches it, so a
good file already there survives byte-identical, and a same
source/destination path verifies in place without deletion.
Corruption inside
the deflated bytes is a `ReleaseVerificationError` refusal, never a
raw decoding crash.

## Feature record

This slice is registered as `F-ENGINE-REVIEWED-RELEASE` under
`catalogue/features/engine/` (rules `Q-RELEASE-R06` and
`Q-RELEASE-R04-MEMBERS`). The source/rights slice is registered as
`F-ENGINE-SOURCE-RIGHTS` in the same directory (rules `Q-DATA-R08`
and `Q-RELEASE-R04-RIGHTS`). The separate-publication slice is
registered as `F-ENGINE-SEPARATE-PUBLICATION` in the same directory
(rule `Q-RELEASE-PUBLICATION`).
