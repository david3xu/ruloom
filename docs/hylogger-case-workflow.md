# HyLogger Case Workflow Specification and Profile Adapter

## Overview and Purpose

This document specifies the profile-owned case workflow adapter implemented in
[profiles/hylogger/validation/workflow.py](../profiles/hylogger/validation/workflow.py).

> [!NOTE]
> This adapter is profile-level workflow helper code implementing case selection,
> practical-use checking, candidate evaluation, release packaging, consumer verification,
> and advisory AI roles. It is maintained strictly on the profile side pending independent
> engine integration and review.

The HyLogger case workflow connects user requests for teaching drill-core inspection
to:
1. reviewed primary-source evidence in the installed cases catalogue;
2. deterministic local source suitability pre-flight;
3. strict canonical contracts with integer row targets and double-read source hash guards;
4. downstream practical-use checking across 13 namespaced check measurements;
5. causal same-seed, same-target plan repair;
6. self-attested integrity digest packaging of frozen release members;
7. recipient consumer verification without requiring raw source data cache;
8. bounded runtime AI advisory roles (case researcher, source assessor, candidate reviewer)
   restricted to StructuredModel completion protocols without authority to waive code checks,
   change seeds, alter contracts, or accept data.

## Rationale and Limitations

### Real Practice and Source Grounding
In core scanning workflows, such as those described by the National Virtual Core Library
(NVCL), data quality and integrity checking are established operational activities.
The scalar teaching case `hylogger_scalar_teaching_v1` models inspection of depth-linked
validity masks and classification labels.

### Strict Non-Claims and Boundaries
- **No scientific or predictive claims**: Synthetic practice tables generated under this
  workflow are teaching artifacts only. They do not predict geology, mineral reserves,
  or physical rock properties.
- **No photographic or spectral equivalence**: Output scalar classification labels are
  discrete interpretation tags; they are not continuous reflectance spectra or raw TSG
  spectral curves. Drill-core photographs are not generated or certified.
- **No privacy or licensing guarantees**: Access to a source package or registry record
  does not grant universal republication permission. Licence status is verified per source
  metadata and bounded in disclosed summaries; byte comparison of source files does not prove
  licence rights.
- **Self-attested integrity digests, not cryptographic provenance**: Run bindings
  (including `source_fit_sha256`, `catalogue_sha256`, `contract_sha256`, `reference_sha256`,
  `thresholds_sha256`, and `checker_sha256`) are self-attested SHA-256 digests computed
  during execution. These integrity pins guarantee internal consistency across run artifacts;
  they do not prove external cryptographic origin, authorship, or third-party licensing rights.
- **Structured measured references and phrase filtering limitations**: `evidence_facts`
  requires structured object references (`{"measurement": <path>, "value": <exact number>}`)
  strictly cross-checked against recomputed measurement numbers with exact JSON scalar types;
  no model-supplied invented numeric prose or regex-based heuristic matching is accepted.
  Free-form opinion fields (`evaluator_opinion`, `research_summary`, `fit_assessment`) are
  screened against a curated phrase blacklist (`PROHIBITED_PROSE_PATTERNS`), but phrase-based
  screening is inherently bounded and cannot provide general natural-language or legal safety proofs.
- **No AI acceptance authority**: Runtime AI roles provide structured operator advisory
  information only. The human operator at the release gate retains exclusive authority to
  accept or reject candidate artifacts.

## Public API Contract

The module [profiles/hylogger/validation/workflow.py](../profiles/hylogger/validation/workflow.py)
exports the following pure profile-owned functions and constants.

### 1. `prepare_case_context(source_path, spec, licence_meta=None, thresholds_path=None) -> dict`
Prepares immutable, code-side run context for case-selected execution.
- **Inputs**:
  - `source_path`: Path to local source CSV.
  - `spec`: Generation spec mapping requiring `case: {id: str, version: int}`,
    `target_rows: int`, `source: {sha256: str}`, and `outputs: ["scalar"]`.
  - `licence_meta`: Optional licence disclosure string.
  - `thresholds_path`: Optional explicit thresholds file path for testing.
- **Invariants Enforced**:
  - Refuses execution if `RULOOM_THRESHOLDS` environment variable is set (calibrated
    thresholds cannot be overridden for selected cases).
  - Verifies installed catalogue pin against `REVIEWED_CASES_CATALOGUE_SHA256`.
  - Verifies requested case ID and version against catalogue (no aliases or type coercion).
  - Verifies local source file existence and matches `spec["source"]["sha256"]` under
    a double-read guard.
  - Executes deterministic suitability checks via `assess_source_suitability`.
  - Computes bounded aggregate reference via `build_source_reference`.
  - Checks plan satisfiability and joint feasibility.
  - Compiles canonical contract via `compile_case_contract`.
  - Binds context-only canonical digest of the full receipt (`source_fit_sha256`) and hashes
    installed threshold bytes, checker (`cases.py`) bytes, and evidence bytes into binding.
- **Returns**: Deep copy dictionary with keys `binding`, `contract`, `reference`,
  `admitted_evidence`, `source_fit_receipt`, and `disclosed_model_summary`.
  For caller compatibility, convenience aliases `evidence` (aliasing `admitted_evidence`),
  `source_fit` (aliasing `source_fit_receipt`), `source_fit_receipt_sha256` (canonical receipt digest),
  and `case` (aliasing the catalogue case definition) are also included.

### 2. `evaluate_case_context(candidate_bytes, context) -> dict`
Evaluates candidate CSV bytes under a strictly validated case context.
- **Inputs**: `candidate_bytes` (bytes, str, or Path) and `context` mapping.
- **Invariants**: Validates that binding hashes match contract, reference, source, thresholds,
  checker, and catalogue pins before evaluation.
- **Returns**: Complete utility evaluation result dictionary from `evaluate_case_utility`.

### 3. `case_check_results(utility_report) -> list[dict]`
Translates an already computed utility report into engine-compatible check outcome dictionaries.
- **Mapping Role and Authority**:
  `case_check_results` is a check result mapping adapter for an ALREADY computed evaluator report.
  It trusts the evaluator's semantic outcomes (recorded in `failures`); actual semantic, target,
  and contract authority resides in `evaluate_case_context` and `verify_case_candidate` under the
  canonical case contract. Mapping alone does not and cannot re-evaluate or prove unprovided target
  row counts or ordered CSV structure.
- **Invariants**:
  - Emits all 13 namespaced `case.*` check IDs every time, in fixed order.
  - Validates top-level report schema (`passed` boolean, 64-char lowercase hex `candidate_sha256`,
    non-empty `limitations` string list, and string list `failures` with defensive handling of unhashable items).
  - Validates exact keysets and exact JSON scalar types (no bool-as-int, no non-finite numbers,
    no scalar-for-object) for all 13 measurement objects.
  - Early-return, unmeasured, or malformed checks fail closed; any malformed report or measurement
    shape emits all 13 checks failing with diagnostic details and never raises an unhandled exception (e.g. `TypeError` or `KeyError`).
  - Strict typing: `status` is literal `"pass"` or `"fail"` (no numeric or boolean aliases).
  - `kind` is `"constraint"`, `threshold_source` is `"case_contract"`.
- **Exported Constant**:
  `CASE_CHECK_IDS` defines the 13 check names:
  1. `case.source_pin_match`
  2. `case.output_columns`
  3. `case.row_count`
  4. `case.well_formed_rows`
  5. `case.ordered_depth`
  6. `case.depth_spacing`
  7. `case.inclusion_fraction`
  8. `case.swir_answer_rate`
  9. `case.tir_answer_rate`
  10. `case.swir_supported_labels`
  11. `case.tir_supported_labels`
  12. `case.downstream_filter_group`
  13. `case.downstream_structure_both_states_present`

### 4. `build_case_members(candidate_bytes, context, utility_report, spec_digest) -> dict[str, bytes]`
Packages frozen case artifacts for release.
- **Members**:
  - `case-binding.json`: Self-attested integrity digests for case, catalogue, contract, reference,
    source, thresholds, checker, evidence, spec_digest, and candidate bytes.
  - `case-contract.json`: Full canonical compiled contract.
  - `case-reference.json`: Bounded aggregate source reference.
  - `case-utility.json`: Complete evaluated utility report for the candidate.
  - `case-evidence.json`: Admitted primary evidence pack entries.
- **Invariants**:
  - Requires `spec_digest` to be a 64-character lowercase hex string.
  - Verifies full utility report via `verify_case_candidate` (rejecting tampered reports or failing candidates).
  - Re-validates contract and reference integrity against case definition.
  - Member names are flat and safe (no path traversal).

### 5. `verify_case_members(members, expected_case=None, source_path=None, expected_spec_digest=None) -> dict`
Independent recipient consumer verification for released case archives.
- **Inputs**:
  - `members`: Dictionary mapping member names to bytes.
  - `expected_case`: Optional expected `{id, version}` mapping from recipient (strictly `{id, version}`).
  - `source_path`: Optional local source CSV path for raw derivation proof.
  - `expected_spec_digest`: Optional 64-character hex spec digest for external anchor verification.
- **Invariants**:
  - **Strict subset contract**: Expects strictly the 5 case JSON files (`case-binding.json`,
    `case-contract.json`, `case-reference.json`, `case-utility.json`, `case-evidence.json`) and `data.csv`.
    Any extra member, missing member, or non-matching filename causes verification to fail closed.
    Engine-level packaging wrappers filtering generic files must preserve this exact profile subset.
  - Recompiles canonical contract from installed catalogue and checks against shipped contract.
  - Verifies binding digests against installed thresholds and checker files.
  - Verifies `case-evidence.json` against installed evidence pack.
  - Recomputes practical checks independently via `verify_case_candidate`.
  - Distinguishes locally proven source derivation from aggregate consistency checking:
    without `source_path`, reports `source_derivation: "unproven (aggregate consistency only)"`;
    with `source_path`, verifies structural source facts and notes licence rights unproven by bytes alone.

### 6. `repair_case_plan(plan, failed_ids, context) -> dict`
Causal, same-seed, same-target plan repair between attempts.
- **Invariants**:
  - Validates case context strictly.
  - Validates `target_rows` and `n_rows` strictly (refusing conflicting, non-positive, or falsy target values).
  - Validates strategy against known causal strategies (`fitted_markov_joint`, `fitted_markov_balanced`).
  - Initial seed and exact row target remain strictly immutable; lucky-seed search is prohibited.
  - When `case.inclusion_fraction` fails under `fitted_markov_joint`, causal adjustment
    moves strategy to `fitted_markov_balanced`.
  - When no supported causal adjustment exists, returns plan unchanged and allows the engine
    attempt loop to terminate.

## Runtime Advisory AI Roles

Three structured AI roles operate via injected `StructuredModel` protocols.
All three roles enforce strict JSON Schemas with exact scalar integer, number, and string types.
Free-form prose outputs are screened against `PROHIBITED_PROSE_PATTERNS`, and candidate review
`evidence_facts` are strictly cross-checked against recomputed measurement numbers.
Phrase screening is a heuristic boundary and does not guarantee formal semantic completeness;
model advisories remain non-authoritative operator recommendations without release or waiver authority.

### 7. `research_case(model, purpose, context) -> dict`
- **Role**: Case Research Advisor.
- **Scope**: Explains and cites only admitted primary evidence IDs from the installed
  catalogue. Identifies purpose requirements outside the case scope (such as image generation
  or geological prediction) and records them in `unsupported_requirements`. Negated requirements
  are distinguished to avoid false refusals.
- **Boundaries**: Cannot invent citations, alter thresholds, or admit new sources.
  Screens `research_summary` and `limitations` against prohibited prose patterns. Returns JSON-native payload.

### 8. `assess_case_source(model, context) -> dict`
- **Role**: Source Assessment Advisor.
- **Scope**: Explains source fit and limitations using disclosed aggregate summaries rebuilt
  from verified reference.
- **Boundaries**: Raw source rows, literal label vocabularies, and free-form licence text
  are withheld. Deterministic code owns source suitability; the model cannot waive checks.
  Screens `fit_assessment` against prohibited prose patterns. Returns JSON-native payload.

### 9. `review_case_candidate(model, candidate_bytes, context, utility_report) -> dict`
- **Role**: Candidate Review Advisor.
- **Scope**: Provides structured operator review recommendations using measured check reports.
  Sanitizes candidate-controlled labels into numeric counts to prevent prompt injection.
- **Boundaries**: Code recomputes checks independently. A model cannot claim a passing verdict
  on failing data, change seeds, alter checks, or bypass the human release gate.
  Requires `evidence_facts` to be a non-empty list of structured objects
  `{"measurement": <path>, "value": <exact number>}` matching recomputed measurement numbers
  with exact JSON scalar types (rejecting boolean aliases, float aliases for integer paths,
  unsupported paths, and duplicate paths), raising clean `ValueError` if a path is absent from
  early-fail reports. Screens `evaluator_opinion` against prohibited
  prose patterns. Returns JSON-native payload.

## Recheck Commands

Run targeted profile test suite:
```sh
.venv/bin/python -m pytest tests/profiles/hylogger/test_workflow.py -v
```

Run code hygiene checks on owned files:
```sh
.venv/bin/ruff check profiles/hylogger/validation/workflow.py tests/profiles/hylogger/test_workflow.py
.venv/bin/ruff format --check profiles/hylogger/validation/workflow.py tests/profiles/hylogger/test_workflow.py
```
