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
returns the first item only when the canonical request identity and
the enforced-fields binding digest also match the key-to-request
binding persisted at first submission; the same key with a different
request, a changed row/attempt bound, or a changed spec overlay is
refused; cross-authority key reuse is refused), an unkeyed submit
matching another owner's item is refused so a keyless resubmission
can never return a foreign job, and an explicit item id is
create-only: resubmitting it returns the existing record unchanged
only under the same authority and request, while a changed request
or a foreign authority is refused -- history is never rewritten and
the fencing epoch never resets. Submit validates the
declared request (supported fields only: profile, source,
source-content and spec digest pins, spec overlay, row/attempt
bounds; unknown fields, malformed pins/bounds, and conflicting
row/attempt bounds refuse) and persists the bound request identity (profile,
pinned source-content digest, pinned normalised spec digest) that
publication later matches against the run, plus the canonical digest
of the enforced request fields (identity with caller row/attempt
bounds and the spec overlay) that same-key retries must repeat. Item ids take a narrow
canonical shape at every entry point (letters, digits, dash,
underscore; at most 64 characters), so no id can address a record
outside the store's own items directory. Claims lease an item to one
worker with a monotonic fencing token and an expiry (a live lease
blocks rivals; an expired lease is re-dispatchable, which is the
launcher-outage path), and every lease-holder operation must present
the current token, so stale workers are fenced off after cancel,
supersession, or expiry. Cancellation and publication require the
submitting authority; each reported failure and each expired lease
(via the expired-claim reclaim or the recovery sweep) consumes one
retry from `max_retries`, while the launch count persists separately
in `attempts`. Until the budget is spent the item returns to the
queue; then it rests `exhausted` (expiry exhaustion records
`retries_exhausted_by_expiry`) and is never claimed again, so
repeated crashes cannot trigger unbounded generation cost. Within
one launcher process
every claim runs under a per-store lock (exactly one threaded
winner); every record write is atomic, so a crash leaves the previous
record intact.

## Restricted model invocation

Planner and critic calls reach the hosted model through one restricted
path (`models/meta_cli.py`, composed in `engine/cli/__init__.py`). The
word "hosted" names the `muse exec` subprocess the CLI spawns, not a
Ruloom service: the product is a local CLI plus libraries and runs no
server of its own. The
child process gets a pinned working directory (the explicit
`--workspace` root when set, otherwise the invoking directory, which
is the repo checkout under normal CLI use), a minimal allowlisted
environment (`PATH`, `HOME`, locale, timezone, and temp-dir keys only
-- secrets, every `RULOOM_*` setting, and any MCP/tool-routing config
are stripped before the child starts), and exactly the `muse exec`
argv the adapter builds, which names no MCP servers, tool dispatch, or
peer routing; a spawn gate (`enforce_spawn_policy`) refuses any
MCP/tool/dispatch/peer/delegate flag in an option slot before
anything spawns, while flag-like text in a known option's value slot
and in the prompt slot (the final argv element) passes. Every
call records its effective invocation -- argv, pinned cwd, inherited
env key names (never values), contributor flag, the requested model,
and the observed model the transport reported -- to the adapter's
`last_invocation` record and to a per-call `model_invocation` event in
the run log (`engine/core/runner.py`), so the run record shows which
restricted invocation acted. Requested vs observed stay separate:
`Usage.model` and completion `meta["model"]` carry what the transport
reported (`unknown` when it reported no usable name, a `multiple:...`
aggregate when it reported several, with the normalised per-model
entries kept in `meta["models"]` and summed into the usage totals),
while `meta["requested_model"]` and the invocation record keep what
was asked. The same split covers permissions: `requested_permissions`
names the policy asked for (cwd pin, no tools, no peers, allowlist
environment) while `observed_permissions` names the effective
permissions the spawn provided. A mismatch never refuses; both sides
are recorded, and the local Glimmer adapter follows the same
unknown-when-silent convention. Secret values never enter the record.
The default pins preserve the validated planner path unchanged;
callers needing tighter confinement pass an explicit allowed root.

The pinned directory is NOT filesystem confinement: a child naming an
absolute host path outside the root can still reach it, and the CLI's
own runtime tool behaviour is outside product control. What is proven
is environment filtering, a pinned cwd, and a tool-free spawn -- not
actual tool restriction -- so C04 stays open for genuinely
restricted, tool-free containment (rule `Q-GOV-TOOL-CONTAINMENT`,
planned).

Denial is proven child-side, not only asserted on the pre-exec dict
(`tests/models/test_child_capability.py`, rule
`Q-GOV-CHILD-CAPABILITY`): a real fixture child spawned through the
real adapter path -- restricted environment, pinned cwd, gated spawn --
reports no probe secret, no `RULOOM_*` key, and no MCP/tool-routing
config in its own environment, including a grandchild peer the fixture
child dispatches to, with a positive control proving non-secret
allowlisted keys still pass through; tool/dispatch flags in option
slots are refused before anything spawns (on fixture and real
`muse exec` argv shapes, with positive controls proving flag-like
wording in free text -- including a literal prompt beginning
`--tool` and flag-like option values -- never trips the gate); and
relative writes land inside the pinned root. One
honest platform allowance: the live child may additionally report
interpreter/platform-injected locale keys (`LC_CTYPE`, exported by the
Python runtime under locale coercion, and `__CF_USER_TEXT_ENCODING`,
injected by macOS CoreFoundation) that the adapter never sent; the
probes name them explicitly and audit their values for secrets instead
of pretending the child sees exactly the pre-exec dict. The fixture
child is the local Python interpreter, never `muse exec`, so no live
model call is made; probes run on ubuntu-latest CI and local macOS,
and no Windows run is claimed.

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
- `F-ENGINE-RESTRICTED-INVOCATION` (CP-05, rules
  `Q-GOV-NO-PEER-DISPATCH` and `Q-GOV-CHILD-CAPABILITY`): the
  restricted invocation above with its recorded requested vs observed
  permissions (HP-05); the unproven remainder is planned as
  `Q-GOV-TOOL-CONTAINMENT`.
- `F-ENGINE-GOVERNANCE-CHECKS` (CP-05, rules `Q-STRUCT-FEATURE-MAP`,
  `Q-STRUCT-IMPORTS`, `Q-DOC-CLAIM-EVIDENCE`, `Q-GOV-SCOPED-WRITES`,
  `Q-GRAPH-AUTHORITY`, `Q-RELEASE-COMPLETE`).
- `F-ENGINE-TABULAR-QUALITY` (CP-05, rule `Q-EVAL-R09-ENGINE`): backfill
  map for the tabular deterministic stages.

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
A saved plan replays deterministically with `--plan PATH`: the file
must decode to a plan object, is validated against the same
spec-supported bounds as a fresh proposal, and is stored with no
planner call, so the same plan plus the same seed yields identical
`data.csv` bytes. Only the initial plan is fixed; repair after a
failed replay still uses the planner. Compare data digests across
replays, never release ZIPs: the ZIP carries per-run identity and log
bytes, so it differs per run by design.
One user-facing `evidence-report.md` per run is rendered from stored
records only and recomputed never; it ships in the run directory and,
when the profile release block declares it, as an `engine-report`
release member rendered at pack time.
