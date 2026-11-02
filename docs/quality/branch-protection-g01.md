# G01 branch-protection receipt (live, read-only)

The `main-gate` ruleset below was read live from the GitHub API with no
settings changed, no push attempted, and no remote branch created. The
repo-observable half -- the CI `gate` job the ruleset requires -- is
enforced by `tests/governance/test_branch_protection.py` (rule
`Q-GOV-BRANCH-PROTECTION`); this file is the recorded receipt for the
GitHub-side half, which no product test can observe.

## Live ruleset (re-checked 2026-11-02 during DP-03)

Exactly one ruleset exists on `david3xu/ruloom`:

- id `26349205`, name `main-gate`, target `branch`, enforcement `active`
- conditions `ref_name.include = ["refs/heads/main"]`, exclude empty
  (targets main only)
- rules: `deletion`, `non_fast_forward`, `required_linear_history`,
  `required_status_checks`, `pull_request`
- `required_status_checks`: context `gate`, non-strict
  (`strict_required_status_checks_policy: false`,
  `do_not_enforce_on_create: true`)
- `pull_request`: `required_approving_review_count: 0`,
  `allowed_merge_methods: ["rebase"]` (rebase-only merges, matching the
  linear-history rule and the no-new-merge-commits half enforced by
  `Q-GOV-SCOPED-WRITES`)
- `bypass_actors: []`, `current_user_can_bypass: "never"` (no bypass
  identity of any kind, admins included)

Classic branch protection (`branches/main/protection`) returns HTTP 404
`{"message":"Branch not protected"}`: expected, the ruleset is the
enforcement mechanism, not the legacy endpoint.

## Merge-gating (proven when the first PRs ran)

- PR #1 (DP-01), #2 (DP-01b), #3 (DP-02): each shows the required
  `gate` check passing on the branch, and each merged to main.
- Latest main CI run (DP-02 push) completed `success`.
- Direct-push refusal is shown by the ruleset text (`deletion` +
  `non_fast_forward` + required `gate` check with zero bypass actors),
  never by a forbidden push probe.

## Limits stated, not waived

- Settings were verified read-only on 2026-11-02 (G01 verify) and
  re-checked live during DP-03 (same id, same active enforcement, same
  five rule types, empty bypass). A future settings change is outside
  what the repo can observe; the executable half above fails closed
  first if the repo side drifts (job renamed, steps hollowed, triggers
  narrowed).
- First verified: 2026-11-02. Re-check with `gh api
  repos/david3xu/ruloom/rulesets/26349205` (read-only).
