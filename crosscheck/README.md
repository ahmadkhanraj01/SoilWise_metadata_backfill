# crosscheck/ — Sep 2026 title / abstract backfill (phase 1, closed)

Kept in place (not restructured) because the scripts, caches and `.env` are referenced from everywhere.

| File(s) | What |
| :-- | :-- |
| `.env` | **DB credentials for the whole project** (never commit). `DB_SCHEMA=validate` since 2026-10-07 |
| `Sep8_SolrData.json` | the Sep 8 Solr export used as reference (130 MB) |
| `check_untitled.py`, `check_missing.py` | find records without title / with missing fields vs the Solr export → `untitled_crosscheck.csv`, `<field>_crosscheck.csv` (e.g. `abstract_crosscheck.csv`) |
| `apply_titles.py`, `apply_field.py` | write recovered titles / fields back |
| `fetch_sources.py` | scrape publisher/source pages for abstracts → `source_fetch_cache.json`, `source_abstract_crosscheck.csv`, `source_abstracts.sql` |
| `apply_abstracts.py` | reads `source_abstract_crosscheck.csv` and writes the verified abstracts; `exclusion_list.*`, `unverified_exclusion_list.json`, `title_mismatch_manual_recover.json` are the review decisions |
| `outside_scope_*`, `*.sql` | out-of-scope rows and the generated UPDATE scripts |
| `soilwise_metadata_backfill_report.md` | the first backfill report |

Narrative and decisions: [`../docs/backfill/handover.md`](../docs/backfill/handover.md).
These scripts take `--schema` (default `DB_SCHEMA` from `.env`).
