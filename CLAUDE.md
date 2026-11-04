# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Ruloom turns a supported source profile (HyLogger NVCL scalars, or a generic tabular CSV) into reproducible synthetic data. A model writes a schema-bound plan, seeded code generates the rows, deterministic checks bound to artifact SHA-256s decide the verdict, and a human gate accepts the release. `README.md` is the authoritative product description; `AGENTS.md` lists the force-pass rules.

## Commands

Python 3.12 only (`requires-python = ">=3.12,<3.13"`). Use the existing repo venv, `.venv/bin/...`. Do not run `uv run`, `uv sync` or `uv lock`; the owner prohibits uv management for this work. Preserve the untracked local `uv.lock` and never stage it.

```sh
.venv/bin/python -m pytest -q                                   # full suite
.venv/bin/python -m pytest -q tests/engine/test_runner.py       # one file
.venv/bin/python -m pytest -q tests/engine/test_runner.py::test_name   # one test
.venv/bin/ruff check . && .venv/bin/ruff format --check .        # lint + format (ruff pinned 0.17.3)
.venv/bin/python scripts/force_pass.py                          # force-pass gate; must exit 0 before review
```

CI (`.github/workflows/ci.yml`) runs those three in this order: ruff, pytest, force_pass. `tests/test_code_hygiene.py` also runs ruff and fails closed if ruff is not installed, so a missing ruff fails the suite.

Running the product:

The manifests include hosted Meta defaults. This session prohibits Meta-account/MuseCLI calls. Bind every active scalar role (`planner`, `case_researcher`, `source_assessor`, `case_reviewer`) explicitly to the authorized provider, or use a saved plan with `--model-policy offline` (no AI endorsement). See `docs/research-workflow-user-guide.md`. Azure engineering configuration is separate from the frozen Test 1/Test 2 comparison; no new paid smoke or benchmark is authorized here. Do not enable the optional image route for this scalar case.

```sh
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3          # default profile: hylogger
.venv/bin/ruloom run --profile tabular SOURCE.csv --max-attempts 2 [--spec spec.json] [--plan plan.json] [--model-policy hosted|local-only|offline] [--out DIR]
.venv/bin/ruloom prepare --profile tabular SOURCE.csv --max-attempts 2 --request "..." --out DIR
.venv/bin/ruloom verify <release>
```

`--max-attempts` is required on purpose. Manifests bind loop `max_attempts` to the caller with no default (FP-CODE-7). `python -m engine.cli` also works.

## Data and source-dependent tests

`data/` (NVCL sources under `data/sources/`, the calibration set under `data/calibration/`) and `runs/` are git-ignored and must never be committed (FP-CODE-5, `tests/governance/test_scoped_writes.py`). Tests that need real data are marked `skipif` and skip when the data is missing, so a source-free checkout shows skips instead of failures. Pytest collects only `tests/`; ignored evidence copies under `runs/` are preserved. Governance tests read `HEAD` history and expect the phased commit sequence; use the existing protected PR workflow.

## Architecture

Three layers, and imports may only cross between them where FP-CODE-2 allows (checked by AST in `scripts/force_pass.py`):

- **`engine/`**: generic and domain-agnostic. FP-CODE-6 bans domain words such as `depth`, `mineral`, `hole`, `TSG`, `Markov`. No feature module may sit at the `engine/` root (FP-CODE-8).
  - `core/runner.py` executes the profile's graph. A graph is built from `step` / `loop` (generate→validate with `on_fail: repair`) / `gate` (human decision) nodes. Each capability gets a frozen `RunContext` limited to its declared permissions. The engine alone owns verdicts, chosen artifacts and state transitions. Advisory outputs (critics) go to a separate store that verdict computation never reads. Raw source bytes are available only to capabilities that declare `reads_raw_source`. Model clients are wrapped in `_BudgetedClient`, which enforces `models.budget.max_tokens_per_run` before each call.
  - `core/manifest.py` loads and validates `profiles/<id>/profile.json`. `spec.py` holds the GenerationSpec. `verdict.py` computes acceptance from per-check `required` status. Only `pass` satisfies a required check, and an empty check set cannot pass. `model_policy.py` handles hosted, local-only and offline. `runstore.py` and `dispatch.py` handle durable run history and leases.
  - `artifacts/`: `hashing.py` is the only place sha256 helpers may live (FP-CODE-3). It also holds release packaging, consumer verification and publication.
  - `audit/runlog.py` is the append-only, hash-chained run log.
  - `cli/__init__.py` is the composition root. It is the only engine module allowed to import `models.meta_cli`. It resolves role bindings in the order CLI > env (`RULOOM_ROLE_<ROLE>_MODEL|ADAPTER|EFFORT|BASE_URL`) > manifest > adapter default, then records the result as a `model_bindings` run-log event.
- **`profiles/<id>/`**: domain code. `profile.json` declares capabilities (`impl`, `stage`, `calls_model`, `reads_raw_source`, `writes_verdict`, `advisory`), the checks, the graph and `models.roles`. `capabilities.py` exports a `CAPABILITIES` dict, which the CLI imports as `profiles.<id>.capabilities`. Feature code goes only in `intake/`, `analysis/`, `generation/`, `validation/` (plus `imaging/` for HyLogger). Profiles must not import `engine` or `models`. The one exception: `generation/plan.py` and `generation/repair.py` may import `models.base`.
- **`models/`**: the only code that talks to a model. Adapters are `meta_cli` (hosted Muse), `agy_cli` (Claude/Gemini/GPT families), `glimmer` (`glimmer_local`, a loopback OpenAI-compatible server), `azure_foundry`, plus `local_transport` and `selection`. Any role with `requires.open_weight` must bind `glimmer_local` (FP-CODE-9). Disabled roles, such as HyLogger's `reporter`, must never resolve (FP-CODE-10).

Invariants that are easy to break by accident:
- A model output never becomes a data row, and raw source rows never reach a model. Prompts get summary statistics only.
- Same plan + seed ⇒ identical `data.csv` bytes. Compare data digests, not release ZIPs, which carry per-run identity.
- A run that exhausts its repairs stops and reports the failing checks. Critics are advisory and cannot flip a verdict.

## Adding or changing a feature

Changes are gated mechanically, so keep these in step:
- Put a new profile feature module in its owning subpackage. Add a matching `tests/profiles/<profile>/test_<feature>.py` (FP-CODE-4), and name the module in `README.md` or `docs/` (FP-DOC-2).
- If the README Layout table changes, also update the `layout` list in `docs/quality/rules.json`, the rule registry that the aggregate Q stage of `force_pass.py` executes (FP-DOC-1).
- Feature records live in `catalogue/features/**/F-*.json`: id, owner, code/doc/test paths, required rules, status. Update the record when you change the code it lists.
- Don't write `TBD` or `TODO` in `README.md` or `docs/*.md` (FP-DOC-4). Product claims in docs are tied to specific dated runs and revisions; don't restate them as general facts.
