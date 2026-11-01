# Ruloom Force-Pass Rules

These rules are enforced by code, not only by docs. The mechanical enforcer is
[scripts/force_pass.py](scripts/force_pass.py) (stdlib only). Run it from the
repo root before review:

```sh
.venv/bin/python scripts/force_pass.py
```

It prints one `PASS`/`FAIL` line per rule with evidence and exits non-zero on
any `FAIL`.

## Docs

- **FP-DOC-1** — Every path named in the README Layout table exists.
- **FP-DOC-2** — Every profile feature module (`profiles/*/*/*.py`,
  except `__init__.py`) is named in `README.md` or `docs/`.
- **FP-DOC-4** — No unmeasured-claim phrasing (`TBD`, `TODO`) in `README.md`
  or `docs/*.md`.

## Code

- **FP-CODE-2** — Layer boundaries (checked via AST): `profiles/` imports no
  `engine` and no `models`, except `profiles/*/generation/plan.py` and
  `profiles/*/generation/repair.py` may import the `models.base` contract
  only (never `models.meta_cli`); `models/` imports no `profiles` and no
  `engine`; `engine/` imports no `models.meta_cli` directly, except
  `engine/cli/__init__.py` (the composition root).
- **FP-CODE-3** — Reuse hashing: `engine/artifacts/hashing.py` owns the
  sha256 helpers; no other module defines its own sha256 helper.
- **FP-CODE-4** — Every profile feature module has a matching
  `tests/profiles/<profile>/test_<feature>.py`.
- **FP-CODE-5** — `git ls-files` contains no `data/` path, and `.gitignore`
  contains a `data/` rule.
- **FP-CODE-6** — `engine/` contains no domain words (`depth`, `mineral`,
  `swir`, `tir`, `TSG`, `Markov`, `hole`, `hylogger`) outside comments
  naming the rule itself.
- **FP-CODE-7** — Every profile manifest (`profiles/*/profile.json`) graph
  uses only `step`/`loop`/`gate` nodes, and every `loop` binds
  `max_attempts` to the caller supply with no default.
- **FP-CODE-8** — No feature `.py` module sits directly under `engine/` or
  under `profiles/<profile>/` (only `__init__.py` at the engine root; only
  `__init__.py` and `capabilities.py` as manifest/wiring at a profile
  root). Engine features live in `core/`, `artifacts/`, `audit/`, `cli/`;
  profile features live in `intake/`, `analysis/`, `generation/`,
  `validation/` (plus `imaging/` for the HyLogger image extension:
  `codec.py`, `intake.py`, `statistics.py`, `generate.py`, `checks.py`,
  `critic.py`).

## Review

Reviewers must score every rule `PASS` or `FAIL` with evidence in the verdict.
Any `FAIL` blocks merge and sends rework. New features follow the same shape:
one named file in the owning package, reuse existing helpers, no
cross-boundary imports, docs kept in step with the tree.
