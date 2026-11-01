# Ruloom

**Synthetic data, woven from reality. Backed by evidence.**

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

## Data

HyLogger source data is **not committed to this repository**, following GSWA
hackathon guidance and the data licence. Fetch it from the National Virtual
Core Library (NVCL) WA node, via AuScope — scalar downloads from
`http://geossdi.dmp.wa.gov.au/NVCLDataServices/downloadscalars.html` — and
place it under `data/` (git-ignored):

- `data/sources/` — the five continuous holes used as generation sources:
  `20LCDD003`, `DDG007`, `HD012`, `NCB0003`, `PDD446`.
- `data/calibration/` — the 38-hole calibration set used to derive the
  quality thresholds in `profiles/hylogger/thresholds.json`; each hole's
  file hash is recorded inside that JSON.

Each hole is a `*-scalars.csv` with a sibling `*-source.json` manifest
recording licence `CC-BY-4.0` and the attribution (Geological Survey of
Western Australia, National Virtual Core Library (NVCL) WA node, via
AuScope). Only summary statistics and check results derived from the data
are committed, plus the synthetic outputs the run releases. Full provenance
statement: `docs/provenance.md`.

## Status

New work, written fresh for the Meta Global AI Developer Hackathon
(Oct 26 – Nov 4, 2026). Nothing here is imported from any prior project;
source data is public NVCL WA data under CC BY 4.0, used as fetched data
only — see `docs/provenance.md`.

First-stage product is complete: one command runs the full lifecycle
(intake, profile, plan, generate, validate, bounded repair, human-gated
release). Verified on a real PDD446 run: plan `{seed 42, n_rows 39300}`,
validation verdict pass, artifact `be9e26d7e664…`, release ZIP
`46c7c07110c5…` with a hash-chained run log that verifies end to end.
Test suite: 134 passed. Force-pass gate (`scripts/force_pass.py`):
7/7 rules PASS. Run it with:

```bash
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3
# Generic profile path (the default profile is hylogger):
.venv/bin/ruloom run --profile tabular <source>.csv --max-attempts 3
```

## Layout

| Path | What lives there |
|---|---|
| `engine/` | The generic lifecycle root (`__init__.py` re-exports only; no feature module sits flat here — FP-CODE-8) |
| `engine/core/` | Graph execution with permission enforcement (`runner.py`), profile manifests (`manifest.py`), strict run records (`records.py`) |
| `engine/artifacts/` | Content addressing (`hashing.py`), release packaging (`package.py`) |
| `engine/audit/` | The append-only hash-chained log (`runlog.py`) |
| `engine/cli/` | The profile runner package: `ruloom run --profile <id>` entry (`__init__.py`) plus `__main__.py` so `python -m engine.cli` works too |
| `profiles/` | Profile registry and loader (`__init__.py`): new profiles arrive as new directories, never as engine edits |
| `profiles/hylogger/` | The first domain profile: `profile.json` (manifest: capabilities, permissions, checks, graph), `thresholds.json`, `capabilities.py` (manifest step implementations); features in `intake/` (`reader.py`), `analysis/` (`statistics.py`, `calibration.py`), `generation/` (`generate.py`, `plan.py`, `repair.py`), `validation/` (`checks.py`, `dataset.py`) |
| `profiles/tabular/` | The second profile (common CSV proof): same root wiring (`profile.json`, `thresholds.json`, `capabilities.py`); features in `intake/` (`reader.py`), `analysis/` (`statistics.py`, `calibration.py`), `generation/` (`generate.py`, `plan.py`), `validation/` (`checks.py`, `dataset.py`) |
| `models/` | Model adapters — the only code that talks to a model (`base.py`, `meta_cli.py`) |
| `tests/` | Mirrors the tree: `tests/engine/`, `tests/profiles/hylogger/`, `tests/profiles/tabular/` (one `test_<feature>.py` per feature module), gate tests at the root |
| `runs/` | Run artifacts, addressed by SHA-256 |
