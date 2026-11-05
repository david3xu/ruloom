# Ruloom

**Synthetic data, woven from reality. Backed by evidence.**

[The idea and design](docs/idea-and-design.md) — why Ruloom exists, one example and the role of Muse.

**Final user test: recorded; independent scoring in progress.** [Read the final test record](docs/final-test-record.md)
and the [shared test prompt](docs/final-test.md).

## Who it helps

Ruloom is for people who need a small, trustworthy practice dataset for one
specific task: a teacher preparing a lesson, a freelancer building a demo, a
developer testing a pipeline. Real records are often large, awkward to clean
or not shareable. A small synthetic set that keeps the patterns that matter
for the task, and says plainly what is invented, can be ready in minutes and
shared without handing over the original records.

## A teaching example

A geology teacher wants students to practise reading drill-hole logs. Ruloom
takes a real NVCL HyLogger scalar export for hole HD012 (CC BY 4.0), checks
that it suits the lesson, and generates, say, 200 synthetic depth records. The
records follow the source's depth spacing, its included/excluded flags and its
pattern of blank mineral-group labels. The release states which checks passed
and what the data cannot be used for.

The richer version of this lesson — following one sample from its map
location and depth to its picture and infrared curve — is the target of the
[pending final test](docs/final-test.md). It is not yet a supported result.

## How it works

1. **Request and evidence.** A plain-language request is matched to an
   installed teaching case that cites real, admitted sources.
2. **Source suitability.** Code checks the source format and whether the
   requested size is feasible, before any model is called.
3. **Plan.** A model proposes a small, schema-bound plan (seed, row count,
   strategy). It never writes data rows and never sees raw source rows.
4. **Generate and check.** Seeded code generates the rows; deterministic checks,
   each bound to the artifact's SHA-256, decide pass or fail. Same plan and seed
   give the same bytes.
5. **Review and release.** Advisory model reviews are recorded separately and
   cannot change a verdict. A person accepts or rejects; only an accepted run
   is packaged as a verifiable release ZIP.

Details: [request-to-data workflow](docs/product-workflow.md) and the
[operator guide](docs/research-workflow-user-guide.md).

## The three parts

- **Engine** (`engine/`): domain-free. It runs a profile's step/loop/gate graph,
  enforces each step's permissions and token budget, owns verdicts and releases,
  and keeps a hash-chained run log.
- **Domain profiles** (`profiles/`): the rules for one kind of data — intake,
  statistics, generation and checks. `hylogger` covers drill-hole scalars (plus
  an optional synthetic-picture extension); `tabular` covers generic CSV.
- **Model adapters** (`models/`): the only code that talks to a model. Roles bind
  explicitly to Muse via the hosted CLI, Azure AI Foundry or a local Glimmer
  server, or to other families through AGY. There is no silent provider fallback.

Design rules and operating details: [engine and operations reference](docs/engine-and-operations.md)
and [model configuration](docs/model-families.md).

## What Muse has contributed so far

Only these runs are claimed; each is recorded with its date and limits.

- **Building Ruloom (1–4 November 2026).** Datacore records Muse Spark 1.3, in contributor
  mode, as the worker on 62 development tasks and the reviewer on 53, with 51 approvals and
  one rework request. These are recorded worker identities, not a per-call audit; see the
  [development evidence](docs/evidence/development/README.md).
- **Advisory critics (2 November).** Muse Glimmer 30B, run locally, wrote a text critique of
  a real PDD446 validation report, and hosted Muse Spark 1.3 reviewed a synthetic PNG
  ([receipt](docs/quality/critic-receipts-gp01.md)).
- **The product workflow on Azure (5 November).** In the final user test's second
  comparison, Muse Spark 1.3 filled the planner, source-assessor, case-researcher and
  case-reviewer roles. Managed Muse Glimmer returned an accepted structured review of the
  synthetic picture. Earlier attempts that day failed and stay on record
  ([final test record](docs/final-test-record.md)).

**Planned use.** Muse roles for researching cited examples, planning, and reviewing the
generated data and pictures. Each role keeps a narrow job; code keeps the verdict and a
person keeps the decision. A Muse Wonder reporter seat is declared but disabled.

## Scope, limits and test status

**Supported:** the bounded HyLogger scalar teaching case
(`hylogger_scalar_teaching_v1@1`; four columns `depth_m`, `valid`,
`swir_group`, `tir_group`), the generic tabular profile, offline replay of a
reviewed plan, and release verification.

**Not supported:** hole or sample identifiers, coordinates, linked
picture-and-curve packages, real core photographs or reflectance spectra,
custom tolerances, geological prediction, and any privacy, anonymity or
licensing guarantee. The image extension renders fictional schematic strips
from fixture statistics only.

**Test status (5 November 2026, revision `3216c5c`):** the local full suite gave 1,321
passed and 4 skipped, with source data present. `scripts/force_pass.py` exited 0 and main CI
is green. The final user test is recorded in [docs/final-test-record.md](docs/final-test-record.md).
Ruloom refused the full linked-lesson request. A narrowed, scalar-plus-picture request then
ran end to end on live Muse models, and both releases verified. Earlier dated results stay
in [`docs/results.md`](docs/results.md) and [`docs/quality/`](docs/quality/README.md).

## Quick start

Python 3.12. Fetch source data as described in the
[data notes](docs/engine-and-operations.md#data) (it is not committed), then:

```sh
.venv/bin/ruloom prepare data/sources/HD012-scalars.csv --profile hylogger \
  --case hylogger_scalar_teaching_v1@1 --max-attempts 2 \
  --request "Create 200 synthetic depth records for a lesson on inclusion flags and blank labels." \
  --model-policy azure-only --out draft
.venv/bin/ruloom run data/sources/HD012-scalars.csv --profile hylogger \
  --spec draft/spec.json --plan draft/plan.json --max-attempts 2 \
  --model-policy azure-only --out work
.venv/bin/ruloom verify work/runs/<run-id>/release.zip \
  --require-case hylogger_scalar_teaching_v1@1
```

Bind every active role to your provider first (or replay a reviewed plan with
`--model-policy offline`); see the
[operator guide](docs/research-workflow-user-guide.md). Checks:
`.venv/bin/python -m pytest -q` and `.venv/bin/python scripts/force_pass.py`.

## Documentation

- [Idea and design](docs/idea-and-design.md) · [Final user test](docs/final-test.md) · [Final test record](docs/final-test-record.md)
- [Request-to-data workflow](docs/product-workflow.md) · [Operator guide](docs/research-workflow-user-guide.md) · [Installed-wheel acceptance](docs/full-workflow-acceptance.md)
- [Engine and operations reference](docs/engine-and-operations.md) · [Model configuration](docs/model-families.md) · [Synthetic-image review](docs/vision-workflow.md)
- [HyLogger case workflow](docs/hylogger-case-workflow.md) · [Provenance](docs/provenance.md) · [Decisions](docs/decisions.md)
- [Results](docs/results.md) · [Quality records](docs/quality/README.md) · [Development evidence](docs/evidence/development/README.md)

Built for the Meta Global AI Developer Hackathon (26 October – 4 November 2026).
Source data is public NVCL WA data under CC BY 4.0, used as fetched data only.

## Layout

| Path | What lives there |
|---|---|
| `engine/` | The generic lifecycle root (`__init__.py` re-exports only; no feature module sits flat here — FP-CODE-8) |
| `engine/core/` | Graph execution with permission enforcement (`runner.py`), profile manifests (`manifest.py`), strict run records (`records.py`), durable per-run history (`runstore.py`), durable dispatch with leases/fencing (`dispatch.py`) |
| `engine/artifacts/` | Content addressing (`hashing.py`), manifest-driven frozen release packaging (`package.py`), consumer-side verification (`consumer.py`), separate publication of accepted releases (`publication.py`) |
| `engine/audit/` | The append-only hash-chained log (`runlog.py`) |
| `engine/cli/` | The profile runner package: `ruloom run --profile <id>` entry (`__init__.py`) plus `__main__.py` so `python -m engine.cli` works too |
| `profiles/` | Profile registry and loader (`__init__.py`): new profiles arrive as new directories, never as engine edits |
| `profiles/hylogger/` | The first domain profile: `profile.json` (manifest: capabilities, permissions, checks, graph), `thresholds.json`, `capabilities.py` (manifest step implementations); features in `intake/` (`reader.py`), `analysis/` (`statistics.py`, `calibration.py`), `generation/` (`generate.py`, `plan.py`, `repair.py`), `validation/` (`checks.py`, `dataset.py`, `critique.py` — advisory text critique over the validation report plus profile statistics, bound to the `local_critic` role) |
| `profiles/hylogger/imaging/` | The HyLogger image extension (optional `--images DIR` branch after validate, before the human gate): `codec.py` (stdlib PNG encode/decode), `intake.py` (image hashing/attribution), `statistics.py` (code-only histograms/texture/strips), `generate.py` (seeded synthetic PNGs), `scene.py` (schematic-only shared scene record driving table + image views, R03), `checks.py` (deterministic image checks incl. ordered strip and scene alignment, R03), `critic.py` (advisory vision note over synthetic renders only) |
| `profiles/tabular/` | The second profile (common CSV proof): same root wiring (`profile.json`, `thresholds.json`, `capabilities.py`); features in `intake/` (`reader.py`), `analysis/` (`statistics.py`, `calibration.py`), `generation/` (`generate.py`, `plan.py`), `validation/` (`checks.py`, `dataset.py`) |
| `models/` | Model adapters — the only code that talks to a model: shared contract and failure reasons (`base.py`), hosted Muse CLI (`meta_cli.py`), AGY families (`agy_cli.py`, `selection.py`), local Glimmer over loopback (`glimmer.py`, `local_transport.py`), Azure AI Foundry (`azure_foundry.py`) and image validation (`image_input.py`) |
| `catalogue/features/engine/` | Feature records for the persistence and reviewed-release slice: one JSON file per feature (stable id, owner, code/doc/test paths, required rules, status) |
| `docs/engine/core/` | Engine-core contracts (`README.md` states the run/attempt persistence contract) |
| `docs/engine/artifacts/` | Engine-artifacts contracts (`README.md` states the freeze/manifest/atomic/consumer release contract) |
| `tests/` | Mirrors the tree: `tests/engine/`, `tests/profiles/hylogger/`, `tests/profiles/tabular/` (one `test_<feature>.py` per feature module), gate tests at the root |
| `runs/` | Per-run durable history under `runs/<run-id>/` plus the content-addressed payload cache under `runs/<payload-sha>/` (generated at runtime, git-ignored — absent in a fresh clone) |
