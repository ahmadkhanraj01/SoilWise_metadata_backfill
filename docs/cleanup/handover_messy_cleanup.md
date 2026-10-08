# Handover — messy-metadata cleanup (dry run)

> **Note 2026-10-08:** historical. The apply script was written and the v4 diff applied on 2026-10-01 (`messy_cleanup_applied_report.md`); scripts now live in `scripts/cleanup/` (paths below were rewritten).

**Date written:** 2026-09-30
**Working dir:** `/home/ahmad/K2X/SoilWise metadata backfill`
**Status:** **Dry run done and verified (v4, `verify_diff.py` PASS). Nothing has been written to the
database. The apply script does not exist yet and must not be written until the before/after CSV is
signed off.**
**Sibling handover:** `handover.md` covers the earlier *abstract backfill* — separate piece of work.

---

## 1. The task

`messy_data_report.md` (§4 "Messy / Malformed Text Data") lists malformed free text found in
`Sep25_solrData.json`: HTML/JATS tags, multi-spaces, pipes, backslashes, emoji, control chars,
mojibake, URL-encoding. The job: find those in the SoilWise `records` table (`msp-kh-dev`) and
produce a reviewable before/after diff of a proposed cleanup. Everything in `scripts/` is
**read-only** (`SELECT` only).

## 2. Where it stands

| | v1 (scratchpad, gone) | v2 | v3 (+pipe rule) | **v4 (+emoji rule)** |
| :--- | ---: | ---: | ---: | ---: |
| records changed | 6,412 | 6,532 | 6,533 | **6,538** |
| field-level change rows | 7,246 | 9,083 | 9,085 | **9,090** |
| residual HTML tags | 38 | 0 | 0 | 0 |
| `verify_diff.py` | n/a | PASS | PASS | **PASS** |

v4 = current. Its 9,090 rows: `raw_metadata` 4,631 · `both` 2,503 · `column` 1,956. Primary change
types: html_tag 3,702 · multi_space 3,254 · entity_decoded 1,286 · nfc 465 · whitespace_only 157 ·
placeholder_drop 112 · mojibake 60 · stray_quote 21 · emoji_removed 11 · bom_removed 10 ·
linebreak 10 · pipe_strip 2 · **unexplained 0**.

Sign-off artifact: `output/dryrun/messy_cleanup_v4_before_after.csv` (one row per change:
`id, identifier, target, field, before, after`). Review-friendly version with change types:
`output/dryrun/messy_cleanup_v4_changes.csv`. Aggregate: `output/dryrun/messy_cleanup_v4_summary.csv`.
Run log: `output/logs/v4_run.log`.
v1/v2/v3 files are left untouched in `output/dryrun/` (`messy_cleanup_*`, `_v2_*`, `_v3_*`).
*(Paths are post-2026-10-01 restructure — see `../README.md`.)*

## 3. What was built (`scripts/`)

| File | Role |
| :--- | :--- |
| `cleanup_rules.py` | Shared regexes + cleaning functions — single source of truth so *detected == fixable*. No DB/file I/O. |
| `find_messy.py` | Scan: flags records with emoji, pipes, backslashes, control chars, mojibake, tags, URL-encoding, multi-space → `output/scan/messy_records*.csv`. |
| `apply_cleanup.py` | Applies a signed-off `*_before_after.csv` (backup + counts + rollback) → `output/apply/*_backup.jsonl`. |
| `dry_run.py` | Applies the rules, emits diffs, never an UPDATE. |
| `analyse_diff.py` | Classifies every change and builds `_changes.csv` / `_summary.csv`. |
| `verify_diff.py` | Gate: residual tags/mojibake/spacing, invalid targets, token-level content loss. Exit 1 = needs review. |
| `db_check*.py`, `inspect_*.py`, `sample_issues.py`, `test_parse.py` | One-off inspection helpers. |

Full README: `scripts/README.md`. Deeper history of the v1 bugs: `cleanup_scripts_review.md`.

### Rules applied (in order, only where a value actually changes)

1. HTML/XML tags in `title`/`abstract`/`subjects` → replaced with a space, **stripped twice** (once
   before ftfy, once after — ftfy's entity unescaping can rebuild `<p>` from `&amp;lt;p&amp;gt;`).
2. Emoji removed (`strip_emoji`, applied first in `clean_plain_text`, all cleaned fields). *(v4)*
3. Stray `\"` / `\'` → plain quote.
4. Mojibake via `ftfy` (quote-curling/width/ligatures off) + gated CP1252 fallback. Intended side
   effects: HTML-entity decoding and NFC normalization.
5. Placeholder `"|"` / `""` list entries dropped from `persons / organizations / contact_persons /
   contact_organizations / matched_subjects`; cleared inside `view_authors` / `view_contacts`.
6. Edge pipes stripped from names (`Name||` → `Name`, `|Name||` → `Name`); item dropped if only
   pipes. Inner pipes (`A | B`) kept. *(v3)*
7. Multi-space collapsed.

### `target` column — where each cleaned value goes

`both` = column and `raw_metadata` hold the identical value (one row, two writes) · `column` =
top-level `records.title`/`records.abstract` only · `raw_metadata` = an existing jsonb key only.
The two copies are cleaned **independently, never synced** (~7k abstracts legitimately differ —
backfilled abstracts exist only in the column). jsonb keys are **never created**.

## 4. What is deliberately NOT cleaned

* Pipes in `subjects` / `title` / `abstract` body text (intentional separators, e.g.
  `"PREPSOIL Interview | Niels Halberg"`).
* LaTeX / R / markdown backslashes in abstracts (e.g. `LRB17\1019`).
* `%xx` inside real URLs / abstract prose.
* The two one-off corruption cases (`S\xF6derstr\xF6m` name, stray `\x7f`) — need a human call.
* `license`, `soilmission`, `projects`, `project_acronyms`, `sources` — scanned, not cleaned.

## 5. Things that look like gaps but aren't (don't re-litigate)

* **Emoji "75 records" in the report vs 10 fixed.** The report's regex also matched arrows and
  geometric shapes. Real pictographs: 15 rows / 10 records (14 abstract, 1 title). Removed: ✔ ×25,
  🧠 ×6, 🔹 ×5, 🤖 ×3, plus singles/pairs of 📎 📘 📂 🗺 ✅ ⚠ 👇 📽 🌾 🎥 📍 🔗.
* **Pipe rule added only +2 rows / +1 record.** Whole-value `"|"` placeholders were already dropped
  in v2; the only partial-pipe case in the DB is `Anadon-Rosell, Alba||` /
  `Bartrons Vilamala, Mireia||` in record `50|RECOLECTA___::144be9704061041da9cb731fa0d89a51`.
* **Report counts (40–42 pipe records, 7 mojibake persons) ≠ v2/v3/v4 counts** (≈26–28 placeholder
  drops; mojibake in `contact_persons`/`view_contacts` only, 2 each). The report was built from the
  Sep25 Solr JSON, the scripts read the live Postgres table — the datasets differ. Not verified
  further.
* **`analyse_diff.py` labels overlap:** ~90 rows are `placeholder_drop;pipe_strip` because any row
  that lost a pipe gets `pipe_strip`. Only 2 rows have `pipe_strip` as *primary*.

## 6. Open decisions / caveats for the reviewer

1. **✔ / ✅ / ⚠ are dingbats, not true emoji** (U+2600–27BF is in the emoji pattern). They are most
   of the removed characters — check those rows in `_changes.csv` if the symbols carry meaning.
2. `verify_diff.py` reports 1 pre-existing mojibake marker left alone (`Ângela` — legitimate name).
3. Sign-off is still pending; suggested review order: the small types first (mojibake 60,
   stray_quote 21, emoji_removed 11, pipe_strip 2, placeholder_drop 112), then a random sample of
   html_tag / multi_space / entity_decoded / nfc.

## 7. Next step — apply script (NOT written)

Build from `output/dryrun/messy_cleanup_v4_before_after.csv`, following the pattern of `crosscheck/apply_field.py`
/ `apply_titles.py` (dry-run by default, explicit commit):

1. Fresh DB backup immediately before.
2. One transaction; per-target `UPDATE`: `SET title/abstract = %s` for `column`; `jsonb_set` for
   `raw_metadata`; both statements for `both`.
3. Assert affected-row counts against the CSV; `COMMIT` only if they match.
4. Post-checks: re-run `find_messy.py` (expect the deliberately-kept items only) and
   `verify_diff.py`.

Checklist lives in `cleanup_scripts_review.md` §7.

## 8. Running it

```bash
cd "/home/ahmad/K2X/SoilWise metadata backfill"
python3 scripts/cleanup/find_messy.py --out output/scan/messy_records_v4.csv
python3 scripts/cleanup/dry_run.py --out-prefix output/dryrun/messy_cleanup_v5   # add --limit 500 to smoke-test
python3 scripts/cleanup/analyse_diff.py output/dryrun/messy_cleanup_v5_before_after.csv
python3 scripts/cleanup/verify_diff.py  output/dryrun/messy_cleanup_v5_before_after.csv
```

Gotchas:

* DB creds from `crosscheck/.env`; deps `psycopg2`, `python-dotenv`, `ftfy` 6.x.
* **The full dry run takes ~20 min** (a `--limit 500` run still takes ~1 min): almost all wall-clock
  is DB latency (server-side cursor startup/streaming), not CPU. Run it in the background with
  output to a log file, not piped through `tail` — piping hides progress and looks hung.
* Scripts refuse to overwrite existing outputs; use a new `--out-prefix` (or `--force`).
* `pkill -f "scripts/cleanup/dry_run.py"` matches your own shell if the pattern is in its command line
  (killed a session once) — use `pgrep`/PID or `pkill -f "[d]ry_run.py"`.

## 9. Files

| Path | What |
| :--- | :--- |
| `messy_data_report.md` | The original findings this work answers. |
| `messy_data_cleanup_report.md` | Earlier (v1-era) cleanup write-up. |
| `cleanup_scripts_review.md` | Review of the v1 scripts: bugs, fixes, apply TODO. |
| `scripts/README.md` | Script docs + version status table. |
| `output/dryrun/messy_cleanup_v4_{dryrun,before_after,changes,summary}.csv` | Current diff (sign-off artifact = `_before_after`). |
| `output/scan/messy_records_v2.csv` | Latest pre-apply scan output (flagged records + issues). |
| `output/scan/messy_records_postapply.csv` | Post-apply scan (what still remains). |
| `output/apply/messy_cleanup_v4_backup_run2.jsonl` | Before-image backup of the applied pass (undo source). |
| `output/logs/v4_run.log` | Log of the v4 pipeline run. |
