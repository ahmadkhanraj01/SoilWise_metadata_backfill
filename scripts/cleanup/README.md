# scripts/cleanup/ — messy-metadata scan, cleanup & apply

Tooling behind `docs/cleanup/messy_data_report.md` and `docs/cleanup/messy_data_cleanup_report.md`:
find malformed free text in the SoilWise `records` table, produce a reviewable
before/after diff of the cleanup, and (once signed off) apply it.

> **Moved 2026-10-08:** these scripts used to sit in the flat `scripts/` folder (see the root
> `README.md` for the old → new table). Empty-abstract backfill: `../abstracts/`; one-off
> inspectors: `../diagnostics/`; `records.url` / `distributions` work: `../url/`.
> **Known issue:** the DB moved from `public` to schema **`validate`** (2026-10-07). These scripts
> query an unqualified `records`, so against the current database they need `search_path` set to
> `validate` (or `validate.` added to the SQL) before they will run again.

> **Safety:** `find_messy.py`, `dry_run.py`, `analyse_diff.py` and `verify_diff.py`
> are **read-only** (they only `SELECT`). `apply_cleanup.py` is the only writer: it
> refuses to run without `--apply`, checks the live DB against the CSV first, takes
> a before-image backup, asserts row counts, and rolls back on mismatch.

---

## Status (2026-09-30, v4 run — done, verified)

| | v1 (scratchpad) | v2 | v3 (+pipe rule) | **v4 (+emoji rule)** |
| :--- | ---: | ---: | ---: | ---: |
| records scanned | 25,130 | 25,130 | 25,130 | 25,130 |
| records changed (dry run) | 6,412 | 6,532 | 6,533 | **6,538** |
| field-level change rows | 7,246 | 9,083 | 9,085 | **9,090** |
| residual HTML tags after cleaning | 38 | 0 | 0 | **0** |
| top-level `title`/`abstract` cleaned | ❌ never | ✅ | ✅ | ✅ |
| `target` per row | ❌ | ✅ | ✅ | ✅ `both` 2,503 / `column` 1,956 / `raw_metadata` 4,631 |
| `verify_diff.py` | n/a | PASS | PASS | **PASS** |

Artifacts on disk (earlier versions left untouched), all under `output/`:
`output/scan/messy_records_v2.csv`, `output/dryrun/messy_cleanup_v4_{dryrun,before_after,changes,summary}.csv`,
`output/logs/v4_run.log`; apply-time files in `output/apply/*_backup.jsonl` and
`output/logs/apply_v4.log` (see `../../docs/cleanup/messy_cleanup_applied_report.md`).

All 9,090 changes are classified: html_tag 3,702 · multi_space 3,254 ·
entity_decoded 1,286 · nfc 465 · whitespace_only 157 · placeholder_drop 112 ·
mojibake 60 · stray_quote 21 · emoji_removed 11 · bom_removed 10 · linebreak 10 ·
pipe_strip 2 · **unexplained 0**. (Primary label only; rows can carry several.)

What v3 and v4 added over v2:

* **Edge pipes (v3):** +2 rows / +1 record — `Name||` in `contact_persons` and
  `view_contacts` of one record. Whole-value `"|"` placeholders were already
  dropped in v2, so the rule changes little on the current DB.
* **Emoji (v4):** 15 rows / 10 records (14 abstract, 1 title) lose emoji. The
  report's 75 was an over-broad count (its regex also matched arrows/geometric
  shapes); real pictographs are few. Characters removed: ✔ ×25, 🧠 ×6, 🔹 ×5,
  🤖 ×3, and singles/pairs of 📎 📘 📂 🗺 ✅ ⚠ 👇 📽 🌾 🎥 📍 🔗. ✔/✅/⚠ are dingbats
  rather than emoji proper — review those rows if the symbols carry meaning.

Reconciliation with v1 (unchanged): 7,132 v2 rows touch `raw_metadata`, plus the
114 rows v1 would have used to fabricate new jsonb keys = 7,246 = v1's total.

---

## Files

### Core

| File | What it does | Writes |
| :--- | :--- | :--- |
| `cleanup_rules.py` | Shared regexes + cleaning functions. Imported by the scan, the dry run and the verifier so **detected == fixable** (v1 had two diverging copies of the tag regex). No DB or file I/O. | — |
| `find_messy.py` | Scan: flags records containing emoji, pipes, backslashes, control chars, mojibake, HTML tags, URL-encoding or irregular spacing. | `output/scan/messy_records.csv` (path via `--out`) |
| `dry_run.py` | Dry run: applies the rules and emits diffs, **never** an UPDATE. | `<prefix>_dryrun.csv`, `<prefix>_before_after.csv` |
| `analyse_diff.py` | Classify every change (html_tag / multi_space / entity_decoded / nfc / placeholder_drop / mojibake / stray_quote / bom_removed / linebreak / pipe_strip / emoji_removed / whitespace_only) and emit a review-friendly CSV. | `<prefix>_changes.csv`, `<prefix>_summary.csv` |
| `verify_diff.py` | Gate: residual tags/mojibake/spacing, invalid targets, token-level content loss. Exit 1 = needs review. | console only |
| `apply_cleanup.py` | **The only writer** for the cleanup pass: applies a signed-off `*_before_after.csv`: staleness check against the live DB, before-image backup, per-target `UPDATE`/`jsonb_set`, row-count assertion, rollback on mismatch; `--restore` writes a backup back. | `output/apply/*_backup.jsonl`, DB |


---

## Quick start

```bash
cd "SoilWise metadata backfill"   # run everything from the project root

# 1. flag records (read-only)
python3 scripts/cleanup/find_messy.py --out output/scan/messy_records_v3.csv

# 2. produce the diff (read-only; add --limit 500 for a smoke test)
python3 scripts/cleanup/dry_run.py --out-prefix output/dryrun/messy_cleanup_v5

# 3. classify every change + build the review CSV (before/after per field)
python3 scripts/cleanup/analyse_diff.py output/dryrun/messy_cleanup_v5_before_after.csv

# 4. gate before reviewing/applying
python3 scripts/cleanup/verify_diff.py output/dryrun/messy_cleanup_v5_before_after.csv

# 5. apply (checks + backup + counts; only writes with --apply)
python3 scripts/cleanup/apply_cleanup.py output/dryrun/messy_cleanup_v5_before_after.csv
python3 scripts/cleanup/apply_cleanup.py output/dryrun/messy_cleanup_v5_before_after.csv --apply

```

DB credentials come from `crosscheck/.env`
(`DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD/DB_SSLMODE/DB_CONNECT_TIMEOUT`);
override with `--env`. Dependencies: `psycopg2`, `python-dotenv`, `ftfy` (6.x).

### Outputs

| File (defaults land in `output/scan/` and `output/dryrun/`) | Grain |
| :--- | :--- |
| `<prefix>_dryrun.csv` | one row per changed record: `id, identifier, changes` (JSON list of `{target, field, before, after}`) |
| `<prefix>_before_after.csv` | one row per change: `id, identifier, target, field, before, after` — **the sign-off artifact** |
| `<prefix>_changes.csv` | the same rows **plus `change_types` + `primary`** — the before/after-per-field CSV to review in a spreadsheet |
| `<prefix>_summary.csv` | aggregated: `field, target, change_type, rows, records` |

`target` says where the cleaned value belongs:

| target | meaning |
| :--- | :--- |
| `both` | the column and `raw_metadata` hold the *identical* value → one row covers both writes |
| `column` | top-level `records.title` / `records.abstract` only |
| `raw_metadata` | an existing jsonb key only |

---

## What is cleaned, and what is deliberately left alone

**Cleaned** (in this order, only where a value genuinely changes):

1. HTML/XML tags in `title`, `abstract`, `subjects` → replaced with a space (so
   `AbstractUndisturbed` → `Abstract Undisturbed`), **stripped twice** — see fix #1.
2. Stray backslash-quotes (`\"`) in plain text.
3. Mojibake via `ftfy` (quote-curling/width/ligature fixes disabled so only real
   encoding corruption is touched), with a gated CP1252 fallback for rare cases.
   Side effects that come with it and are intended: HTML-entity decoding
   (`&amp;`→`&`, `&ndash;`→`–`) and NFC normalization (`Miličić`).
4. Literal `"|"` / `""` placeholder entries dropped from
   `persons / organizations / contact_persons / contact_organizations / matched_subjects`
   and `"person"`/`"organization"` cleared inside `view_authors` / `view_contacts`.
5. Multi-space collapsed to a single space.
6. **Emoji removed** (`strip_emoji`, all cleaned fields, applied first): emoji,
   pictographs, dingbats and flags plus a trailing variation selector/ZWJ are
   deleted; the spacing left behind is collapsed by step 5. (v4)
7. **Edge pipes stripped** from `persons / organizations / contact_persons /
   contact_organizations / matched_subjects` items and from `person` /
   `organization` inside `view_authors` / `view_contacts`
   (`Name||` → `Name`, `|Name||` → `Name`); an item that is nothing but pipes is
   dropped. Inner pipes (`A | B`) are kept. (v3)

**Not cleaned:** pipes inside `subjects`/`title`/`abstract` body text (intentional
separators, e.g. `"PREPSOIL Interview | Niels Halberg"`), LaTeX/R/markdown
backslashes in abstracts, `%xx` inside real URLs, and the two one-off corruption
cases (the `S\xF6derstr\xF6m` name and the stray `\x7f` characters) — those need
a human call. Fields scanned but out of scope: `license`, `soilmission`,
`projects`, `project_acronyms`, `sources`.

---

## Fixes vs. the v1 scratchpad scripts

The v1 scripts lived in a `/tmp` scratchpad that has since been wiped; they are
superseded by this folder. Full write-up: `../../docs/cleanup/cleanup_scripts_review.md`.

1. **Tags no longer come back.** v1 stripped tags, *then* ran ftfy — ftfy's
   `unescape_html="auto"` only protects entities when a literal `<` is present, so
   `<jats:p>&amp;lt;p&amp;gt;` decoded into a fresh `<p>` (9 titles + 29 abstracts
   still tagged). Now tags are stripped again after ftfy.
2. **Top-level columns are cleaned too.** v1 only ever built a new `raw_metadata`,
   so ~3,436 dirty values in `records.title` / `records.abstract` would have been
   left behind. Each copy is now cleaned **independently** — they are never synced,
   because ~7k abstracts differ (backfilled abstracts exist only in the column).
3. **The diff says where each change goes.** v1 had no `target` column and would
   have silently *created* 114 new jsonb keys. Keys are never created now; every
   row carries `target = both | column | raw_metadata`.
4. **Scanner and cleaner share one set of patterns** (`cleanup_rules.py`); the emoji
   regex no longer flags plain arrows/geometric symbols; the multi-space pattern
   matches on tab+space too.
5. **`clean_json_list` guards against non-object JSON**, and dropping an empty-string
   placeholder now counts as a change instead of being silently discarded.

---

## Applying (done for v4)

`apply_cleanup.py` implements the checklist: staleness check against the live DB,
fresh before-image backup (`output/apply/<prefix>_backup.jsonl`), one transaction
with per-target `SET title/abstract = %s` / `jsonb_set(...)`, row-count assertion
against the CSV (`--expect-records` / `--expect-rows`), `COMMIT` only on match, and
`--restore BACKUP.jsonl --apply` to undo. The v4 pass was applied on 2026-10-01 —
status and rollback instructions: `../../docs/cleanup/messy_cleanup_applied_report.md`.
Post-apply re-scan: `output/scan/messy_records_postapply.csv`.
