# Demo video script (Step 15)

Target: under 3 minutes, publicly viewable, recorded from a real terminal.
Every number spoken comes from `docs/results.md` (measured, review-audited).
Do not paraphrase a number from memory; read the table on screen.

## Storyboard

| Time | On screen | Narration |
|---|---|---|
| 0:00–0:30 | `docs/results.md` per-hole table, baseline rows highlighted | "Ask a large model to write data directly and it fails the checks on three of four unseen drill holes — while using forty-seven times the tokens. This is Ruloom: the model plans, code generates, checks prove, a human accepts." |
| 0:30–1:00 | `README.md` Status and Layout sections | "Ruloom is a generic engine. A profile declares its workflow, its checks, and which model may do what. The engine enforces those declarations in code. Quality thresholds were calibrated real-against-real before any generator existed." |
| 1:00–2:15 | Live terminal run (command below) | Walk the stages as they print: intake hashes the source and reads its licence; only statistics reach the model; the model proposes seed and row count; seeded code generates; deterministic checks validate; the run stops and asks a human. Type `accept`. Show the release line and the artifact hash. Then run the same command again and show the identical artifact hash: same plan, same seed, same bytes. |
| 2:15–2:45 | `docs/results.md` aggregate table | "On four held-out holes the pipeline passes four of four, reproducible four of four, in eighty-two seconds and one hundred thirty-five thousand tokens. The direct-LLM baseline passes one of four and is reproducible zero of four. On one hole, NCB0003, both pass — we say so in the report." |
| 2:45–3:00 | Run directory listing + `release.zip` contents | "Every release carries its own evidence: the plan, the seed, the validation report, and a hash-chained log. Synthetic data, woven from reality — backed by evidence." |

## Live-run command

```bash
cd ~/workspace/ruloom
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3
```

At `Decision [accept/modify/reject]:` type `accept`, then a short note such
as `demo run`. Expected: verdict `pass`, artifact
`55eb1c22e212cc61523548875af5e98b3c040aecdb7d9dc7ac59d7836f58d85f`,
`release.zip` written. Run it a second time to show the identical hash.

Optional, if the local critic is wanted on camera (server must be started
first, model already on disk):

```bash
llama-server -m ~/models/muse-glimmer/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf --port 8080
.venv/bin/ruloom run data/sources/PDD446-scalars.csv --max-attempts 3 --local-critic
```

The local open-weight critic's note appears as `critic-note.json`. Say
explicitly: the critic is advisory and cannot change the verdict.

## Recording checklist

- Record the real terminal at readable font size; no mock-ups, no edited output.
- Keep the whole video under 3:00 (submission checklist, plan section 15).
- Upload publicly (unlisted is acceptable if the submission form accepts a link).
- Put the video link in `README.md` next to the Status section after upload.
- Attach pitch slides (PDF, max 25 MB) through the submission form, per the
  GSWA README checklist in the hackathon home repository.
