# Quality registry

`rules.json` in this directory is the single source of rule metadata
for the Ruloom quality gates. `scripts/force_pass.py` loads it on every
run and prints one status line per rule; hand-copied rule lists
elsewhere are not authoritative.

## Entry shape

Each entry carries `id`, `scope`, `requirement`, `checker` (the script
or test path that enforces it), `positive_fixture` (proof the rule
passes on good input), `violation_fixture` (proof it fails on bad
input), `required_proof` (what a handoff must quote), `owner`, and
`status`.

The `layout` table classifies README Layout paths as `tracked` (must
exist in the tree) or `generated` (runtime output such as `runs/`:
must be git-ignored, never required to exist).

## Statuses

- `enforced` — the gate checks it today; a FAIL blocks merge.
- `open_finding` — an audit-confirmed defect that is NOT fixed. The
  entry names the audit finding (`audit_ref` R01-R12). Some open
  findings already have strict-xfail regression tests under
  `tests/audit/`; the rest name the later work package that will add
  them. Open never means accepted or waived.
- `planned` — a rule stub for work that does not exist yet.

## Aggregate execution

`scripts/force_pass.py` is the executable aggregate gate: after the
twelve FP checks it derives one outcome per `Q-` registry entry and
executes it. `enforced` entries run their checker -- pytest files run
as `python -m pytest -q <checker>` subprocesses, while an in-gate
`scripts/force_pass.py:<check>` checker first resolves to that check's
FP outcome from the same run when the check is part of the FP stage,
and otherwise executes the module-local `check_*` function live in
this run -- and any nonzero exit, missing checker file, unknown
member, raised error, or unimplemented checker fails the gate.
`open_finding` entries block unconditionally; `planned` entries are
reported but never executed. A missing or invalid registry fails
closed. Canary tests in `tests/test_aggregate_gate.py` pin the
behaviour: planted open findings, unimplemented or missing checkers,
unknown statuses, checker failures, and raising in-gate checks each
block, the module-local in-gate path is proven to execute live, and
the default runner is proven to really execute pytest against witness
files.

## The strict-xfail convention

Counterexample tests assert the corrected behaviour and are marked
`pytest.mark.xfail(strict=True)`. While the defect reproduces, the
test fails and xfail keeps the suite green with the failure honestly
recorded. When a fix lands, the test passes, strict XPASS turns red,
and the marker must be removed — the fix is not done until the marker
is gone and the registry status is updated.

## Current map

RP-00 enforced the 12 existing FP rules, recorded R01-R12 as open
findings with executable counterexamples for R01, R02, R03, R05, R06,
R07, R09 and R10, and fixed the clean-checkout defect R12
(`runs/` is generated, not required). R04, R08 and R11 are recorded
without RP-00 regression tests; their checkers arrive with RP-03/RP-06
(release packaging and image registration), RP-04 (rights and model
views) and RP-02 (durable runs) respectively, per the implementation
plan. Product behaviour is unchanged in RP-00: gates, tests, CI and
wording only.

RP-01 closed the engine side of R07 and R09. Capabilities now receive
frozen input views and validated write methods — no shared mutable
state, no runner reference, no verdict/artifact/decision path —
advisories land in a separate store the verdict never reads, and raw
source is gated behind `reads_raw_source`. Manifests declare
per-check `required`/`applicable_when` status; the verdict comes only
from results bound to the current attempt's exact artifact hash with a
recognised status, and only `pass` satisfies. `Q-ENGINE-R07` and
`Q-EVAL-R09-ENGINE` are enforced; `Q-EVAL-R09-HARNESS` stays
open for RP-08. One declared exception: HyLogger `swir_answer_rate`
is `required: false` with a manifest note because its calibrated band
is vacuous (informative bands are RP-05). R01, R02, R03, R05, R06, R10
remain open findings with strict-xfail counterexamples.

RP-02 closed R05, R10, and R11. Every execution gets a unique run id
and its own durable history under `runs/<run-id>/` (`run.json`,
`events.jsonl` plus the legacy-named `run.jsonl`, per-attempt
`plan.json`/`artifacts.json`/`checks.json`/`receipt.json`,
`decision.json`, packaged evidence, and `release.zip` only on accept);
identical output bytes only reuse the content-addressed payload cache
(`runs/<payload-sha>/data.csv`), never another run's plan, log,
decision, or release. Attempts are immutable records binding the
manifest digest, source hash, plan, seed, artifacts, checks, and
tokens, and packaging writes the selected attempt's own plan, seed,
and artifact together. The run record is created before intake with
write-through events, so a model failure after intake leaves a durable
failed record; manifest row limits are enforced against the plan before
generation; and each critic declares `required`/`optional`, so a
required critic's budget failure blocks the run while an optional
one's is recorded as `advisory-failed` without touching the verdict.
`Q-ENGINE-R05`, `Q-ENGINE-R10`, and `Q-ENGINE-R11` are enforced. The
governance and structure rules are registered as planned (not
enforced): `Q-GOV-*`, `Q-STRUCT-*`, `Q-DOC-CLAIM-EVIDENCE`,
`Q-GRAPH-AUTHORITY`, `Q-RELEASE-COMPLETE`. R06 stays open for RP-03
(the in-review mutation probe was made deterministic against the run's
own directory; no product change).

RP-04 closed R08 and the rights side of R04. Intake verifies the source
against its sibling `*-source.json` registry record: a recorded hash that
disagrees with the measured bytes fails loudly before any model call,
while a missing record leaves the source unverified without stopping the
run. Prompts (planner, repair, critic) consume only the disclosed
statistics view, which withholds text literals and group labels shared by
fewer than two source rows (a declared disclosure floor, not a quality
threshold); the seeded tabular generator draws from the same disclosed
vocabulary, so a single-row source cannot be recreated exactly. Output
terms resolve from the recorded rights evidence alone: the synthetic
output is released under the granted term exactly when the source hash
verified and the verified sibling record carries an explicit
`output_terms` grant -- a recorded source licence alone, permissive or
restrictive, never clears the output -- and every other release states
`NOT CLEARED` with a separate `rights` evidence block (field name plus
recorded grant value) in the release manifest.
`Q-DATA-R08` and `Q-RELEASE-R04-RIGHTS` are enforced; the section-9 rules
stay planned. The full suite, the gate, and the clean-archive check pass,
and the golden PDD446 bytes reproduce unchanged.

RP-05 closed R01 and R02. Twelve required checks judge the HyLogger
depth/missingness/label/joint-sequence slice, all source-relative or
structural so the frozen 38-hole calibration is untouched (no band
added, changed, or relaxed): `depth_finite` and `depth_nonnegative`
(StartDepth metres are never negative -- minimum 0.0001 m across the
43 measured holes); `swir_answer_fidelity`/`tir_answer_fidelity`
(answer-rate drift beyond the source hole's own split-half delta
plus the cross-hole p95 plus a 2/sqrt(n) finite-sample allowance, so
the audit's 99% SWIR removal fails while single-digit-row runs survive
sampling noise); `swir_source_vocabulary`/`tir_source_vocabulary`
(labels the source hole never answers) and
`swir_label_richness`/`tir_label_richness` (collapse to one label while
the source answers two or more); `invalid_label_rate` (labelling
invalid rows more often than the source hole does -- four real holes
do label some invalid rows, so the rule is relative, not zero);
`row_count_match`, `depth_start_match`, and
`depth_end_within_requested` (the artifact carries the plan's row
count, start, and extent, wired through the validate capability).
A split-half TVD tolerance at the 1.0 maximum is reported unsupported
(skip), never a fidelity pass -- measured real maximum is 0.895, so
the skip never fires on real sources. The vacuous `swir_answer_rate`
band stays required:false with its manifest note. Evidence:
`tests/audit/test_r01_answer_loss.py` and
`tests/audit/test_r02_depth_geometry.py` pass without xfail (each
corruption fails at the responsible check); `scripts/scorecard_rp05.py`
regenerates `docs/quality/scorecard-rp05.md` (15/15 multi-seed
synthetics pass across PDD446 plus the four held-out holes, with one
disclosed limitation: TIR persistence runs lower in synthetics because
joint-conditioned draws reset TIR runs -- the generator stays
byte-identical, so the golden PDD446 bytes reproduce unchanged).
`Q-DATA-R01` and `Q-DATA-R02` are enforced; R03 stays open for RP-06.

RP-03 closed R06 and the membership side of R04. When the run reaches
the human gate, the engine snapshots every release candidate into a
frozen store and records a candidate manifest (path, size, sha256,
producing attempt) whose digest the human decision binds; packaging
reads only the frozen snapshot plus the engine-serialised run log,
never mutable working files, so mutating or deleting working bytes
during review cannot change the released files. The release member
list comes from the profile's declared `release` block: the scalar
release keeps its members, and the synthetic image plus its
registration/check evidence ship exactly when the imaging branch
produced them for the run. The ZIP is built in a temporary file and
renamed into place (no partial release on failure, decision still
recorded), `manifest.json` and the detached `release.sha256` sit
beside it, and `engine/artifacts/consumer.py` re-verifies the bundle
from the recipient side. `Q-RELEASE-R06` and `Q-RELEASE-R04-MEMBERS`
are enforced; the rights/claims side of R04 stays open as
`Q-RELEASE-R04-RIGHTS` for RP-06, and the section-9 rules stay
planned. The full suite, the gate, and the clean-archive check pass,
and the golden PDD446 bytes reproduce unchanged.

RP-06 closed R03, schematic-only. One shared scene record (sample
id, depth interval, image id, pixel row band, orientation) drives
both the table view and the image view, and the image checks prove
registration in both directions: `image_strip_order` rejects
reversed and interleaved strips by ordered per-strip mean agreement
(literal 32.0 band; fitted synthetics drift by <1 unit while the
audit fixtures differ by 50+), and `image_scene_alignment`
validates the scene record itself (missing region, out-of-bounds,
duplicate/missing sample, wrong order/orientation, geometry
mismatch all fail) with the scene geometry bound to both the
reference and the candidate. The scene ships inside the existing
`image-statistics.json` member, so release membership is unchanged
and consumer verification still passes. Schematic-only: the bands
are the encoder's own strip layout with schematic depths, never
measured core-photo alignment; real NVCL core-photo registration is
unclaimed (`docs/quality/image-registration-rp06.md`).
`Q-DATA-R03` is enforced. The PDD446 golden bytes reproduce
unchanged.

RP-07 closed the GenerationSpec slice with bounded plan/repair and the
evidence report. Every run executes under one versioned GenerationSpec
(`engine/core/spec.py`: spec id/version, profile, source pin, requested
outputs, plan strategy, row/attempt limits, rights intent): the CLI
flags imply a default spec and `--spec PATH` overlays a spec file, the
normalised spec plus its digest ride the run record, the run log, and
every attempt receipt, and the manifests declare the implemented
strategies and outputs per profile (`generation` block: HyLogger
`fitted_markov_joint` with scalar+imaging outputs, tabular
`fitted_gaussian_disclosed` with scalar output). Unsupported requests
-- unknown profile or strategy, unknown output modality, unsupported
spec version, rows over the declared limit, imaging requested where
the profile declares no imaging output, or an attempt bound
contradicting the caller -- are refused during spec normalisation,
before any model call and before any generation, with a durable
terminal `refused` run record. A spec source pin, when present, is
verified at intake before anything downstream runs. The planner and
repair proposals must stay inside the spec-supported bounded schema
(optional `strategy` key, validated against the run's strategy set;
the repair prompt carries the failed measured properties plus only the
supported corrective options), and the seeded no-model path runs under
the same spec. One user-facing `evidence-report.md` per run is
rendered by the engine from stored records only (spec digest, source
pin, selected plan, check results with required/optional status,
human decision, frozen manifest digest, rights block, limitations
including schematic-only imaging and disclosed-view scope): it
recomputes nothing, ships in the run directory, and -- declared in
both profile release blocks -- as a release member rendered at pack
time. `Q-ENGINE-SPEC` and `Q-ENGINE-EVIDENCE` are enforced;
`Q-EVAL-R09-HARNESS` stays open for RP-08 and the section-9 rules
stay planned. The PDD446 golden bytes reproduce unchanged.

RP-08 closed the harness side of R09 and reran the held-out benchmark
once as `benchmark-v2-post-rp07`. `scripts/evaluate.py` now binds
every score to its own fresh invocation: run directories predating the
CLI call are counted as stale-ignored and never read, so a CLI failure
over old or missing artifacts records the arm `unscorable` with the
error instead of re-scoring stale bytes; empty check groups fail
coverage and an arm with zero executed required checks is unscorable
(coverage executed/required is recorded per arm); every baseline
invocation saves its raw bytes plus an invocation record (model,
invocation id, prompt/output digests, usage, error), and a failed
invocation is unscorable without reusing prior output; aggregates
recompute independently from the saved per-hole artifacts
(`--recompute` fails loudly on mismatch) with unscorable arms kept in
the `holes_total` denominator. The live rerun (code `f1ed40d`, spec
v1, both manifests v1, evidence git-ignored under
`runs/evaluation/20261101T180552Z-benchmark-v2/`, results in
`docs/benchmark-v2-post-rp07.md`, Step 14 tables untouched) scored 4/4
baseline arms with saved bytes (3 pass; HD012 fails fidelity on
`repeated_depths_masked_out`; 9075234 tokens, 2302.5 s wall) and 4/4
pipeline arms honestly unscorable: every live planner call fails with
API 400 because RP-07's `PLAN_SCHEMA` (optional `strategy`) is sent
verbatim as the model output schema, which the API rejects — a live-only
product regression fake-model suites cannot catch, left for an
RP-07-rework slice with no product file changed here. No
pipeline-vs-baseline comparison is claimed from this round.
`Q-EVAL-R09-HARNESS` is enforced (`tests/test_evaluate.py`); the
section-9 rules stay planned. The PDD446 golden bytes reproduce
unchanged.

RP-08 rework fixed the live-only planner 400 and reran the held-out
benchmark once as `benchmark-v3-post-rp08-rework`. Each profile gained
a strict wire schema (`provider_schema`: required covers every
declared property, `strategy` required when the spec bounds one) sent
on every live planner and repair call, while validation stays on the
still-optional `PLAN_SCHEMA`; regression tests pin the strict sent
shape on the propose and repair paths for both profiles. The live
rerun (code `1fac399` plus this fix, spec v1, both manifests v1,
evidence git-ignored under `runs/evaluation/20261101T190018Z/`,
results in `docs/benchmark-v3-post-rp08-rework.md`, Step 14 tables and
the v2 doc untouched) scored 4/4 pipeline arms pass (single attempt
each, all reproducible, 134806 tokens, 80.0 s wall) and 4/4 baseline
arms fresh with saved bytes (2 pass; 20LCDD003 and DDG007 fail
fidelity on `repeated_depths_masked_out`; 4633554 tokens, 1220.2 s
wall). No general pipeline-vs-baseline superiority is claimed: both
sides are single-run stochastic samples.

RP-09 closed dispatch and separate publication as local-only durable
machinery (no platform code imported). Dispatch
(`engine/core/dispatch.py`, rule `Q-ENGINE-DISPATCH`) holds one JSON
record per item under `<base>/dispatch/`: idempotent submit (same key
returns the first item; cross-authority key reuse refused),
single-winner lease claims with expiry (live leases conflict, expired
leases re-dispatch with the token moved on — the launcher-outage
path, also swept by `recover`), fencing tokens refusing every stale
lease-holder operation, owning-authority-only cancellation that
fences off in-flight workers, and bounded retries resting
`exhausted`. Publication (`engine/artifacts/publication.py`, rule
`Q-RELEASE-PUBLICATION`) ships only a `succeeded` item's reviewed
bytes into a separate area after consumer verification and digest
and authority checks, with `in_progress`/`complete` records so an
interrupted copy resumes to identical bytes, re-publication is a
no-op, and unattributed destination bytes refuse loudly; `download`
verifies at the destination and removes unverifiable bundles.
Consumer verification now refuses undecodable bytes as
`ReleaseVerificationError` instead of crashing. Evidence:
`tests/engine/test_dispatch.py` (15 passed),
`tests/engine/test_publication.py` (11 passed) plus the consumer
bit-flip regression, and an executed `/tmp` chain
submit → claim → tabular accept → publish → download whose downloaded
zip hash equals the reviewed `release.sha256`
(`f7040fc94e29e702544e2c18c302750bea5030562ef299bd4841dbe208ffc1bf`;
artifact
`fda9cf255638a5f3fb274b4d61971ff9a37702fc3f5042a145fb2fc3a4a318e4`),
recorded in `docs/dispatch-publication-rp09.md`. Full pytest 500
passed; `force_pass.py` 12/12 exit 0; golden bytes unchanged.

CP-02 repaired the remaining R02 depth/sequence defects (re-audit
A04) with a full repair, not a disclosure. Three required checks are
new, bringing the R01/R02 slice to fifteen: `spacing_step_match`
(median forward spacing matches the requested plan step within the
frozen calibration split-half p95, 0.000248 m -- a uniform 0.7x
contraction fails it alongside the calibration band it used to hide
inside), `depth_span_match` (end depth equals the requested span after
allowing exactly the artifact's own masked repeats, 1e-6 m slack --
contraction or unmasked extension fails), and
`tir_persistence_fidelity` (synthetic TIR persistence tracks the
source hole's own persistence within its split-half delta plus the
frozen cross-hole p95 of 0.155469 and a finite-sample allowance).
Both-answered rows now draw TIR through the TIR transition structure
instead of the joint counts, and transition rows with fewer than two
outgoing pairs fall back instead of absorbing the chain (PDD446
GARNET). Evidence: `tests/audit/test_r02_depth_geometry.py` gains the
0.7x-contraction and run-derangement corruptions (each fails only at
the responsible new check) plus uncorrupted controls;
`scripts/scorecard_cp02_addendum.py` regenerates
`docs/quality/scorecard-cp02-addendum.md` (15/15 multi-seed synthetics
pass across PDD446 plus the four held-out holes, synthetic TIR
persistence within ~0.01 of source on every cell, declared band
stated with its derivation). Joint SWIR/TIR agreement is lower than
under the joint draw and is disclosed, not gated; depth-dependent
interpretation needing joint co-occurrence stays unclaimed. The
generator bytes move deliberately: PDD446 seed-42 golden artifact
`be9e26d7e664…` becomes `55eb1c22e212…` (same profile/seed/n_rows,
verdict pass, reproduced identically by a live CLI run and by direct
computation). `Q-DATA-R02` is re-scoped to this contract; earlier
per-RP paragraphs above are historical.

CP-04 made the gate executable end to end (re-audit A07). Before,
`scripts/force_pass.py` printed each `Q-` registry entry as a status
line without running its checker, so `force_pass.py` exit 0 never
proved the Q checkers passed. Now the aggregate stage executes every
`enforced` checker (seventeen pytest files plus the FP-DOC-1-covered
`Q-DOC-R12`), blocks on any `open_finding`, and reports `planned`
stubs without executing them; the entry-point contract (one Q line
per rule, each carrying its status token) is unchanged. No rule
changes status and no bytes move: the registry, the suite, and the
golden artifact are untouched. Evidence: `tests/test_aggregate_gate.py`
(15 canary tests) and the gate's own per-checker execution lines.

CP-05 closed the governance section-9 slice. Six of the nine planned
rules now enforce with proven checkers, all executed live by the
aggregate stage (twenty pytest files plus four in-gate checks); three
stay planned with explicit rationale because no product mechanism can
observe the worker behaviour they govern. The product half of the
model-invocation boundary is enforced: the hosted adapter runs the
child with a pinned working directory, an allowlisted environment
(secrets never inherited), and an argv that names no peer-dispatch
capability, and every call's effective invocation (argv, pinned cwd,
inherited env key names, contributor flag, returned model identity) is
recorded to the run log (`models/meta_cli.py`,
`engine/core/runner.py`,
`tests/models/test_meta_cli_restrictions.py`).

DP-01 closed the release-binding slice (re-audit C01+C02).
`Q-ENGINE-DISPATCH` now validates the declared request at submit
(supported fields only, 64-hex digest pins, positive-integer bounds,
no conflicting row bounds) and persists the bound request identity
(profile, pinned source-content digest, pinned normalised spec
digest); unpinned components persist as None and can never match a
real run, so a request that pins nothing shippable refuses at
publication instead of shipping on profile-name equality alone.
`Q-RELEASE-PUBLICATION` now requires persisted == acceptance-bound
== delivered identities plus a declared row bound covering the run's
executed bound, and verifies/copies one captured immutable byte
snapshot (staged through a private temp file): the run file is never
re-read for shippable bytes, a post-acceptance change refuses at the
stability guard with the item never marked complete, and the
snapshot, destination, and persisted accepted digests must agree
before the complete mark. Evidence:
`tests/engine/test_dispatch.py` (40 tests: unknown fields, malformed
bounds/pins, conflicting `max_rows`, persisted identity) and
`tests/engine/test_publication.py` (27 tests: wrong-source,
tighter-row-bound, and changed-source refusals with nothing shipped;
post-acceptance ZIP swaps, corrupt and well-formed, refused with no
destination and no complete mark; round trips still publish). No
generation bytes move: the PDD446 seed-42 golden artifact
`55eb1c22e212…` is untouched (the acceptance block gains only the
request-identity binding, which no attested digest covers).

| Rule | Status | Checker or rationale |
| --- | --- | --- |
| `Q-STRUCT-FEATURE-MAP` | enforced | `scripts/force_pass.py:check_feature_map` -- the catalogue names every feature module (16 records, 37 modules), live in the gate |
| `Q-STRUCT-IMPORTS` | enforced | `scripts/force_pass.py:check_imports` -- the registry requirement through the `FP-CODE-2` scan, reconciled so they cannot drift, live in the gate |
| `Q-DOC-CLAIM-EVIDENCE` | enforced | `scripts/force_pass.py:check_claim_evidence` -- every enforced rule cites a resolvable checker plus proof and fixtures, README citations resolve, doc paths exist or are declared generated, live in the gate |
| `Q-GOV-SCOPED-WRITES` | enforced | `tests/governance/test_scoped_writes.py` -- no `data/` or `runs/` path tracked or ever committed, generated roots ignored, ordered phase tips ancestors of `HEAD` (ref-free, single-ref CI safe), no new merges |
| `Q-GRAPH-AUTHORITY` | enforced | `tests/engine/test_graph_authority.py` -- the engine walks only declared graphs; undeclared stages refused at load; contexts expose no dispatch or peer authority |
| `Q-RELEASE-COMPLETE` | enforced | `tests/audit/test_release_complete.py` -- both profiles release every declared member with frozen hashes rechecked on the extracted bundle; tampered or incomplete bundles refused |
| `Q-GOV-NO-PEER-DISPATCH` | planned | Runtime-owned: task assignment and review routing happen in the Datacore coordinator. The product half (restricted invocation above) is enforced and recorded; a product test refusing a worker peer-dispatch would fabricate Runtime authority |
| `Q-GOV-REVIEW-ISOLATION` | planned | Runtime-owned: verdicts arrive through the Runtime handoff and the product has no reviewer-identity channel, so a fake product checker would assert what the product cannot observe |
| `Q-GOV-CURRENT-LEASE` | planned | Runtime-owned: task leases live in the coordinator. The engine dispatch leases in `engine/core/dispatch.py` (`Q-ENGINE-DISPATCH`) are the analogous local mechanism, stated as analogy rather than conflated proof |

Evidence: `tests/test_aggregate_gate.py` (18 canary tests, including
module-local in-gate execution and fail-closed raising checks),
`tests/test_force_pass.py` (planted fixtures for all three in-gate
checks), and the gate's own in-gate execution lines. Catalogue
`F-ENGINE-RESTRICTED-INVOCATION`, `F-ENGINE-GOVERNANCE-CHECKS`, and
`F-ENGINE-TABULAR-QUALITY` close the feature map; the invocation
boundary is documented in `docs/engine/core/README.md`.
