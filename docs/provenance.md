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
- The source data is **not relicensed** by its presence here. CC BY 4.0
  terms and the attribution above continue to apply to everything under
  `data/`.

## Synthetic output

Output terms are recorded per release (the release `provenance.md`
plus the release manifest). No blanket licence is promised here: a
release is not cleared for public dedication by statistical similarity
or by a generic acceptance prompt. A release that reuses another
source's licence text without a matching source-hash check has no
output-rights clearance.

## Model boundary

No model in this pipeline is given source rows. The only
source-derived content any model may see is the `Profile.to_json()`
summary statistics for a hole, which carries the source file's SHA-256
and name for traceability. That scoping limits exposure but is not an
anonymity or non-copying guarantee: text statistics preserve literal
values (a single-row fixture round-trips exactly), and identical
reference images cannot prove non-copying. Formal privacy claims are
unsupported.
