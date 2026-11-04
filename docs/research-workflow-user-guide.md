# HyLogger Research-Led Workflow User Guide

> [!IMPORTANT]
> **Scope: bounded scalar teaching workflow.**
> The commands below describe the implemented bounded HyLogger scalar teaching route. Use the release PR's exact-revision independent review and pinned strict acceptance receipt to assess an installed revision; these instructions alone are not an acceptance record. Engineering controls use fixed plans and mock role responses; they do not establish the frozen Gemini-versus-Claude comparison, a scientific result or human approval.

---

## 1. Overview and Scope Boundaries

Ruloom connects plain-language requests to recorded survey practice, a suitable real drillhole source, an explicit contract, seeded generation, practical utility checks, and human review.

### Supported Scope: Bounded Scalar Teaching Case

- **Activity**: Generates synthetic drillhole tables to teach inspection of depth-linked inclusion flags and missing classification-label patterns.
- **Source Input**: Continuous scalar drillhole CSV (such as `HD012-scalars.csv`) with accompanying rights manifest (`HD012-source.json`). Source files can reside anywhere on disk; no repo-internal directory is required.
- **Installed Case ID**: `hylogger_scalar_teaching_v1@1` from `profiles/hylogger/cases.json` (canonical digest `26b083a521447b4fa4bfce22c812da9ba9d6d5aa19a6d381e06a55fd87ac6ab3`).
- **Fixed Output Columns**: Exactly four columns: `depth_m`, `valid`, `swir_group`, `tir_group`.
- **Primary Evidence**: AuScope NVCL ([https://www.auscope.org.au/nvcl/](https://www.auscope.org.au/nvcl/)) confirms quality control is a real activity around NVCL datasets; GSWA Hackathon 2026 ([discussion #1](https://github.com/Geological-Survey-of-Western-Australia/gswa-hackathon-2026/discussions/1)) documents source package contents; WAPIMS Digital Core Atlas ([CoreAtlas](https://wapims.dmp.wa.gov.au/WAPIMS/Search/CoreAtlas)) documents linked core inspection.

### Explicit Non-Goals and Limitations

- **No Geological Prediction**: Does not predict ore grade, mineralogy, or unmeasured subsurface ground truth.
- **Interpretation Labels, Not Reflectance Curves**: Categorical labels (`swir_group`, `tir_group`) are interpretations, not measured spectral reflectance curves or elemental concentrations. Blanks indicate unassigned classifications, not absent rock.
- **No Autonomous Web Research**: AI roles select and cite admitted catalogue citations only; live web browsing is excluded.
- **No Photography or Multimodal Claims**: Core photography is not generated or aligned in this scalar workflow.
- **No Scientific, Privacy, or Licensing Guarantees**: Summaries omit raw rows, but this is a scoping control, not differential privacy. Rights must be confirmed per source manifest.
- **Product Exercise Design, Not Industry Standards**: Admitted references are real, but exercise structure and tolerances (0.01 relative median spacing, 0.02 absolute valid fraction and answer rate) are product design choices, not official industry standards.

---

## 2. Model Roles and Environment Configuration

The workflow uses four active case roles:
- `planner`: Proposes plan (`seed`, `n_rows`, `strategy`).
- `case_researcher`: Selects and explains admitted catalogue citations.
- `source_assessor`: Interprets disclosed aggregate statistics.
- `case_reviewer`: Advisory review of check results.

> [!WARNING]
> In `profiles/hylogger/profile.json`, the default configuration binds roles to `meta_cli`. This is a static manifest configuration, not a runtime retry fallback. **All four active roles must be explicitly configured to your chosen provider**, or unbound roles will use the configured default.

### 2.1 AGY CLI Setup (Gemini / Claude)

```bash
for ROLE in PLANNER CASE_RESEARCHER SOURCE_ASSESSOR CASE_REVIEWER; do
  export RULOOM_ROLE_${ROLE}_ADAPTER="agy_cli"
  export RULOOM_ROLE_${ROLE}_FAMILY="gemini"
  export RULOOM_ROLE_${ROLE}_MODEL="gemini-4-argon-high"
  export RULOOM_ROLE_${ROLE}_EFFORT="high"
done
```

### 2.2 Azure AI Foundry Setup (muse-spark-1.3)

The configured Azure route uses Entra ID tokens and text-only `muse-spark-1.3`; use your already authorized account and deployment. Availability of other models is not established by this guide. Pass `--model-policy azure-only` so non-Azure adapters are refused pre-flight with no Meta-account fallback.

```bash
export RULOOM_AZURE_SUBSCRIPTION_ID="00000000-0000-0000-0000-000000000000"
for ROLE in PLANNER CASE_RESEARCHER SOURCE_ASSESSOR CASE_REVIEWER; do
  export RULOOM_ROLE_${ROLE}_ADAPTER="azure_foundry"
  export RULOOM_ROLE_${ROLE}_FAMILY="meta"
  export RULOOM_ROLE_${ROLE}_MODEL="muse-spark-1.3"
  export RULOOM_ROLE_${ROLE}_EFFORT="xhigh"
  export RULOOM_ROLE_${ROLE}_BASE_URL="https://your-account.openai.azure.com/openai/v1/"
done
```

---

## 3. Workflow Walkthrough

### Step 1: Prepare Draft Specification (`ruloom prepare`)

`prepare` validates the selected case and checks the source format and suitability locally **before contacting the request model**. It records the source SHA-256 pin without sending source rows, classification vocabulary or licence free text to that model. After interpreting the requested row count, it checks whether that exact count is feasible **before writing a draft specification or plan**. A changed source or source registry record is refused; run intake independently repeats the source and feasibility checks before generation.

`prepare` translates the prompt into candidate settings for **human review** (`draft/request.json`, `draft/spec.json`, `draft/plan.json`). The operator must inspect the drafted specification before execution. Case references use `ID@VERSION`, with a positive ASCII integer version and no leading zeroes, version whitespace, signs, underscores or decimal forms; ID whitespace is trimmed. A failed final source or registry check removes both runnable draft files and leaves an error receipt:

```bash
# Hosted AGY run (prepare defaults to local-only; pass --model-policy hosted):
ruloom prepare /path/to/HD012-scalars.csv \
  --profile hylogger \
  --case hylogger_scalar_teaching_v1@1 \
  --request "Create 10000 synthetic rows for teaching depth-linked inclusion and missing label checks." \
  --max-attempts 2 \
  --model-policy hosted \
  --out draft/

# Azure Foundry run (requires --model-policy azure-only):
ruloom prepare /path/to/HD012-scalars.csv \
  --profile hylogger \
  --case hylogger_scalar_teaching_v1@1 \
  --request "Create 10000 synthetic rows for teaching depth-linked inclusion and missing label checks." \
  --max-attempts 2 \
  --model-policy azure-only \
  --out draft/
```

- **Draft Artifacts for Review**: `draft/spec.json` records requested target rows, source SHA-256 pin, and case identity. `draft/plan.json` contains candidate generator parameters: `seed`, `n_rows`, and `strategy` (no intervals or Markov matrices).
- **Refusal Boundaries**: An invalid case or unsuitable source is refused before the request model is called. An infeasible interpreted count is refused before a draft is written. Out-of-scope requirements reported by request interpretation are refused; the draft is still subject to human review and the independent checks at run intake.

### Step 2: Execute Generation (`ruloom run`)

`run` takes the reviewed specification, reads the source CSV, and verifies all requirements deterministically at intake before calling the planner:
- Reads the source CSV and checks source format suitability (required columns, finite non-negative ordered depth, positive median spacing, binary inclusion states, answer rates).
- Enforces source-aware integer feasibility via `compute_minimum_feasible_rows` (case floor is 25 rows, but `HD012` requires at least **44 rows** to jointly satisfy inclusion and non-empty blank label tolerances; ceiling is 200,000 rows).
- Verifies source hash matches the spec's pinned `source_sha256`.
- Compiles the canonical contract.

To replay the exact prepared plan deterministically, pass `--plan draft/plan.json` (same plan + same seed = identical bytes). Running without `--plan` invokes the planner model fresh, which is nondeterministic:

```bash
# AGY hosted execution:
ruloom run /path/to/HD012-scalars.csv \
  --profile hylogger \
  --spec draft/spec.json \
  --plan draft/plan.json \
  --max-attempts 2 \
  --model-policy hosted \
  --out workspace/

# Azure Foundry execution (requires --model-policy azure-only):
ruloom run /path/to/HD012-scalars.csv \
  --profile hylogger \
  --spec draft/spec.json \
  --plan draft/plan.json \
  --max-attempts 2 \
  --model-policy azure-only \
  --out workspace/

# Offline engineering replay (0 model calls; AI recorded as unavailable, not fake pass):
ruloom run /path/to/HD012-scalars.csv \
  --profile hylogger \
  --spec draft/spec.json \
  --plan draft/plan.json \
  --max-attempts 2 \
  --model-policy offline \
  --out workspace/
```

**Execution Order**:
1. Pre-flight suitability, hash pin, and feasibility checks.
2. `source_assessor` (aggregate only) and `case_researcher` (advisory context).
3. Plan generation (or replay if `--plan` supplied).
4. Generation and validation loop: evaluates all 13 product exercise checks over actual parsed CSV row values (`CASE_CHECK_IDS` from `profiles/hylogger/validation/workflow.py`):
   `case.source_pin_match`, `case.output_columns`, `case.row_count`, `case.well_formed_rows`, `case.ordered_depth`, `case.depth_spacing`, `case.inclusion_fraction`, `case.swir_answer_rate`, `case.tir_answer_rate`, `case.swir_supported_labels`, `case.tir_supported_labels`, `case.downstream_filter_group`, `case.downstream_structure_both_states_present`.
5. Bounded repair may change `fitted_markov_joint` to `fitted_markov_balanced` on the **exact same seed**. This can correct inclusion while another check still fails; correction is not release success. If already balanced, a remaining attempt can repeat identical bytes. No seed reroll or seed search is performed.
6. `case_reviewer`: writes advisory observations. An invalid mandatory handoff halts the run fail-closed.
7. Run artifacts land in `workspace/runs/<run_id>/`.

### Step 3: Human Operator Decision Gate

The operator inspects `workspace/runs/<run_id>/evidence-report.md`:
- **Accept**: Builds `workspace/runs/<run_id>/release.zip`.
  - Inside `release.zip`: `data.csv`, `plan.json`, `validation-report.json`, `seed.txt`, `provenance.md`, `run.jsonl`, `evidence-report.md`, `case-binding.json`, `case-contract.json`, `case-reference.json`, `case-utility.json`, `case-evidence.json`, and `checksums.sha256`.
  - Adjacent in release directory: `manifest.json` and `release.sha256`.
- **Reject**: Halts run; **no release files created**.

### Step 4: Recipient Verification Outside Repository

A recipient verifies the bundle using Ruloom without repository access:

```bash
ruloom verify workspace/runs/<run_id>/release.zip \
  --require-case hylogger_scalar_teaching_v1@1
```

- **Recipient scope**: The trusted installed profile recomputes the bounded case utility from shipped CSV and aggregate reference. Every required case result must occur exactly once, pass, match recomputed details, and agree with the shipped utility. An explicit incompatible `--profile` is refused. A legacy ZIP with no case members reports `not case-checked`; use `--require-case` whenever a case is expected.
- **Anchor vs Self-Attestation**: Keep the expected case identity separately and pass `--require-case` to prevent stripping case members. Also preserve a trusted adjacent `manifest.json` or a separately trusted whole-ZIP digest: ZIP-only semantic verification does not independently prove origin, the case binding's `spec_digest`, or the chosen plan strategy. Internal checksums can be regenerated. Expected contract/catalogue digests can additionally be supplied with `--expect-contract-sha256` and `--expect-catalogue-sha256`, but do not authenticate every release member.
- **Purpose and AI limits**: `case.verified: true` means the deterministic exercise checks passed. It does not approve user-supplied purpose, scientific validity, privacy, licensing or AI endorsement. Offline AI stages remain explicitly `unavailable`; a human must review the requested purpose and evidence report. Source context and rights may be self-attested, and keyword screens do not guarantee understanding of prose.
- **Optional Source Check**:
  ```bash
  ruloom verify workspace/runs/<run_id>/release.zip \
    --require-case hylogger_scalar_teaching_v1@1 \
    --source /path/to/HD012-scalars.csv
  ```
  Supplying `--source` recomputes reference statistics against the raw source and checks the pinned SHA-256. It does not provide blanket proof of geological truth or copyright licensing.

---

## 4. Document Cross-References

- [Research-led workflow agreement](research-led-workflow.md)
- [Model families and configuration](model-families.md)
- [Credit-free product principles](credit-free-product.md)
