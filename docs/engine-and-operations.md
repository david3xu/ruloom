# Engine and operations reference

This page holds the detailed design and operating notes that used to sit in the
README. Dated statements remain tied to the runs and revisions named in them;
see the [README](../README.md) for current scope and test status.

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
The structured vision extension and its separate scope are documented in
[`docs/vision-workflow.md`](vision-workflow.md). Vision opinions remain
advisory; unavailable calls are recorded explicitly.

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
llama-server -m ~/models/muse-glimmer/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf --port 8080 -c 32768
```

The `-c 32768` context is required, not optional: the live-measured
critique prompt on PDD446 is ~8.4k tokens, and the server must also fit
the completion plus reasoning budget (`models/glimmer.py`
`RECOMMENDED_CONTEXT_TOKENS`). A smaller context fails loudly with an
actionable error instead of skipping the critique.

then run with the default adapter base URL `http://127.0.0.1:8080`
(override per role with the `base_url` binding, or per client with
`GlimmerLocalModel(base_url=...)`). The image weights (`mmproj`) for
Glimmer are not present, so Glimmer serves the text critique only; the
Spark multimodal adapter stays the vision critic for synthetic renders.

Live runs (2026-11-02): one Glimmer text-critic run through
`ruloom run --local-critic` on PDD446 (verdict pass, advisory note
recorded) and live vision-critic calls on a synthetic-only PNG. No
claim beyond these runs; see `docs/quality/critic-receipts-gp01.md`
for both receipts with usage.


## Model roles, budget, and overrides

Each profile manifest binds named roles to models under `models.roles`
(`planner`, `critic`, `local_critic` for HyLogger; `planner` for the
tabular profile). A role binding names its `adapter` (`meta_cli` for
the hosted `muse exec` adapter, `agy_cli` for AGY model families,
`glimmer_local` for the local OpenAI-compatible server) plus the model id, reasoning effort, and —
for local roles — the base URL.

AGY family selection is explicit and never falls back to Muse:

```sh
ruloom run SOURCE.csv --profile tabular --max-attempts 2 \
  --role-adapter planner=agy_cli --role-family planner=claude \
  --role-model planner=claude-opus-5-5-high --role-effort planner=high
```

The same role settings work on `prepare` with `--model-policy hosted`.
Family defaults cover Claude, Gemini and GPT aliases; check `agy models`
for availability on your account. `meta_cli` selects the Meta family via
Muse. See [model workflow](model-families.md) for configuration,
identity evidence and the limits of AGY tool permissions.

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
`--role-model ROLE=MODEL` (plus `--role-adapter`, `--role-effort` and `--role-base-url`)
beat `RULOOM_ROLE_<ROLE>_MODEL` / `_ADAPTER` / `_EFFORT` / `_BASE_URL` (role
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

Every run executes under one versioned GenerationSpec (spec id and
version, profile, source pin, requested outputs, plan strategy,
row/attempt limits, rights intent), recorded with its digest in the
run record and every attempt receipt. The flags above already imply
the default spec; `--spec spec.json` overlays a spec file on those
defaults, and unsupported requests are refused before any model call
with a durable terminal record. A saved `plan.json` replays with
`--plan plan.json` (no planner call): the same plan plus the same seed
yields identical `data.csv` bytes. Compare data digests across replays,
never release ZIPs (per-run identity, so they differ by design).
The run also ships a user-facing
`evidence-report.md` rendered from stored records only.


## Work without hosted credits

Replay a reviewed plan with **all model calls disabled**, including repair:

```bash
ruloom run --profile tabular SOURCE.csv --max-attempts 2 \
  --plan saved/plan.json --model-policy offline --out replay
```

Checks and the human gate still apply. A failing check cannot trigger paid
repair or become a pass. Optional critics blocked by the policy are recorded
as unavailable; a required model stage fails. Plain `--plan` alone still
permits later model calls under the default `hosted` policy.

For local text planning, start your trusted Glimmer server separately, then:

```bash
ruloom run --profile tabular SOURCE.csv --max-attempts 2 \
  --model-policy local-only --role-adapter planner=glimmer_local --out local-run
```

This uses a literal loopback endpoint and has no hosted fallback. Proxy
settings and redirects are refused by this transport. The policy controls
Ruloom's adapters, not what a separately operated server does internally.
The local adapter is text only; it does not replace the vision critic.

To draft settings from a short request before generation:

```bash
ruloom prepare --profile tabular SOURCE.csv --max-attempts 2 \
  --request "Create 100 synthetic rows using the supported source-fitted strategy." \
  --role-adapter planner=glimmer_local --out request-draft
```

`prepare` defaults to `local-only`. It sends the request and supported settings
to the model, and hashes source bytes locally to pin the draft. Source rows
are never sent by the preparation command. Review `request-draft/request.json`,
`spec.json` and `plan.json` for a faithful interpretation, then run the reviewed
spec and plan with `run --spec ... --plan ... --model-policy offline`. Draft
success is not data-quality approval. Unsupported requirements refuse the
draft. This first request interface supports scalar output; existing image
workflows remain available through `run` with explicit source inputs.

The evidence report shows the exact row target when supplied, planned rows,
stored check results, initial plan origin, actual successful model calls and
unavailable reviews. It does not manufacture a measured row count from the
plan. Development attribution and deferred real evaluation are documented in
[the product workflow](credit-free-product.md).

For the complete user journey, model roles, supported scope and later
comparison design, read [Request-to-data workflow](product-workflow.md).
For the implemented bounded scalar teaching case, use the
[operator guide](research-workflow-user-guide.md) and
[installed-wheel acceptance instructions](full-workflow-acceptance.md).
The [combined scalar validation record](quality/scalar-workflow-2026-11-05.md)
describes the reviewed revision, engineering controls and remaining limits.

```bash
.venv/bin/ruloom run --profile tabular <source>.csv --max-attempts 2 \
  --spec docs/spec-example.json
```

