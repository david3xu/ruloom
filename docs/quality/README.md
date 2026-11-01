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
