# scripts/diagnostics/

One-off, **read-only** inspectors kept so earlier decisions can be reproduced. None writes to the database.

| File | What it answers |
| :-- | :-- |
| `db_check.py` | schema of `records` + row count |
| `db_check2.py` | what one `raw_metadata` document looks like |
| `inspect_types.py` | type of each jsonb key (str / list / list-of-JSON) |
| `test_parse.py` | do `view_*` items parse as JSON? |
| `inspect_singles.py` | literal `\xNN` escapes and control characters (the two manual-fix cases) |
| `inspect_backslash.py` | classify every backslash: `\xNN` vs LaTeX-like vs other |
| `sample_issues.py` | print real samples for each (field, issue) before encoding a rule |

All query an unqualified `records`; since the DB moved to schema `validate` they fail until `search_path` is set
(see [`../README.md`](../README.md#known-issues-as-of-2026-10-08)).
