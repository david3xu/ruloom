# Demo video script (Step 15)

Target: under 3 minutes, publicly viewable, recorded from a real terminal.
Every number spoken comes from `docs/results.md` (measured, review-audited).
Do not paraphrase a number from memory; read the table on screen.

With hosted credits exhausted, use the **offline replay** below and show a
genuine earlier `model_invocation` receipt beside it. Label the receipt as a
previous Meta call and the running command as replay, not fresh inference.
The current credit-free extension was implemented by Codex. Local planning
and request interpretation await the owner's real evaluation; do not present
them as demonstrated model performance yet.

## Storyboard

| Time | On screen | Narration |
|---|---|---|
| 0:00–0:30 | `docs/results.md` per-hole table | "Ruloom turns a supported source into synthetic data with checked evidence. The model proposes settings, code generates the rows, and a human decides acceptance. These are dated benchmark results; the baseline varied across repeated runs." |
| 0:30–1:00 | `README.md` Status and Layout sections | "Ruloom is a generic engine. A profile declares its workflow, its checks, and which model may do what. The engine enforces those declarations in code. Quality thresholds were calibrated real-against-real before any generator existed." |
| 1:00–2:15 | Earlier genuine model receipt, then offline terminal replay | Show the recorded requested/reported model and its plan. Say "That was the earlier Meta call. This replay makes no model calls." Run the fixed plan under the offline policy: source identity, deterministic generation, checks, human review, and evidence report. Accept only after inspecting the actual checks. Compare the data digest with the earlier run, never ZIP hashes. The report shows replay origin and any unavailable reviews. |
| 2:15–2:45 | `docs/results.md` aggregate and repeat tables | "The pipeline passed all four held-out cases. The direct-LLM baseline passed one in the primary run and three in the repeat. The token comparison used different output sizes, so it is not a matched-row cost benchmark." |
| 2:45–3:00 | Run directory listing + `release.zip` contents | "Every release carries its own evidence: the plan, the seed, the validation report, and a hash-chained log. Synthetic data, woven from reality — backed by evidence." |

## Fresh inference (deferred while credits are unavailable)

```bash
cd ~/workspace/ruloom
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3
```

Inspect the actual verdict before deciding. A fresh model may choose a
different seed, so no fixed artifact digest or passing result is promised.
Keep its run directory (`runs/<run-id>/`) and original model receipt.

## Reproducibility rerun (fixed-plan replay)

A fresh planner call is a new run: it may propose a different plan, so
repeating the live command never proves reproducibility. Deterministic
generation is demonstrated by replaying the fixed plan saved above
(`runs/<run-id>/plan.json`), with all model calls disabled:

```bash
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3 \
  --plan runs/<run-id>/plan.json --model-policy offline --out /tmp/ruloom-replay
```

Inspect the actual checks before accepting. Then compare the data digests
(identical), not the release ZIPs (per-run identity and log, so they
differ by design):

```bash
sha256sum runs/<run-id>/data.csv /tmp/ruloom-replay/runs/*/data.csv
```

Optional later local evaluation (not part of the present preparation;
server must be started first, model already on disk):

```bash
llama-server -m ~/models/muse-glimmer/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf --port 8080 -c 32768
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3 \
  --model-policy local-only --role-adapter planner=glimmer_local --local-critic
```

The local open-weight critic's note appears as `critic-note.json`. Say
explicitly: the critic is advisory and cannot change the verdict.

## Recording checklist

- Record the real terminal at readable font size; no mock-ups, no edited output.
- Keep the whole video under 3:00 (submission checklist, plan section 15).
- Upload publicly (unlisted is acceptable if the submission form accepts a link).
- Put the video link in `README.md` next to the Status section after upload.
- Attach pitch slides (PDF, max 25 MB) through the submission form, per the
  GSWA README checklist in the hackathon home repository. Margin note:
  that checklist was not found in plan docs 01-13, so the slides
  requirement is unverified here — confirm the current requirement on
  the submission form itself before submitting.
