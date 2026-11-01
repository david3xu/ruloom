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
the evidence digest, the final digest, and every member hash),
`release.zip`, and the detached `release.sha256` hash. The ZIP is
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
parse, the validation report must carry its required fields, and the
report's artifact hash must resolve to the shipped payload bytes (an
older substituted report fails even when the checksums were re-cut
around it). Any violation raises loudly; a return means every check
passed.

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
output is dedicated under `CC0-1.0` exactly when the source hash
verified *and* a source licence was recorded. Every other release --
unverified source, unconfirmed licence -- states `NOT CLEARED`
explicitly and carries no public-dedication claim. There is no default
dedication in `build_provenance`: without an explicit, evidence-backed
`output_terms` value, the no-clearance statement renders.

## Feature record

This slice is registered as `F-ENGINE-REVIEWED-RELEASE` under
`catalogue/features/engine/` (rules `Q-RELEASE-R06` and
`Q-RELEASE-R04-MEMBERS`). The source/rights slice is registered as
`F-ENGINE-SOURCE-RIGHTS` in the same directory (rules `Q-DATA-R08`
and `Q-RELEASE-R04-RIGHTS`).
