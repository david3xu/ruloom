# Request capability implementation — 4 November 2026

**Draft; independent review and live interpretation remain blocked by quota.
This is not a product-readiness approval.** Machine-readable measurements:
[receipt](request-capabilities-2026-11-04.json).

## Change and design review

Implementation revision `b206e5ca8f84c3a002829d2d41f18aac2bff80d8` extends
`18ba5ee`. The local initial design documents 06, 07, 08, 09, 12 and 13 were
re-read for the lifecycle, product audience, profile boundaries, model roles,
quality measurements and coordinator-only handoffs. The
[current workflow](../product-workflow.md) keeps the usable local scalar
journey and explicitly excludes unproved early-design promises such as
automatic CC0 release, anonymity and real image fidelity.

The earlier [human request trial](human-request-trial-2026-11-04.md) failed
because interpretation was missing implemented capability information. The
change supplies strictly validated profile text, actual applicable checks,
release members and digest-bound context. Generators, thresholds, human
acceptance and publication logic are unchanged. Model interpretation still
needs operator review; metadata shape validation cannot verify its meaning.

## Development handoffs

- AGY CLI reported `claude-opus-5-5-high`, effort high, session
  `af5c6229-0874-407e-9c39-1a31c162183d`. It produced partial code before an
  individual quota error. Codex completed the code, tests and documentation.
- Requested independent AGY Sonnet 5.5 high review, session
  `fbe62bf5-e2c5-4de6-9f4b-d7380f289443`, was refused by quota before any
  review work (zero tokens reported). **No independent PASS exists.**
- Codex coordinated and recorded the handoffs in Datacore project `ruloom`,
  ledger `ruloom-human-request-readiness-20261104`. Runtime had no eligible
  registered workers; the records describe external CLI sessions and do not
  claim automated Runtime leases or approval. No nested AI dispatch was
  observed in the implementation stream. Tool exposure is not confinement.

## Checks observed

Codex ran `.venv/bin/python -m pytest -q`: **770 passed, 14 source-dependent
tests skipped** on the implementation revision. The aggregate gate exited
zero: **43 enforced checks passed and 4 planned stubs were not executed**.
Ruff and `git diff --check` passed. A further installed-wheel request test
exercises the new module/profile metadata outside the repository; later
checks must be read from the final pushed revision's CI.

After adding that installed-wheel test, Codex reran the complete suite:
**771 passed, 14 source-dependent tests skipped** (28.73 seconds).

The gate executes the request regressions, including malformed declarations
refusing before model contact, old manifests without a brief, context
overflow, required/optional/conditional check handling, source canary/path
absence, changed-source refusal, exact row counts and offline execution.
These are fixtures, not proof that a live model understands every request.

## Independent real-source measurement

The unchanged 10,000-row lesson request was exercised with a **fixture
interpretation and fixed seed 42**, followed by actual offline generation
from the recorded HD012 source. No live AGY or Muse call occurred. This
does not retest the failed model interpretation.

All 28 applicable required profile checks passed; the optional vacuous
`swir_answer_rate` check was reported as skipped. The original source hash
remained unchanged. The stricter engineering contract was fixed before the
run; it is not an industry standard:

| Measurement | Source | Generated | Predeclared result |
| --- | ---: | ---: | --- |
| Rows | 10,260 | 10,000 | Exact requested output count passed |
| Median positive spacing (m) | 0.007540 | 0.007540 | Within 1% passed |
| Inclusion-flag fraction | 0.750682 | 0.776800 | Difference 0.026118 exceeds 0.02: **failed** |
| SWIR answer fraction among valid rows | 0.849000 | 0.852343 | Difference within 0.02 passed |
| TIR answer fraction among valid rows | 0.988834 | 0.989186 | Difference within 0.02 passed |

Finite, nonnegative, ordered values and source-supported group labels also
passed. The test operator rejected the candidate because of the flag-rate
difference, and **no release was written**. No alternative seeds were tried
to select a passing result, and no thresholds were weakened. Replay and
recipient verification were not reached in this case. Their existing fixture
checks passed in the suite; that is separate evidence.

The profile currently checks inclusion fraction against calibrated ranges,
not this extra source-centred 2-point limit. This is a visible capability
limit and a follow-up design question; arbitrary per-request tolerances are
not supported by this change.

## Resume and approval conditions

After the quota resets, have the requested Sonnet reviewer examine the final
revision and all changes since `18ba5ee`. Resolve substantive findings, then
rerun checks on any corrections. Run the original prompt through the real
AGY interpreter without changing the wording; retain explicit unsupported
privacy-request refusal. Define any stronger source-specific flag-fidelity
requirement and its check before changing generation. Preserve this failed
case, and do not describe it as an accepted dataset or comparative win.
