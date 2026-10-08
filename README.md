# SoilWise metadata backfill

Working folder for the SoilWise metadata clean-up and backfill: title/abstract crosscheck (Sep 2026), messy-text
cleanup, empty-abstract backfill, the `records.url` column and `distributions` rows. Database:
`msp-kh-dev`, schema **`validate`** (it was `public` until 2026-10-07). Credentials: `crosscheck/.env` (never commit).

> Last restructure: **2026-10-08** (scripts and docs grouped by job; see the old → new table at the bottom).
> Earlier restructure: 2026-10-01. Nothing is ever deleted when restructuring.

## Layout

```
README.md            this file: layout, pipelines, timeline, old -> new paths
crosscheck/          phase 1 (Sep backfill): scripts, caches, Sep8_SolrData.json, .env, report   -> crosscheck/README.md
data/                inputs and extracts (empty-abstract lists, empty-url export)               -> data/README.md
docs/                reports, plans, handovers, grouped by topic                                -> docs/README.md
  backfill/ cleanup/ abstracts/ url/
scripts/             all tooling, grouped by job                                                -> scripts/README.md
  cleanup/           messy-text scan -> dry run -> verify -> apply
  abstracts/         empty abstract <- title
  url/               records.url, source-catalogue pass, URL checks, distributions rows
  diagnostics/       one-off read-only inspectors
output/              generated artifacts + undo material (before-images, inserted ids), logs   -> output/README.md
.claude/             tooling state (not project data)
```

Each folder has its own README; start at the one for the job you are doing.

## Pipelines (run from this folder; every writer is dry-run unless `--apply`)

```
# messy-text cleanup            (scripts/cleanup/README.md)
python3 scripts/cleanup/find_messy.py
python3 scripts/cleanup/dry_run.py --out-prefix output/dryrun/messy_cleanup_v5
python3 scripts/cleanup/analyse_diff.py output/dryrun/messy_cleanup_v5_before_after.csv
python3 scripts/cleanup/verify_diff.py  output/dryrun/messy_cleanup_v5_before_after.csv
python3 scripts/cleanup/apply_cleanup.py output/dryrun/messy_cleanup_v5_before_after.csv [--apply]

# empty abstract <- title       (scripts/abstracts/README.md)
python3 scripts/abstracts/fill_abstract_from_title.py [--apply]

# records.url + distributions   (scripts/url/README.md, docs/url/url_table_plan.md)
python3 scripts/url/test_url_rules.py
python3 scripts/url/find_urls_external.py
python3 scripts/url/build_url_column.py --candidates output/url/url_candidates_DE.csv [--apply]
python3 scripts/url/apply_source_urls.py [--apply]
python3 scripts/url/add_source_distributions.py [--apply]
python3 scripts/url/add_missing_distributions.py [--apply]
```

**Known issue:** since the move to schema `validate` only the newest `scripts/url/*` writers and `compare_url_accessurl.py`
work against the database as is; the older scripts query an unqualified `records` and need `search_path = validate`
(details: `scripts/README.md`).

## Current state (2026-10-08)

| | |
| :-- | --: |
| records in `validate.records` | 25,129 |
| with a `url` | 23,139 (92.1%) |
| still without `url` | 1,990 (FAO 1,012 · BONARES 102 · INSPIRE ~300 · OpenAIRE 225 · misc.) |
| `distributions` rows | 46,702 (every `records.url` is also a distribution) |

## Timeline

### Phase 1 — metadata backfill (`crosscheck/`)

| When | What |
| :--- | :--- |
| **2026-09-11** | Pulled the Sep 8 Solr export (`crosscheck/Sep8_SolrData.json`); found untitled records (`check_untitled.py`) and missing fields (`check_missing.py`); wrote recovered titles/fields back with `apply_titles.py` / `apply_field.py`. First report: `crosscheck/soilwise_metadata_backfill_report.md`. |
| **2026-09-23** | Snapshot of records with no abstract: `data/records_202609231630_empty_abstract.csv`. |
| **2026-09-25** | Source-based abstract recovery: `fetch_sources.py` scraped publisher/source pages (cache + `source_abstract_crosscheck.csv`), built exclusion lists for Copernicus review-comment threads and site-tagline false positives, wrote the verified abstracts with `apply_abstracts.py`; outside-scope crosscheck done; DB credentials added as `crosscheck/.env`. Phase closed by `docs/backfill/handover.md`. |

### Phase 2 — messy-metadata cleanup (`scripts/`, `output/`)

| When | What |
| :--- | :--- |
| **2026-09-30 morning** | Scanned the Sep 25 export for 8 issue types (emoji, pipes, backslashes, control chars, mojibake, HTML tags, URL-encoding, multi-space) → `docs/cleanup/messy_data_report.md`. Ran the v1 scratchpad dry run (6,412 records / 7,246 rows) → `docs/cleanup/messy_data_cleanup_report.md`. |
| **2026-09-30 afternoon** | Reviewed the v1 scripts — 3 bugs found, written up in `docs/cleanup/cleanup_scripts_review.md` — and rebuilt them as `scripts/` with a shared `cleanup_rules.py` (detected == fixable) + `find_messy` / `dry_run` / `analyse_diff` / `verify_diff`. Dry runs: **v2** 6,532 / 9,083 (verify PASS, 0 unexplained changes), **v3** +edge-pipe rule 6,533 / 9,085, **v4** +emoji rule **6,538 / 9,090**. Handover: `docs/cleanup/handover_messy_cleanup.md`. |
| **2026-10-01 morning** | Wrote `scripts/cleanup/apply_cleanup.py` (staleness check → before-image backup → transactional per-target `UPDATE` → row-count assertion → rollback) and **applied the signed-off v4 diff**; first attempt was killed and re-run (`_run2` backup kept). Post-apply scan → `output/scan/messy_records_postapply.csv`; write-up → `docs/cleanup/messy_cleanup_applied_report.md` (undo instructions included). |
| **2026-10-01 late morning** | **Folder restructure:** reports → `docs/`, artifacts → `output/{scan,dryrun,apply,logs}`, extracts → `data/`; this README added; script defaults repointed at `output/`. Nothing deleted, `crosscheck/` untouched. |

### Phase 3 — empty-abstract backfill (`fill_abstract_from_title.py`)

| When | What |
| :--- | :--- |
| **2026-10-01 14:51** | Found the empty/NULL abstracts: **2,133** records (89 `NULL` + 2,044 `''`), none of which had a usable abstract hiding in `raw_metadata`. Export → `data/empty_abstracts_2133.csv`. |
| **2026-10-01 14:55** | Dry run + apply of `scripts/abstracts/fill_abstract_from_title.py`: copied each record's `title` into its `abstract` — **2,133 rows**, `abstract` column only (`raw_metadata`, `updated_at` untouched), before-image saved, count asserted. |
| **2026-10-01 15:01** | Previewed, then applied, the 31 placeholder abstracts (`xx` ×19, `x` ×4, `-` ×4, `xxx` ×2, `NA` ×1, `2022` ×1) with `--placeholders`. **0 empty/NULL/placeholder abstracts remain** in `public.records`. |
| **2026-10-01 15:05** | Write-up with every SQL statement: `docs/abstracts/abstract_from_title_backfill_report.md`. |

### Phase 4 — `records.url` column (`build_url_column.py`, `url_rules.py`, `check_urls.py`, `find_urls_external.py`)

| When | What |
| :--- | :--- |
| **2026-10-02 morning** | Read the harmonizer (`soilwise-he/md-harmonization`): its only write to `records` is an upsert with an explicit column list, so a new nullable column is safe. Decision: one nullable `url` column on `records`, no new table. |
| **2026-10-02 midday** | Wrote `scripts/url/url_rules.py` (identifier → type + URL; DOI/URL/arXiv/handle/URN rules, tiers A–C from `raw_metadata` and `oai:` handles) with `test_url_rules.py` (31 cases) and `build_url_column.py` (dry run, verify gate, transactional apply). Dry run: 18,910 → 21,363 records get a URL (85.0%). |
| **2026-10-02 afternoon** | `check_urls.py` HTTP-checked all 21,159 distinct URLs (66% ok, 24.5% blocked 401/403/429, 3.2% 4xx, 6.2% no answer — 1,018 of those are `maps.bonares.de`, unreachable from our network). `find_urls_external.py` added tiers D (OpenAIRE title+year match) and E (page containing the record title): 150 + 64 verified. |
| **2026-10-02 13:4x** | **Applied:** `ALTER TABLE records ADD COLUMN url text` + format CHECK; **21,577 urls written, 3,553 NULL**, count and every other column asserted unchanged. Before-image `output/url/url_before_image.csv`; undo `ALTER TABLE records DROP COLUMN url`. Write-up: `docs/url/url_table_plan.md`, `docs/url/url_check_report.md`. |
| **2026-10-07** | **Source-catalogue pass** on the 3,552 records still without a URL (`data/empty_urls_2026-10-07.csv`): looked each record up on its own source catalogue (ISRIC / EEA / islandr / INSPIRE GeoNetwork APIs, EJP SOIL GitHub page, OpenAIRE by id, data.europa.eu by id). **1,562 URLs written** (`scripts/url/apply_source_urls.py`); NULL 3,552 → 1,990. Post-checks: only `url` changed, nothing overwritten. HTTP check found 473 OpenAIRE URLs with a literal `&amp;` (400) → repaired (`fix_source_urls_unescape.py`) → 98.0% of the new URLs answer 2xx. Report: `docs/url/source_url_check_report.md`; method table: `docs/url/url_table_plan.md` §9. |
| **2026-10-07** | **DB schema moved** `public` → `validate` (a backup table `records_backup_ogc_types_20261007_152216` was created at 10:22 UTC; 7,216 rows differ from it in `resource_type`/`updated_at` only — not part of the URL work). |

### Phase 5 — `distributions` (2026-10-07)

| When | What |
| :--- | :--- |
| **2026-10-07** | Compared `records.url` with `distributions.access_url` (`scripts/url/compare_url_accessurl.py`, HTTP check of all 32,531 distinct URLs): `docs/url/url_vs_accessurl_report.md`. Draft fix plan (decode + dedupe): `docs/url/distributions_url_backfill_plan.md` — **not executed**. |
| **2026-10-07** | **Inserted 1,307 `distributions` rows** for the source-pass URLs (`add_source_distributions.py`; 255 were already present). Then **9,254 rows** so that every `records.url` has a matching distribution (`add_missing_distributions.py`): `landing page` / `text/html` (8,678) or `download` / `WWW:DOWNLOAD-1.0-http--download` (576). `access_url` percent-encoded like the 36,141 existing rows. Table: 36,141 → 46,702. Undo ids: `output/url/*_inserted_ids.csv`. |

### Phase 6 — restructure (2026-10-08)

Scripts → `scripts/{cleanup,abstracts,url,diagnostics}/`, docs → `docs/{backfill,cleanup,abstracts,url}/`,
`empty urls.csv` → `data/empty_urls_2026-10-07.csv`; path logic in the moved scripts updated, every reference in the
READMEs/docs rewritten, a README added per folder. Verified: all scripts compile, `test_url_rules.py` passes (31),
the DB-backed writers run from the new paths (dry runs report 0 rows left to write). A backup of the old tree was
taken before the move.


---

## Docs

Index with one line per document: [`docs/README.md`](docs/README.md).

## Old → new paths

Moved on **2026-10-08**:

| Old | New |
| :--- | :--- |
| `scripts/{cleanup_rules,find_messy,dry_run,analyse_diff,verify_diff,apply_cleanup}.py` | `scripts/cleanup/` |
| `scripts/fill_abstract_from_title.py` | `scripts/abstracts/` |
| `scripts/{url_rules,build_url_column,check_urls,check_urls_source,find_urls_external,test_url_rules,compare_url_accessurl,url_check_ui,apply_source_urls,fix_source_urls_unescape,add_source_distributions,add_missing_distributions}.py` | `scripts/url/` |
| `scripts/{db_check,db_check2,inspect_*,sample_issues,test_parse}.py` | `scripts/diagnostics/` |
| `scripts/README.md` (cleanup details) | `scripts/cleanup/README.md` (new `scripts/README.md` is an index) |
| `docs/handover.md` | `docs/backfill/` |
| `docs/{handover_messy_cleanup,messy_data_report,messy_data_cleanup_report,messy_cleanup_applied_report,cleanup_scripts_review}.md` | `docs/cleanup/` |
| `docs/abstract_from_title_backfill_report.md` | `docs/abstracts/` |
| `docs/{url_table_plan,url_check_report,source_url_check_report,url_vs_accessurl_plan,url_vs_accessurl_report,distributions_url_backfill_plan}.md` | `docs/url/` |
| `empty urls.csv` (project root) | `data/empty_urls_2026-10-07.csv` |

Moved on 2026-10-01 (still valid):

| Old location (project root) | New location |
| :--- | :--- |
| `messy_records*.csv` | `output/scan/` |
| `messy_cleanup*.csv`, `messy_cleanup_v{2,3,4}_*.csv` | `output/dryrun/` |
| `messy_cleanup_v4_backup*.jsonl` | `output/apply/` |
| `v3_run.log`, `v4_run.log`, `apply_v4.log`, `find_messy_post.log` | `output/logs/` |
| `handover*.md`, `messy_*report.md`, `cleanup_scripts_review.md` | `docs/` (then `docs/<topic>/` as above) |
| `source_no_abstract_1930.csv`, `records_*_empty_abstract.csv` | `data/` |

Older handovers and reports still show the paths of their day inside their text; use this table to resolve them.
