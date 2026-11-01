# Provenance

## Source data

The files under a local `data/` checkout (`data/sources/` and
`data/calibration/`) are National Virtual Core Library (NVCL) Western
Australia drill-hole scalar data. They are **source data**, kept as
a git-ignored local checkout (never committed), frozen and
unmodified for calibration only.

- Licence: **CC BY 4.0**.
- Attribution (verbatim from each per-hole manifest):

  Geological Survey of Western Australia, National Virtual Core Library (NVCL) WA node, via AuScope. Licensed CC BY 4.0.

- Each hole has a `<HOLE>-source.json` manifest recording the dataset id,
  borehole URI, retrieval URL, retrieval timestamp, file SHA-256, size in
  bytes, licence, attribution, and row count. The CSVs are hashed in their
  manifests; the manifests are the integrity record.
- Intake verifies the registry record: the measured source hash must equal
  the manifest's recorded `sha256`, or intake fails loudly before any model
  call. A source with no matching record still runs, but stays unverified:
  its licence and attribution are carried as unconfirmed, and the release
  carries no output-rights clearance. The verification outcome
  (`manifest_name`, `manifest_sha256`, `hash_verified`) is stored with the
  run and shipped as the separate `rights` block of `manifest.json`.
- The source data is **not relicensed** by its presence here. CC BY 4.0
  terms and the attribution above continue to apply to everything under
  `data/`.

## Synthetic output

Output terms are recorded per release (the release `provenance.md`
plus the `rights` block of the release manifest). No blanket licence is
promised here: a release is not cleared for public dedication by
statistical similarity or by a generic acceptance prompt. A release that
reuses another source's licence text without a matching source-hash check
has no output-rights clearance. The only dedication the pipeline ever
states is the granted term over the synthetic output, and only when
the source hash verified against the sibling manifest at intake and that
verified record carries an explicit `output_terms` grant. A recorded
source licence alone is never such a grant -- even a permissive licence
without the grant field, or a restrictive licence, leaves the release
`NOT CLEARED`. Every other release states `NOT CLEARED` explicitly,
with no dedication implied.

## Model boundary

No model in this pipeline is given source rows. The only
source-derived content any model may see is the *disclosed* statistics
view for a hole (`to_disclosed_json()`), which carries the source file's
SHA-256 and name for traceability but withholds rare literals: a text
value (tabular) or group label (HyLogger) shared by fewer than two
source rows never reaches a prompt, and the seeded tabular generator
draws from the same disclosed vocabulary, so a single-row source cannot
be recreated exactly. The full aggregate record (`to_json()`) stays
code-side for checks, generation fits, and logs. Withheld counts are
recorded (`suppressed_values`, `suppressed_label_count`), never the
withheld literals. This scoping limits exposure but is not an anonymity
or non-copying guarantee: numeric aggregates still describe the source,
and identical reference images cannot prove non-copying. Formal privacy
claims are unsupported.
