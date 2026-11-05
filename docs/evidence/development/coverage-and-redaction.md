# Export coverage and redaction

## Retrieval

- **When:** 5 November 2026, about 01:50–02:00 UTC.
- **How:** read-only calls to the local Datacore front door through its HTTP MCP interface,
  using the `dc-call` helper. The helper reads its token from the local bundle and never
  prints it.
- **Tools:**
  - `get_tasks` with `project: "ruloom"` and `status: "all"`, then repeated per status and
    per assignee, because each call returns at most 100 records;
  - `get_questions`;
  - `get_planning_ledgers`;
  - `search` for `decision`, `lesson`, `task_fail` and `task_block` events.
- **Scope:** project `ruloom` only. No other project was queried.

## Coverage

| Record kind | Retrieved | Verified complete? |
|---|---|---|
| Tasks | 122 of the 122 reported by `total_tasks` | yes: the union of status and assignee queries matches the total |
| Task events | 774, embedded in those tasks | complete for those tasks, as returned |
| Review verdicts | 52 `task_review_verdict` events | as returned |
| Decisions | 15 | not verified: search returns ranked matches, capped at 100 |
| Lessons | 1 | not verified, for the same reason |
| Questions | 0 | the API returned none |
| Planning ledgers | 56, counted only | content kept private, not published |

Date range: tasks were created from 2026-11-01T07:10Z to 2026-11-04T12:12Z, and events run
to 2026-11-04T15:46Z.

**Known gaps:**
- Work on 5 November (PRs #30–#35, the mini smoke and the final test) has no task records in
  this export; it is evidenced by pull requests and run logs instead.
- Conversation and worker-session events were not exported.
- Search-based kinds may be incomplete. This is not a complete history of the project.

## Redaction

The published files keep only metadata (ids, timestamps, types, sources, statuses, counts)
and short summaries. Each summary is passed through these rules:

- local home paths become `~/`, and temporary paths become `[local-tmp-path]`;
- email addresses become `[email]`, and the owner's personal name becomes `[owner]`. The
  public GitHub account in repository URLs is kept;
- UUIDs, including any subscription or dataset identifiers, become `[uuid]`;
- Azure endpoint hostnames become `[azure-host]`;
- anything shaped like `token=`, `secret=`, `password=` or `api_key=` is redacted;
- summaries are cut to 220 characters (300 for decisions).

Excluded altogether:
- event contents (prompts and worker reports);
- planning-ledger bodies;
- credentials;
- source data rows;
- benchmark answers.

Benchmark work appears only as task titles.

A commit is listed in `related_commits` only if `git cat-file` confirmed it exists in the
local repository: 112 distinct commits, across 114 tasks.

The unredacted export stays outside Git with private permissions. The builder script that
produced these files is kept with the local test evidence.
