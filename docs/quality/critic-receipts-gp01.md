# GP-01 live critic receipts (G3)

Dated 2026-11-02. One live Glimmer text-critic run through the real
product path plus live vision-critic calls on a synthetic-only PNG.
Branch `gp01/live-critic-runs`. Machine-local run directories live
under /tmp/gp01-live (git-ignored by design, never committed); the
essential values are copied below with the run id and artifact hashes
they came from.

Scope of what ran live: exactly one Glimmer text run and the vision
calls listed below. No local vision claim is made: the Glimmer image
weights (mmproj) are absent, so Glimmer serves the text critique only
and the Spark multimodal adapter stays the vision critic for synthetic
renders.

## 1. Glimmer text critic (live, real product path)

Command (repository root):

    ruloom run data/sources/PDD446-scalars.csv --max-attempts 1 --local-critic

Server: llama-server with
`Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf`, `-c 32768`
(see `models/glimmer.py` `RECOMMENDED_CONTEXT_TOKENS`).

- Run id: 8b917d31eb7c45238cae64e520924483 (2026-11-02T10:38:14Z).
- Source: PDD446-scalars, 39300 rows,
  sha256 9705c4b6494d34fea6392dd25483fdad952ef2cf4103305ab67196f5dc2349d7.
- Artifact: sha256
  55eb1c22e212cc61523548875af5e98b3c040aecdb7d9dc7ac59d7836f58d85f
  (matches the golden artifact), verdict pass, human decision accept.
- Critic step: `[local_critique] advisory text note recorded`; note
  stored as critic-note.json in the machine-local run directory.
- Server-reported model identity (recorded in the run log):
  /Users/291928k/models/muse-glimmer/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf
  (requested model muse-glimmer-30B).
- Wall time: about 778 s for the local-critic call, derived from run
  event timestamps (generate_validate_repair ok 10:38:32Z,
  model_invocation for role local_critic 10:51:31Z; the image steps
  are silent no-ops without companion images, so the gap is the
  critic call). The server measured the prompt at 8396 tokens (seen
  in the pre-fix 400 refusal below, same prompt shape). The product
  records the per-call model identity; per-call critic token usage is
  accumulated toward the run budget only, so the receipt records wall
  time instead, as the task allows.
- Critic note (verbatim):
  - critique: "The report passes basic geometry, row count, depth
    monotonicity and marginal TVD checks for SWIR and TIR, and TIR
    persistence is constrained. However SWIR answer rate is skipped
    with a vacuous band, SWIR persistence/transition fidelity is
    absent, and joint SWIR-TIR agreement/coverage is not validated.
    Tolerances for swir_answer_fidelity and
    repeated_depth_fraction are very permissive, and the synthetic
    depth end is shorter than the source end with a large number of
    masked repeats. Overall the checks cover structural integrity but
    leave key distributional and cross-band failure modes weakly or
    untested."
  - concerns: swir_answer_rate skipped due to vacuous band; no SWIR
    persistence or transition fidelity check; no joint SWIR-TIR
    agreement/coverage validation; synthetic depth end 551.3 m <
    source end 555.5 m; swir_answer_fidelity tolerance 0.645 is
    effectively vacuous; repeated_depth_fraction band [0,0.351] is
    overly permissive; no validation of suppressed label handling.

### Pre-fix finding (fixed on this branch)

The first live attempt failed loudly: the prompt (8396 tokens)
exceeded the server context (`-c 8192`), and the run refused instead
of skipping the critique. The documented startup command used no
`-c` flag (server default is smaller still), so the documented path
was broken for a real hole. Fix, bounded to the critic path:

- `models/glimmer.py`: new `RECOMMENDED_CONTEXT_TOKENS = 32768`,
  and a 400 context-window refusal now raises an actionable
  `ModelError` naming the `-c` remedy instead of a bare HTTP
  status. No prompt, verdict, or release behaviour changed.
- `tests/test_glimmer.py`:
  `test_context_overflow_raises_actionable_model_error` (stub-server
  400 with the live exceed-context body; fails before, passes
  after). The live run above proves the fix.
- `README.md` and `docs/demo-script.md`: server command now carries
  `-c 32768`, with a note that the context is required, not
  optional.

## 2. Vision critic (live, synthetic image only)

The attached image was rendered by seeded repository code only
(`profiles/hylogger/imaging/generate.py`, seed 7, 64x32 banded
strips): 4116 bytes, sha256
e5ab9fb500398d557e211761d87580c4542493f92161e9fca0271853becec45e.
No source pixels, no core photo, never a real image.

Product path: the `image_critique` module prompt and schema builders
plus the `complete_with_image` adapter the capability calls
(`profiles/hylogger/imaging/critic.py`,
`models/meta_cli.py`), client `muse-spark-1.3` at default max
effort, contributor mode on.

Three calls were made in total and all are disclosed: one
exploratory call at low effort (note returned, usage not captured),
one max-effort call whose output was truncated by local logging, and
the recorded call below (2026-11-02T10:44:50Z-10:45:16Z, wall
26.3 s):

- usage: model muse-spark-1.3, input_tokens 26790, output_tokens 561,
  calls 1 (input count includes the attached image).
- note (verbatim): "Tiny 64x32 synthetic strip shows muted dark olive
  to brown horizontal banding with broadly continuous colour and no
  obvious block or noise artefacts at this scale, but contrast is very
  low and texture appears overly smooth and uniform, giving limited
  plausibility as a detailed banded core-strip rendering."
