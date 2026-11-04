# Human-style drill-sample request trial

4 November 2026, personally by Codex. Code revision:
`797989e97ef7e229eee94ebb50bcfd0eab4a5c63`.

> I need 10,000 fictional drill-sample records for a data-checking lesson. Keep the depth spacing, blank result labels and inclusion flags similar to this source, and show which checks passed.

The purpose is to teach people to recognise incomplete and out-of-use
records without treating the practice table as observations of another site.
The request was adapted from an existing evidence-review teaching use case.
The other project's documents and inputs were not changed.

**Result: failed at request preparation.** AGY reported Claude Opus 5.5 high
and returned a schema-valid interpretation, but marked its requirements
unsupported. Ruloom correctly stopped rather than silently omitting them.
No runnable spec, plan, synthetic table, acceptance or release was produced.
This is not a failed generated-data quality check: generation never began.

The existing local HD012 scalar source has 10,260 rows. Its hash was checked
before and after the test and remained unchanged. No raw source rows were
sent. Only AGY was permitted by the harness; there were zero Muse calls.
The one live call took 50.703 seconds and used only the schema-finalisation
`finish` tool. Identity is CLI-reported, not independently verified weights.

## Findings

- **Capability description gap:** the preparation prompt gives strategy
  names and the output-settings schema, without telling the model about
  source-fitted generation, applicable validators or built-in reports.
  The reply incorrectly says there is no check report. The manifest already
  includes `validation-report.json` and `evidence-report.md`; validation
  already measures geometry, flags, answer rates, vocabulary and fidelity.
- **Meaning drift:** the reply treats “fictional” as a guarantee that no row
  can coincide with a source record. That guarantee was not requested and
  is not established. It also claims to keep the source columns, whereas
  the generator uses its canonical four-column output schema.

The next targeted correction is a truthful, profile-owned capability brief
for request interpretation. Describe what checks can measure and reports
can show, while keeping exact-match, scientific-validity and privacy
claims outside that contract. Retest the same human request without
rewriting it as internal strategy and seed instructions. Do not weaken
refusal or silently remove the requested quality checks.

A prospective engineering quality contract was saved before generation:
10,000 rows, ordered finite nonnegative depths, source-supported labels,
median spacing within 1%, and flag/blank-label fractions within two
percentage points. These are proposed lesson-test criteria, not an industry
standard. They were **not executed**, because the request was refused.

[Sanitised response and test receipt](human-request-trial-2026-11-04.json)
retain the model's complete interpretation, usage, source digest and limits.
Raw local logs remain outside the repository. Product code and source data
are unchanged; no full regression rerun was needed for this read-only test.
