# Ruloom

**Synthetic data, woven from reality. Backed by evidence.**

A supported source profile becomes reproducible synthetic data,
calibrated against real data, proven by executed checks, and delivered
with its evidence under the terms recorded in each release.

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
  statistics only — a scoping control, not an anonymity guarantee (see
  Supported scope below).
- **If repair attempts are exhausted, the run stops and reports the failing
  checks.** It never ships as passing.

## Engine authority

The engine privately owns verdicts, chosen artifacts, and state
transitions. Each capability receives a frozen view — its declared
inputs, statistics views, prior attempt records — and returns declared
outputs through validated engine methods. Capabilities get no reference
to the runner, no mutable shared state, and no path to
verdict/artifact/decision state. Advisory outputs land in a separate
store that verdict computation never reads, so a critic can never flip
a deterministic result. Raw source bytes, source paths, and parsed
source frames are materialised only for capabilities declaring
`reads_raw_source`; every other capability works from statistics views.

Acceptance is a contract, not a counter. Each manifest declares its
check set with per-check `required` status (HyLogger image checks
additionally declare `applicable_when: images_present`). The verdict
comes only from results that belong to the current attempt, name the
exact `artifact_sha256` they measured, and carry a recognised status
(`pass`, `fail`, `error`, `unsupported`, `skipped`) — only `pass`
satisfies a required check. Missing, duplicated, unknown-required,
error, unsupported, or skipped required checks block acceptance, and an
empty check set cannot pass. One declared exception:
HyLogger `swir_answer_rate` is `required: false` with a manifest note
because its calibrated band is vacuous; the skip is shown in the report
but never gates, until RP-05 delivers informative bands.

Trust assumption: this boundary is in-process typing against
accidental misuse, not a sandbox. Running untrusted third-party
capabilities would need isolated processes with restricted input mounts
and mediated model/network access.

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

Core imagery is a separate, optional source. Real HyLogger core images
live in the TSG packages behind the same NVCL node (scalar service above
serves values only; imagery downloads come from the NVCL imagery service,
and the `pytsg` package reads TSG files). No image files are committed
here and none were available locally when the machinery was built, so the
image extension is proven on deterministic fixture PNGs only — no claim
about real images is made. To run with real imagery, export the tray
images to PNG and point `ruloom run --profile hylogger SOURCE --images DIR`
at the directory (one `*-source.json` sibling manifest per image for
licence/attribution, same pattern as the scalars). JPEG files are
inventoried at intake but carry no statistics (no stdlib JPEG decoder);
only PNG is decoded, statistics are computed by code, the synthetic
render is seeded code fitted to those statistics, and a vision model may
look at the synthetic render as an advisory critic only.

A second, text-only critic seat is bound to a local Muse Glimmer model
served over an OpenAI-compatible endpoint. It reads the run's validation
report plus the profile statistics summary only — never raw rows, never
images — and writes an advisory `critic-note.json`
(`{critique, concerns}`) to the run directory alongside the run-log
advisory event. It runs after validate and before the human gate, and
like every advisory it cannot change a pass/fail verdict. It is opt-in:
pass `ruloom run --profile hylogger SOURCE --local-critic
--max-attempts N`; without the flag the step is a silent no-op and the
run is byte-identical to a run without it. With the flag on and the
server unreachable, the run fails loudly with the adapter error instead
of skipping the critique. To serve the model locally:

```bash
llama-server -m ~/models/muse-glimmer/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf --port 8080
```

then run with the default adapter base URL `http://127.0.0.1:8080`
(override per role with the `base_url` binding, or per client with
`GlimmerLocalModel(base_url=...)`). The image weights (`mmproj`) for
Glimmer are not present, so Glimmer serves the text critique only; the
Spark multimodal adapter stays the vision critic for synthetic renders.

## Model roles, budget, and overrides

Each profile manifest binds named roles to models under `models.roles`
(`planner`, `critic`, `local_critic` for HyLogger; `planner` for the
tabular profile). A role binding names its `adapter` (`meta_cli` for
the hosted `muse exec` adapter, `glimmer_local` for the local
OpenAI-compatible server) plus the model id, reasoning effort, and —
for local roles — the base URL.

Three guardrails from the engine design are enforced as behaviour, not
prose:

- **Fail-closed requirements.** A binding may declare
  `requires: {open_weight: true}` (HyLogger's `local_critic` does).
  Loading a manifest — or composing a client — that puts such a role on
  the hosted adapter is refused loudly with no run and no model call.
- **Token budget.** Each manifest declares
  `models.budget.max_tokens_per_run` (HyLogger `500000`, tabular
  `250000`). These are declared limits, not measured results — they sit
  several times above the largest observed full runs. The engine counts
  every model call's `input + output + reasoning` tokens as the adapters
  report them and refuses the next call *before* making it once the
  ceiling is reached, failing the run with the budget error. Local
  Glimmer calls count exactly the same way: cost is zero, but the budget
  is denominated in tokens, not money.
- **Disabled roles never resolve.** HyLogger declares a `reporter` seat
  (`muse-wonder-1.0`, `enabled: false`). No capability is wired to it,
  and binding any capability to it is refused at composition. The seat
  documents where a future Wonder-based reporter would attach; refusal
  is the feature.
- **Band position.** Every band check (HyLogger fidelity/invariant bands
  plus the tabular fidelity band) reports `band_position` alongside
  pass/fail: the measured value's fractional position across `[lo, hi]`
  (`lo` → `0.0`, midpoint → `0.5`, `hi` → `1.0`; outside, the signed
  distance in band-widths, negative below `lo`, above `1.0` past `hi`).
  It rides in each check result and into `validation-report.json`. No
  existing field changed meaning and pass/fail semantics are untouched.

Role fields resolve **CLI > env > profile > engine default**: repeatable
`--role-model ROLE=MODEL` (plus `--role-effort` and `--role-base-url`)
beat `RULOOM_ROLE_<ROLE>_MODEL` / `_EFFORT` / `_BASE_URL` (role
uppercased, e.g. `RULOOM_ROLE_PLANNER_MODEL`), which beat the manifest
binding, which beats the adapter default (`muse-spark-1.3` /
`muse-glimmer-30B`, effort `max`, local base URL
`http://127.0.0.1:8080`). The composition root applies this precedence
and records the effective binding — value plus source
(`cli`/`env`/`profile`/`default`) — as a `model_bindings` event in the
run log. Example:

```bash
RULOOM_ROLE_PLANNER_EFFORT=low .venv/bin/ruloom run --profile hylogger \
  data/sources/PDD446-scalars.csv --max-attempts 3 \
  --role-model planner=muse-spark-1.3
```

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
Test suite: 274 passed. Force-pass gate (`scripts/force_pass.py`):
12/12 rules PASS. The engine is generic: profiles declare their
step/loop/gate graphs and permissions, and the same engine code runs
the HyLogger profile and the common tabular profile
(`ruloom run --profile tabular ...`). Run it with:

```bash
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3
# Generic profile path (the default profile is hylogger):
.venv/bin/ruloom run --profile tabular <source>.csv --max-attempts 3
```

## Supported scope

The supported product is the categorical scalar profile (HyLogger
scalars and the common tabular profile): seeded generation from
statistics, deterministic checks, and a human-gated release. Image
support is fixture-proven machinery that renders procedural schematics
from statistics; it is not registered core photography and proves no
sample-to-image alignment. There is no dispatch, lease, or publication
service in this tree — the CLI runs one local lifecycle. The CLI takes
a source file and options, not a plain-language requirement, and there
is no requirement-clarification step. Statistics-only prompts limit
what a model sees; they are not an anonymity guarantee. Output terms
are recorded per release (`provenance.md` plus the release manifest);
no blanket licence promise is made here. Historical results in
`docs/results.md` stay tied to the revisions and runs that produced
them.

## Layout

| Path | What lives there |
|---|---|
| `engine/` | The generic lifecycle root (`__init__.py` re-exports only; no feature module sits flat here — FP-CODE-8) |
| `engine/core/` | Graph execution with permission enforcement (`runner.py`), profile manifests (`manifest.py`), strict run records (`records.py`), durable per-run history (`runstore.py`) |
| `engine/artifacts/` | Content addressing (`hashing.py`), manifest-driven frozen release packaging (`package.py`), consumer-side verification (`consumer.py`) |
| `engine/audit/` | The append-only hash-chained log (`runlog.py`) |
| `engine/cli/` | The profile runner package: `ruloom run --profile <id>` entry (`__init__.py`) plus `__main__.py` so `python -m engine.cli` works too |
| `profiles/` | Profile registry and loader (`__init__.py`): new profiles arrive as new directories, never as engine edits |
| `profiles/hylogger/` | The first domain profile: `profile.json` (manifest: capabilities, permissions, checks, graph), `thresholds.json`, `capabilities.py` (manifest step implementations); features in `intake/` (`reader.py`), `analysis/` (`statistics.py`, `calibration.py`), `generation/` (`generate.py`, `plan.py`, `repair.py`), `validation/` (`checks.py`, `dataset.py`, `critique.py` — advisory text critique over the validation report plus profile statistics, bound to the `local_critic` role) |
| `profiles/hylogger/imaging/` | The HyLogger image extension (optional `--images DIR` branch after validate, before the human gate): `codec.py` (stdlib PNG encode/decode), `intake.py` (image hashing/attribution), `statistics.py` (code-only histograms/texture/strips), `generate.py` (seeded synthetic PNGs), `checks.py` (deterministic image checks), `critic.py` (advisory vision note over synthetic renders only) |
| `profiles/tabular/` | The second profile (common CSV proof): same root wiring (`profile.json`, `thresholds.json`, `capabilities.py`); features in `intake/` (`reader.py`), `analysis/` (`statistics.py`, `calibration.py`), `generation/` (`generate.py`, `plan.py`), `validation/` (`checks.py`, `dataset.py`) |
| `models/` | Model adapters — the only code that talks to a model (`base.py`, `meta_cli.py`, `glimmer.py` — local OpenAI-compatible text adapter; role bindings select it with `"adapter": "glimmer_local"`) |
| `catalogue/features/engine/` | Feature records for the persistence and reviewed-release slice: one JSON file per feature (stable id, owner, code/doc/test paths, required rules, status) |
| `docs/engine/core/` | Engine-core contracts (`README.md` states the run/attempt persistence contract) |
| `docs/engine/artifacts/` | Engine-artifacts contracts (`README.md` states the freeze/manifest/atomic/consumer release contract) |
| `tests/` | Mirrors the tree: `tests/engine/`, `tests/profiles/hylogger/`, `tests/profiles/tabular/` (one `test_<feature>.py` per feature module), gate tests at the root |
| `runs/` | Per-run durable history under `runs/<run-id>/` plus the content-addressed payload cache under `runs/<payload-sha>/` (generated at runtime, git-ignored — absent in a fresh clone) |
