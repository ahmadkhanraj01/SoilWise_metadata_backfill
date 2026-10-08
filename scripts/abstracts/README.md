# scripts/abstracts/

| File | What it does |
| :-- | :-- |
| `fill_abstract_from_title.py` | Fills empty/NULL `abstract` from `title` — **top-level `abstract` column only**, never `raw_metadata` or `updated_at`. Dry run by default; `--apply` takes a before-image backup and asserts the row count; `--restore BACKUP.jsonl --apply` undoes it. `--placeholders "xx,x,xxx,-,NA,2022"` also treats those literal values as empty. |

Run history (2026-10-01): 2,133 empty/NULL abstracts filled, then 31 placeholders — 0 remain.
Before-images: `../../output/apply/abstract_from_title*_backup.jsonl`. Input extract:
`../../data/empty_abstracts_2133.csv`. Full write-up with every SQL statement and the undo:
[`../../docs/abstracts/abstract_from_title_backfill_report.md`](../../docs/abstracts/abstract_from_title_backfill_report.md).

```bash
python3 scripts/abstracts/fill_abstract_from_title.py             # dry run
python3 scripts/abstracts/fill_abstract_from_title.py --apply
```

Known issue: queries an unqualified `records` — see [`../README.md`](../README.md#known-issues-as-of-2026-10-08) (schema now `validate`).
