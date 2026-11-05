# Development evidence

This folder records how Ruloom was built: who or what did each piece of work, which reviews
passed or asked for rework, and which commits the records point to. It is a sanitized
extract. The original export is kept privately, outside Git.

| File | What it holds |
|---|---|
| [timeline.md](timeline.md) | A readable development timeline, 1–5 November 2026 |
| [model-and-review-evidence.md](model-and-review-evidence.md) | Worker and model identities, verified live model calls and independent reviews |
| [coverage-and-redaction.md](coverage-and-redaction.md) | Retrieval method, date range, known gaps and redaction rules |
| `tasks.json` | 122 Datacore task records: id, dates, assignee, final status, review verdict, event counts, redacted summary, verified related commits |
| `events.json` | 774 task-event references: timestamp, event id, task id, type, source, status (no content) |
| `decisions.json` | 15 decision records and 1 lesson record, with redacted summaries |
| `export-stats.json` | Counts behind the tables in these pages |

Attribution is recorded as each system logged it. No contribution percentages are given,
because the records don't support them: a task count doesn't measure effort or quality.
