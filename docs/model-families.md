# Model families and the execution workflow

Ruloom selects a **role, transport, family, model and effort** independently
of the seeded generator and deterministic quality checks. A different model
name alone cannot switch the connection used to reach that model.

1. `prepare` asks the configured planner to translate the request into
   supported settings. It sends the request and capability schema, never
   source rows. The source digest binds the resulting draft locally.
2. Review the draft settings. `run --spec ...` asks the selected planner
   for a fresh generation plan. `run --spec ... --plan ... --model-policy
   offline` instead reuses a saved plan with zero model calls.
3. Code generates the data and measures its checks. A model may propose a
   repair within the caller's attempt bound; it cannot change the checks or
   grant acceptance. Offline failure cannot contact a repair model.
4. The operator reviews the evidence and chooses accept, modify or reject.
   Acceptance permits packaging. Recipient verification checks the shipped
   bytes and report; it is not a scientific endorsement or privacy proof.

## Configuration

CLI values override environment values, which override profile role bindings.
Use `--role-adapter`, `--role-family`, `--role-model` and `--role-effort` on
`prepare` or `run`. Environment equivalents are `RULOOM_ROLE_PLANNER_ADAPTER`,
`RULOOM_ROLE_PLANNER_FAMILY`, `RULOOM_ROLE_PLANNER_MODEL` and
`RULOOM_ROLE_PLANNER_EFFORT`. A profile role uses `adapter`, `family`, `model`
and `reasoning_effort`.

| Transport | Family | Example model | Effort |
| --- | --- | --- | --- |
| `agy_cli` | `claude` | `claude-opus-5-5-high` | `high` |
| `agy_cli` | `gemini` | `gemini-4-argon-high` | `high` |
| `agy_cli` | `gpt` | `gpt-oss-120b-medium` | `medium` |
| `meta_cli` | `meta` | `muse-spark-1.3` | `high` |
| `glimmer_local` | `meta` | `muse-glimmer-30B` | server dependent |

The AGY aliases were listed by the installed `agy models` on 4 November
2026. Defaults are convenience mappings, not promises about another account
or future availability. An explicit model/effort conflict or family mismatch
is refused; a provider failure never automatically switches to another model.
Selecting `agy_cli` without a model defaults to Claude Opus 5.5 high. An
explicit family can choose its default alias; GPT's listed default is medium
only. Configure an exact model when a profile already names a different model.

```sh
ruloom run SOURCE.csv --profile tabular --max-attempts 2 \
  --role-adapter planner=agy_cli --role-family planner=claude \
  --role-model planner=claude-opus-5-5-high --role-effort planner=high
```

For Muse, select `planner=meta_cli`, `planner=meta`, the intended Muse model
and its effort. AGY authentication and quotas stay with AGY; Muse uses its
own installation and credentials. No global CLI/account settings are edited.
`offline` denies both transports. `local-only` and `requires.open_weight`
refuse AGY; they require the existing local adapter.

## Evidence and limits

`models/agy_cli.py` uses an isolated temporary working directory, an
allowlisted environment and requested deny settings. `models/selection.py`
owns family defaults and conflict checks. The CLI composition root selects
the adapter; domain profiles still depend only on the shared model contract.

AGY's `init.model` identifies what its CLI reports, not independently verified
backend weights. Its terminal usage is counted once, with cache and thinking
counts recorded separately. A missing model name remains `unknown`. The
adapter refuses a reported model different from the requested alias.

AGY can expose built-in tools even when deny settings are requested. The
adapter records the exposed tools, permission mode and actual tool events;
it refuses responses using anything except the `finish` schema-finalisation
tool. That is a **post-call audit**, not prevention of a tool's effects or
filesystem confinement. Do not send private rows or treat this adapter as
an isolation boundary. True tool confinement remains planned.

`prepare` retains selected configuration, CLI-reported identity, usage and
invocation evidence in `request.json`. Runs keep their own invocation records.
Receipts distinguish Codex development work, live model proposals, seeded
generation, deterministic checks and operator decisions.

## Opt-in live smoke test

This command spends provider quota; CI never runs it:

```sh
python scripts/provider_smoke.py --adapter agy_cli --family claude \
  --model claude-opus-5-5-high --effort high --out NEW_EMPTY_PATH
```

The script creates the same invented 20-row source for each provider, asks
for 100 rows with seed 42, reviews the exact draft settings mechanically,
executes and replays that plan offline, checks byte equality, runs fresh
planning, verifies the bundles, and confirms rejection produces no release.
The accept/reject inputs are explicitly labelled automated engineering
decisions, not the owner's acceptance. All required scalar checks must pass
without skips. Evidence lands in the new directory; existing directories
are refused. It does not test images, geological validity, privacy, local
Glimmer, or comparative model quality. One passing case cannot rank models.
