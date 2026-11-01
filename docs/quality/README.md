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
