# Engine core: run and attempt persistence

Every execution gets a unique run id and its own durable history under
`runs/<run-id>/`. This directory is the authoritative record of the run;
no execution ever overwrites another run's plan, log, decision, or
release reference. Identical output bytes may reuse the
content-addressed payload cache (`runs/<payload-sha>/data.csv`, plus
`synthetic.png` when the run renders one), which carries payload bytes
only -- never plans, logs, decisions, or releases.

## Run directory layout

- `run.json` -- the run record: identity (`Run`), terminal status
  (`released`, `declined`, `invalid`, `failed`, `error`), the error
  when the run fails, the attempt count, the selected artifact hash,
  the human decision, and timestamps.
- `events.jsonl` -- the hash-chained event export, one event per line,
  persisted write-through as each transition occurs. `run.jsonl`
  carries the same bytes under the legacy name existing readers use.
- `attempts/attempt-<n>/` -- one directory per loop attempt:
  - `plan.json` -- the complete resolved plan of that attempt.
  - `artifacts.json` -- the artifact manifest (hash, media type, name,
    producing capability) bound to that attempt's plan.
  - `checks.json` -- the check results measured over those artifacts.
  - `receipt.json` -- the attempt verdict, the failed check ids, the
    seed, the model tokens spent inside the attempt, the manifest
    digest, and the source hash.
- `decision.json` -- the human gate decision, recorded verbatim.
- Packaged evidence for the selected attempt (`data.csv`, `plan.json`,
  `validation-report.json`, `seed.txt`, `provenance.md`, plus
  `synthetic.png`, `image-statistics.json`, `image-validation.json`,
  and `critic-note.json` when the run produces them).
- `release.zip` plus `checksums.sha256` -- only when the human accepts.
  A rejected, failed, or error run leaves no release reference.

## Attempt records

An attempt is immutable once the engine records it: it binds the
manifest digest the run executed under, the source hash measured at
intake, the complete resolved plan (including its seed), the artifact
hashes produced from that plan, the check results measured over those
artifacts, and the model tokens spent inside the attempt. Selecting a
best attempt selects the whole record; packaging writes the selected
attempt's own `plan.json` and seed, never the last attempt's plan.

## Durability

The run directory is created before intake runs, and every event is
appended write-through as it occurs -- including the terminal error.
A model failure after intake therefore leaves a durable failed run
record with the error, not just an in-memory run id.

## Bounds before work

Row, pixel, and byte limits come from the manifest (`limits`), and the
engine validates the resolved plan against them before any generation
or check runs: `store_plan` refuses an over-limit plan at plan time,
and the loop re-validates before each attempt body. Plan validation
also accepts the profile-declared maximum (`max_rows`), so an oversize
plan is refused loudly with a durable failed record instead of burning
generation work. No default limit is invented: a manifest without a
bound runs unbounded, exactly as declared.

## Critic failure policy

Each advisory critic is declared `required` or `optional` in the
manifest (`critic_policy`). A required critic's failure -- including
budget exhaustion -- blocks the run loudly with a durable failed
record. An optional critic's failure is recorded visibly as an
`advisory-failed` entry (with a `budget_exhausted` flag when the run
token ceiling caused it) and the run continues on its deterministic
evidence; the verdict never reads advisories either way. Budget
exhaustion is never swallowed by a broad handler: the engine checks
the budget before every model call, and the critic policy decides what
a refusal means.

## Durable dispatch

`dispatch.py` coordinates *which* work runs and *who owns it*, in a
local durable store beside the per-run history (`<base>/dispatch/`,
one JSON record per item). Submits are idempotent (the same key
returns the first item; cross-authority key reuse is refused), claims
lease an item to one worker with a monotonic fencing token and an
expiry (a live lease blocks rivals; an expired lease is
re-dispatchable, which is the launcher-outage path), and every
lease-holder operation must present the current token, so stale
workers are fenced off after cancel or supersession. Cancellation and
publication require the submitting authority; failures return the
item to the queue until `max_retries` is spent, then it rests
`exhausted`. Within one launcher process every claim runs under a
per-store lock (exactly one threaded winner); every record write is
atomic, so a crash leaves the previous record intact.

## Feature records

The persistence features implemented here are registered under
`catalogue/features/engine/` with stable ids, owners, code, doc, and
test paths, and the quality rules they satisfy:

- `F-ENGINE-RUN-PERSISTENCE` (audit R05, rule `Q-ENGINE-R05`).
- `F-ENGINE-ATTEMPT-RECORDS` (audit R10, rule `Q-ENGINE-R10`).
- `F-ENGINE-DURABLE-LOG` (audit R11, rule `Q-ENGINE-R11`).
- `F-ENGINE-PLAN-BOUNDS` (audit R11, rule `Q-ENGINE-R11`).
- `F-ENGINE-BUDGET-POLICY` (audit R11, rule `Q-ENGINE-R11`).
- `F-ENGINE-GENERATION-SPEC` (RP-07, rules `Q-ENGINE-SPEC`,
  `Q-ENGINE-EVIDENCE`).
- `F-ENGINE-DURABLE-DISPATCH` (RP-09, rule `Q-ENGINE-DISPATCH`).

## GenerationSpec, bounded plans, and the evidence report

Every run executes under one versioned GenerationSpec
(`spec.py`, `report.py`): the spec names the profile, the source pin,
the requested outputs, the plan strategy, the row/attempt limits, and
the rights intent, and its canonical digest rides the run record
(`run.json`), the run log (`spec` event), and every attempt receipt
(`spec_digest`). The CLI flags imply a default spec (scalar output
always; the render branch exactly when `--images DIR` is given; the
manifest's default strategy; the manifest row bound); `--spec PATH`
overlays a spec file on those defaults. Unsupported requests -- an
unknown profile or strategy, an unknown output modality, an
unsupported spec version, rows over the manifest limit, an output the
profile never declares, a source pin pointing elsewhere, or an
attempt bound contradicting the caller's `--max-attempts` -- are
refused during normalisation, before any model call and before any
generation, with a durable terminal `refused` run record
(`runstore.record_refusal`). The plan schema carries an optional
`strategy` key validated against the run's strategy set, so the
planner and repair can only propose values inside the spec-supported
bounds; a plan omitting the key runs under the run's own strategy.
One user-facing `evidence-report.md` per run is rendered from stored
records only and recomputed never; it ships in the run directory and,
when the profile release block declares it, as an `engine-report`
release member rendered at pack time.
