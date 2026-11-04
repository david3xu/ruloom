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
| `azure_foundry` | `meta` | `muse-spark-1.3` | `xhigh` |

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

For Muse via Meta CLI, select `planner=meta_cli`, `planner=meta`, the intended Muse model
and its effort. AGY authentication and quotas stay with AGY; Muse uses its
own installation and credentials. No global CLI/account settings are edited.
`offline` denies both transports. `local-only` and `requires.open_weight`
refuse AGY; they require the existing local adapter.

For Azure Foundry, select `planner=azure_foundry`, `planner=meta`, model
`muse-spark-1.3`, and specify `--azure-subscription-id <UUID>` (or
`RULOOM_AZURE_SUBSCRIPTION_ID`) alongside `--role-base-url
planner=https://<account>.openai.azure.com/openai/v1/` (or
`RULOOM_ROLE_PLANNER_BASE_URL`). Azure Foundry requires Entra ID bearer tokens
acquired via `az account get-access-token` scoped to `--resource
https://cognitiveservices.azure.com` and the explicit subscription UUID.
No ambient default account, API keys, custom ports, or redirects are permitted.
Reasoning effort defaults to `xhigh` (`max` is refused pre-call by the backend;
supported efforts are `minimal`, `low`, `medium`, `high`, `xhigh`).
Completion token bounds default to 4096 tokens (up to 16384 via direct adapter
API; the CLI currently uses the 4096 default).
The `azure_foundry` adapter is text-only for `muse-spark-1.3`; image inputs and
vision critic roles (`complete_with_image`) are not supported.
Only the exact deployment name `muse-spark-1.3` is supported; Glimmer has no
deployable SKU or capacity on Azure Foundry in eastus.

To enforce that only Azure Foundry is contacted and prohibit any Meta account
fallback, pass `--model-policy azure-only`. Any role attempting to use a
non-Azure adapter is refused before token acquisition or invocation.
`offline` denies Azure Foundry, while `local-only` and `requires.open_weight`
refuse Azure Foundry because it is a remote cloud service.

## Evidence and limits

`models/agy_cli.py` uses an isolated temporary working directory, an
allowlisted environment and requested deny settings. `models/selection.py`
owns family defaults and conflict checks. The CLI composition root selects
the adapter; domain profiles still depend only on the shared model contract.

AGY's `init.model` identifies what its CLI reports, not independently verified
backend weights. Its terminal usage is counted once, with cache and thinking
counts recorded separately. The adapter refuses a missing, unknown or
different reported model; the selected alias must be observed in `init.model`.

AGY can expose built-in tools even when deny settings are requested. The
adapter records the exposed tools, permission mode and actual tool events;
it refuses responses using anything except the `finish` schema-finalisation
tool. That is a **post-call audit**, not prevention of a tool's effects or
filesystem confinement. Do not send private rows or treat this adapter as
an isolation boundary. True tool confinement remains planned.

`models/azure_foundry.py` validates the Azure OpenAI endpoint URL and Entra
subscription ID format before token acquisition. It validates the full returned
JSON payload against the requested schema using `jsonschema`, ensures that the
observed model matches the requested `muse-spark-1.3` deployment, and keeps raw
provider completion totals in `Usage`. Azure OpenAI completion tokens reported
by Azure Foundry already include reasoning tokens; the runner charges
`prompt_tokens + completion_tokens` against the run budget without double-counting
reasoning tokens. The adapter discards reasoning content blobs, bounds and sanitizes
request IDs, and redacts raw HTTP error bodies and bearer tokens from receipts.

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
