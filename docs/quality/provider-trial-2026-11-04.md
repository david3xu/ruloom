# Claude through AGY, then Muse: controlled trial

Observed 4 November 2026 (Australia/Perth), personally by Codex. Code tested:
`6385c9eee337ef81a1322008280b80fe438c6779`. Both final trials ran at this
exact committed revision, sequentially, with no code changes between them.
The owner authorised Claude first and Muse only after Claude passed.

| Trial | Result | Generated rows | Scalar checks | Bundle verification | Full trial wall time |
| --- | --- | --- | --- | --- | --- |
| AGY / Claude Opus 5.5 high | PASS | 100 | 4 PASS, 0 skipped | PASS | 86.380 s |
| Muse / Spark 1.3 high | PASS | 100 | 4 PASS, 0 skipped | PASS | 65.979 s |

These times cover preparation, fresh planning, offline runs and checks. They
are observations from one sequential case, not a speed ranking. Providers
reported usage differently and had different cached context.

## Frozen case and observed checks

Both used the same invented 20-row source, request, row target, seed 42,
strategy, two-attempt bound and 25% mean-drift tolerance. The request asked
for exactly 100 scalar rows using `fitted_gaussian_disclosed`. No private or
geological source observations were used. `prepare` sent no source rows;
fresh planning saw only the profile's disclosed statistics.

Each trial passed request interpretation, offline generation, byte-identical
saved-plan replay, fresh live planning, recipient ZIP verification, rejection
with no release, and an unchanged source digest. The four scalar checks were
non-empty rows, schema columns, exact row count and mean drift. Both offline
outputs had the same data digest because code generated them from the same
source and seed; that does not establish equivalent model capabilities.

Model configuration selects a role, transport, family, model and effort.
The AGY CLI reported `claude-opus-5-5-high`; Muse usage reported
`muse-spark-1.3`. Each final trial made one live preparation call and one fresh
planning call. Offline/rejection runs made no model call. AGY recorded only
the `finish` schema-finalisation tool. Its other tools remained exposed,
so no actual tool containment is claimed.

## Findings and verification

Early integration calls refused unsupported AGY setting names before any
inference. One live Claude request then falsely treated demo purpose as an
unsupported fitness guarantee. The prompt now distinguishes intended use
from an explicitly requested guarantee and explains that the tool binds the
source locally. The unsupported-requirement gate remains intact. Earlier
failures remain in the machine-local logs; the table reports final runs.

Automated source-free regression suite: **756 passed, 14 source-dependent
tests skipped**. Aggregate gate: **47 PASS outcomes, 0 FAIL**, comprising 43
executed rules and four planned stubs. Ruff and diff checks passed. The new
26-test AGY checker runs in the mandatory gate without calling a provider.

These are software trials on invented scalar rows. Accept/reject inputs
were automated engineering-test inputs, not owner or scientific approval.
No independent code review, image testing, local-model testing, broad
data-quality assessment, production isolation or provider ranking is claimed.
Main was not merged as part of this test.

Portable, sanitised counts, identities, hashes and stage receipts are in the
[trial evidence](provider-trial-2026-11-04.json). Raw transcripts, bundles and
earlier failures remain machine-local. Reproduction uses the explicit,
quota-consuming [provider smoke command](../model-families.md#opt-in-live-smoke-test).
