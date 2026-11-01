# Ruloom

**Synthetic data, woven from reality. Backed by evidence.*

A plain-language dataset requirement becomes reproducible synthetic data,
calibrated against real data, proven by executed checks, delivered as a CC0
package with its evidence.

Synthetic data is easy to generate and hard to trust. Ruloom's answer is
calibration: quality thresholds are derived from how real datasets differ from
each other, the generator is fitted to statistics of the real source, and
every quality claim is a deterministic check result bound to the SHA-256 of
the artifact it was computed over.

## The four rules

Every design decision in this repository traces to one of these.

1. **The model plans; it never writes data rows.** The plan is a JSON object
   constrained by a schema. Not a single value in the delivered dataset comes
   out of a model.
2. **Seeded code generates.** Same plan + same seed ⇒ same bytes. A run is
   replayable by anyone.
3. **Deterministic checks prove.** Every quality claim is a check result bound
   to an artifact hash. A claim with no check behind it does not appear.
4. **A human accepts.** No model approves its own output. The critic is
   advisory and cannot change a pass/fail verdict.

Two consequences that are easy to violate by accident:

- **No raw source rows ever reach a model.** The profile stage passes summary
  statistics only — a leakage control and a licence control at once.
- **If repair attempts are exhausted, the run stops and reports the failing
  checks.** It never ships as passing.

## Status

New work, written fresh for the Meta Global AI Developer Hackathon
(Oct 26 – Nov 4, 2026). Nothing here is imported from any prior project;
source data is public NVCL WA data under CC BY 4.0, used as data only —
see `docs/provenance.md` once the source intake lands.

## Layout

| Path | What lives there |
|---|---|
| `engine/` | The lifecycle, the append-only hash-chained log, content addressing |
| `profiles/hylogger/` | The first domain profile: reader, statistics, calibration, generator, checks |
| `models/` | Model adapters — the only code that talks to a model |
| `runs/` | Run artifacts, addressed by SHA-256 |
