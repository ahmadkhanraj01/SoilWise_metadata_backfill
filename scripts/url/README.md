# scripts/url/ — `records.url`, source-catalogue URLs, URL checks, `distributions`

Everything about URLs. Design and numbers: [`../../docs/url/url_table_plan.md`](../../docs/url/url_table_plan.md).
Run from the project root. DB: `validate.records` / `validate.distributions` in `msp-kh-dev`.

## Files

### A. The `records.url` column (applied 2026-10-02)

| File | What it does | Writes DB |
| :-- | :-- | :-- |
| `url_rules.py` | the single rules module: `classify(identifier, raw_metadata) -> (type, url)` (DOI, URL, arXiv, handle, URN, `oai:` handle, MCF landing page, BONARES link) | no |
| `test_url_rules.py` | 31 plain-assert checks of the rules (`python3 scripts/url/test_url_rules.py`) | no |
| `build_url_column.py` | recon + classify + verify gate + preview CSV; `--candidates` merges verified tier D/E; `--apply` adds the column + format CHECK and fills it (fill-only, md5 of every other column asserted). Also exports `connect()`, `OUT_DIR`, `URL_FORMAT_RE` used by the later scripts | only with `--apply` |
| `find_urls_external.py` | tiers D (OpenAIRE title+year) and E (page link that contains the record title) → `output/url/url_candidates_DE.csv` | no |
| `check_urls.py` | HTTP HEAD (GET fallback) of every URL in a preview CSV, cached and resumable → `output/url/url_status.csv`, `docs/url/url_check_report.md` | no |

### B. Source-catalogue pass (applied 2026-10-07) — the records that were still NULL

| File | What it does | Writes DB |
| :-- | :-- | :-- |
| `apply_source_urls.py` | fills empty `url` from `output/url/source_url_check*.csv` (high-confidence methods only; fill-only, before-image `source_urls_before_image.csv`) | `--apply` |
| `fix_source_urls_unescape.py` | repairs `&amp;` in the URLs written by the step above (only rows still holding the written value; before-image `source_urls_unescape_before_image.csv`) | `--apply` |
| `check_urls_source.py` | `check_urls.py` writing `source_url_status.csv`, `source_url_check_cache.csv`, `docs/url/source_url_check_report.md` (duplicate, see `../README.md`) | no |

The search itself (per-source GeoNetwork API check, OpenAIRE `search=` by title with id match, data.europa.eu
search by id/title) was run from scratchpad scripts; its results are `output/url/source_url_check.csv` (pass 1) and
`source_url_check_pass2.csv` (pass 2). The method table is in `docs/url/url_table_plan.md` §9.

### C. `distributions` rows (applied 2026-10-07)

| File | What it does | Writes DB |
| :-- | :-- | :-- |
| `add_source_distributions.py` | one `distributions` row per record for the 1,562 source-pass URLs (name = source label, format `WWW:LINK-1.0-http--link`); skips a URL the record already has → 1,307 rows | `--apply` |
| `add_missing_distributions.py` | adds a row for every `records.url` not yet in the record's distributions: `landing page` / `text/html`, or `download` / `WWW:DOWNLOAD-1.0-http--download` for `downloadPublic` links → 9,254 rows; `access_url` percent-encoded like the existing rows | `--apply` |

Inserted ids are saved to `output/url/source_distributions_inserted_ids.csv` and
`missing_distributions_inserted_ids.csv`; undo = `DELETE FROM validate.distributions WHERE id IN (…)`.

### D. Comparison of `records.url` vs `distributions.access_url` (read-only)

| File | What it does |
| :-- | :-- |
| `compare_url_accessurl.py` | decodes + normalises both columns, classifies every record (`neither / url_only / dist_only / sets_equal / …`), `--http` adds HTTP status → `output/url/url_vs_dist_*.csv`, `docs/url/url_vs_accessurl_report.md`. Auto-detects the schema (`DB_SCHEMA`) |
| `url_check_ui.py` | the same HTTP check with a small local web UI (progress, pause/resume) at `http://127.0.0.1:8111`; appends to `output/url/url_check_cache.csv` |

## Run order

```bash
python3 scripts/url/test_url_rules.py
python3 scripts/url/find_urls_external.py                                   # tiers D+E  (~15 min)
python3 scripts/url/build_url_column.py --candidates output/url/url_candidates_DE.csv        # dry run + gate
python3 scripts/url/check_urls.py --preview output/url/url_preview.csv --workers 48 --per-host 12
python3 scripts/url/build_url_column.py --candidates output/url/url_candidates_DE.csv --apply

python3 scripts/url/apply_source_urls.py            # dry run; add --apply to write
python3 scripts/url/add_source_distributions.py     # dry run; add --apply to write
python3 scripts/url/add_missing_distributions.py    # dry run; add --apply to write
```

Re-running the 2026-10-07 writers now reports `0` rows to write (idempotent).

## Safety

Every writer: dry run by default · before-image / inserted-id list (refuses to overwrite it) · one transaction ·
count assertion · rollback on mismatch. `url` is the only column the `records` writers touch.

## Known issues

* `build_url_column.py` and `find_urls_external.py` use an unqualified `records` (schema is now `validate`) — see
  [`../README.md`](../README.md#known-issues-as-of-2026-10-08). The 2026-10-07 writers qualify `validate.records`.
* `docs/url/distributions_url_backfill_plan.md` (decode + dedupe `access_url`) is still a **draft** and partly
  superseded; see its status note.
