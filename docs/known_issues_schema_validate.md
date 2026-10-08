# Known issue — older scripts do not work against schema `validate`

**Written 2026-10-08. Status: documented only. No script was changed.**

## What happened

On 2026-10-07 the database `msp-kh-dev` was migrated from schema `public` to schema **`validate`**
(`validate.records`, `validate.distributions`, …). `crosscheck/.env` now says `DB_SCHEMA=validate`.
The connection's `search_path` is `"$user", public`, so an **unqualified** `records` no longer resolves:

```
psycopg2.errors.UndefinedTable: relation "records" does not exist
```

Seen when running `scripts/diagnostics/db_check.py` on 2026-10-08.

## Which scripts are affected

All of these use an unqualified `records` and read neither `DB_SCHEMA` nor set `search_path`:

| Script | Unqualified statements (line) | Writes? |
| :-- | :-- | :-- |
| `scripts/cleanup/find_messy.py` | `SELECT … FROM records` (88) | no |
| `scripts/cleanup/dry_run.py` | `SELECT … FROM records` (116) | no |
| `scripts/cleanup/apply_cleanup.py` | staleness check `FROM records` (87) and the per-target UPDATEs | **yes** |
| `scripts/abstracts/fill_abstract_from_title.py` | `UPDATE records` (65, 137), `SELECT … FROM records` (96, 99, 145, 148) | **yes** |
| `scripts/url/build_url_column.py` | 15 statements incl. `ALTER TABLE records ADD COLUMN url`, `ADD CONSTRAINT`, `UPDATE records`, `VALIDATE CONSTRAINT` (73–288) | **yes** |
| `scripts/url/find_urls_external.py` | `SELECT … FROM records` (181) | no |
| `scripts/diagnostics/*` (`db_check`, `db_check2`, `inspect_types`, `inspect_singles`, `inspect_backslash`, `test_parse`, `sample_issues`) | one or more `SELECT … FROM records` each | no |

`db_check.py` also looks up columns with `information_schema.columns WHERE table_name = 'records'` without a schema
filter.

## Which scripts are fine

| Script | Why |
| :-- | :-- |
| `scripts/url/apply_source_urls.py`, `fix_source_urls_unescape.py`, `add_source_distributions.py`, `add_missing_distributions.py` | written 2026-10-07 with `validate.records` / `validate.distributions` spelled out |
| `scripts/url/compare_url_accessurl.py` | auto-detects the schema, honours `DB_SCHEMA` |
| `scripts/url/check_urls.py`, `check_urls_source.py`, `url_check_ui.py`, `url_rules.py`, `test_url_rules.py` | no direct table access (they read CSV files or use the compare script's loader) |
| `crosscheck/*.py` | take `--schema`, default `DB_SCHEMA` from `.env` |

## What this means in practice

* **Read-only scripts** just fail with the error above — harmless.
* **Writers fail before writing anything** (the first statement errors), so nothing is half-applied. Do not "fix"
  that by creating a `public.records` view or table: the scripts would then silently read/write the wrong copy.
* **Do not re-run `build_url_column.py --apply`.** The `url` column already exists in `validate.records` and is
  filled (23,139 of 25,129 rows); its `ALTER TABLE … ADD COLUMN` is idempotent but its fill logic predates the
  2026-10-07 source-catalogue pass and the `distributions` inserts. Use the 2026-10-07 writers instead.
* The cleanup (v4) and abstract-from-title passes are **already applied** (2026-10-01); there is nothing to re-run.

## If someone wants to fix it later (not done)

Smallest options, in order of preference — pick one, do not mix:

1. Set the schema once per connection, right after `psycopg2.connect(...)`:
   `cur.execute("SET search_path TO " + os.getenv("DB_SCHEMA", "public") + ", public")`
   (every script has its own `connect`, except the `url/` ones that import it from `build_url_column.py`).
2. Pass it in the connection: `options="-c search_path=validate,public"`.
3. Qualify every table name (`validate.records`) — most explicit, most edits, and hard-codes the schema.

After any change: run the read-only scripts first (`db_check.py`, `find_messy.py`), then a dry run of a writer
(they all default to dry run), and compare its counts with the reports in `docs/` before using `--apply`.
