# Decision log: implicitly settled decisions

Nine product decisions that were settled in code without a recorded
decision note. Each entry names the date, the code that settles it,
and the rationale where the code states one; where the code states no
rationale, the entry names the introducing commit
(decided-by-implementation). Verified against the tree on 2026-11-02.

1. Attempts bind the manifest digest and source hash. `engine/core/records.py:117`
   (`Attempt.manifest_digest`, alongside `source_sha256` and `spec_digest`);
   the digest itself is computed in `engine/core/manifest.py:486` and
   persisted per attempt in `engine/core/runstore.py:117`. Rationale is
   stated in the `records.py` module docstring: attempts are immutable,
   so selecting a best attempt replays that attempt's own plan, seed,
   and artifact. Introduced 2026-11-01 by 6ce0635 (RP-02).

2. Only `writes_verdict` capabilities record checks (the ledger's
   "writes_artifact" label maps to this permission). Declared in
   `engine/core/manifest.py:52`, enforced in `engine/core/runner.py:112`
   and `engine/core/runner.py:446`, read from the manifest in
   `engine/core/manifest.py:544`; profiles declare it in
   `profiles/hylogger/profile.json:34` and `profiles/tabular/profile.json:34`.
   Rationale is stated in the `runner.py` module docstring: the engine
   binds artifact hash, attempt, and verifier, and computes the verdict
   only from that validated evidence. Introduced 2026-11-01 by 1975d8a
   (Loop 7).

3. Generation structure is selected by plan strategy inside spec bounds.
   The HyLogger manifest offers `profiles/hylogger/profile.json:474`
   (`strategies`) with `profiles/hylogger/profile.json:475`
   (`default_strategy`); `profiles/hylogger/generation/plan.py:88`
   refuses a plan naming a strategy outside the spec-supported set, and
   repair prompts carry only the allowed strategies
   (`profiles/hylogger/generation/repair.py:57`). The image/table views
   share one schematic scene record
   (`profiles/hylogger/imaging/scene.py:110`). Decided-by-implementation:
   strategy bounds 2026-11-02 by f1ed40d (RP-07), shared scene
   2026-11-02 by fe9efbf (RP-06).

4. Repair consumes failed-check JSON, never the critic note.
   `profiles/hylogger/generation/repair.py:57` builds the repair prompt
   from failed `CheckResult` JSON plus the current plan, the supported
   corrective options, and the disclosed profile statistics; the module
   docstring bars raw rows. Advisory critic notes cannot reach repair
   by construction: `engine/core/runner.py:1691` records an optional
   critic's failure without touching the verdict. Introduced 2026-11-01
   by 49a9fc0 (Loop 5); advisory isolation 2026-11-02 by 1c4d155 (RP-01).

5. Invariant bands are derived real-against-real and frozen in the
   profile. `profiles/hylogger/analysis/calibration.py:1` derives each
   band as the observed range across calibration holes widened by the
   within-hole split-half delta; the frozen values ship in
   `profiles/hylogger/thresholds.json` and are consumed in
   `profiles/hylogger/validation/checks.py:263`. Rationale is stated in
   the `calibration.py` module docstring. Introduced 2026-11-01 by
   8d17afb (Loop 2).

6. A human accepts; nothing else can. `engine/core/runner.py:1868`
   prompts `Decision [accept/modify/reject]`, records the verbatim
   `decision.json`, and releases only on `accept`
   (`engine/core/runner.py:2074`); a failing validation never reaches
   the gate (`engine/core/runner.py:2148`). Rationale is README rule 4
   (no model approves its own output). Introduced 2026-11-01 by 75fb115
   (Loop 6).

7. The model sees disclosed statistics only. Prompts carry
   `Profile.to_disclosed_json`
   (`profiles/hylogger/analysis/statistics.py:389`,
   `profiles/tabular/analysis/statistics.py:158`), which withholds
   values shared by fewer than two source rows
   (`profiles/hylogger/analysis/statistics.py:30`,
   `profiles/tabular/analysis/statistics.py:30`). Rationale is stated
   in both statistics module docstrings (scoping control, not an
   anonymity guarantee). Introduced 2026-11-02 by a044776 (RP-04).

8. Releases carry no default output dedication. `engine/artifacts/package.py:129`
   returns the granted term only for a hash-verified source with an
   explicit grant, else `None` ("There is no default dedication");
   `docs/provenance.md:27` records that the source is not relicensed by
   its presence here. Rationale is stated in the `resolve_output_terms`
   docstring (a recorded source licence describes the source, never the
   output). Introduced 2026-11-02 by a044776 (RP-04).

9. The vision-critic seat stays with the Spark adapter while Glimmer
   image weights are absent. `models/glimmer.py:16` records that the
   `mmproj` weights are not present, so the local adapter never
   attaches images; vision critique over synthetic renders only is
   `profiles/hylogger/imaging/critic.py:1` via
   `models/meta_cli.py:199`. Decided-by-implementation: Glimmer text
   seat 2026-11-01 by 457b49d (Loop 9), vision seat 2026-11-01 by
   2db9a2b (Loop 8).
