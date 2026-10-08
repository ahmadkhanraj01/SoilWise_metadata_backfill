# scripts/

Everything that reads or writes the SoilWise database, grouped by job. Run every script
**from the project root** (`python3 scripts/<folder>/<script>.py`). DB credentials come from
`crosscheck/.env`. Dependencies: `psycopg2`, `python-dotenv`, `requests`, `ftfy` (cleanup only).

| Folder | Job | Writes to the DB? | Details |
| :-- | :-- | :-- | :-- |
| [`cleanup/`](cleanup/README.md) | scan, dry-run, verify and apply the messy-text cleanup (emoji, pipes, HTML tags, mojibake …) | `apply_cleanup.py` only | `cleanup/README.md` |
| [`abstracts/`](abstracts/README.md) | fill empty / placeholder `abstract` from `title` | with `--apply` | `abstracts/README.md` |
| [`url/`](url/README.md) | `records.url` column, source-catalogue URL pass, HTTP checks, `distributions` rows | `build_url_column`, `apply_source_urls`, `fix_source_urls_unescape`, `add_*_distributions` (each with `--apply`) | `url/README.md` |
| [`diagnostics/`](diagnostics/README.md) | one-off, read-only inspectors kept for reproducibility | no | `diagnostics/README.md` |

The earliest phase (Sep 2026 title/abstract crosscheck) lives in [`../crosscheck/`](../crosscheck/README.md).

## Conventions

* **Dry run by default.** Every writer needs `--apply`. Each takes a before-image first, writes in one
  transaction, asserts the row count, and rolls back on any mismatch. Undo instructions are in the
  matching report under `../docs/`.
* **Shared modules:** `cleanup/cleanup_rules.py` (cleanup rules) and `url/url_rules.py` (identifier → URL
  rules); other scripts in the same folder import them by plain name, so keep each folder together.
* **Paths:** scripts find the project root as `Path(__file__).resolve().parents[2]` and write into
  `../output/` and `../docs/`; nothing is read from the current directory.

## Known issues (as of 2026-10-08)

1. **Schema** (full list and advice: [`../docs/known_issues_schema_validate.md`](../docs/known_issues_schema_validate.md)). The database moved from `public` to schema `validate` on 2026-10-07 and
   `crosscheck/.env` now says `DB_SCHEMA=validate`. Only `url/compare_url_accessurl.py`, the newest
   `url/*` writers (`apply_source_urls`, `fix_source_urls_unescape`, `add_*_distributions`) and the
   `crosscheck/` scripts honour it. The older scripts (`cleanup/*`, `abstracts/*`, `url/build_url_column.py`,
   `url/find_urls_external.py`, `diagnostics/*`) query an unqualified `records` and fail with
   *relation "records" does not exist* until `search_path` is set to `validate`.
2. **Duplicate code.** `url/check_urls_source.py` is a copy of `url/check_urls.py` with different output
   file names; fold it into a `--tag` option of `check_urls.py` when convenient.
