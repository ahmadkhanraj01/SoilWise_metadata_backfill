# docs/ — reports, plans and handovers

Grouped by topic. Older documents keep the file paths of their day inside the text; the old → new table in the
root [`README.md`](../README.md) resolves them. Status as of 2026-10-08.

## Known issues

| File | What |
| :-- | :-- |
| [`known_issues_schema_validate.md`](known_issues_schema_validate.md) | older scripts fail against schema `validate` (documented only, not fixed): affected scripts, safe ones, what not to re-run |

## backfill/ — Sep 2026 title / abstract backfill (closed)

| File | What | Status |
| :-- | :-- | :-- |
| [`backfill/handover.md`](backfill/handover.md) | handover of the source-based abstract recovery (runs #1–#3, review, exclusion lists, writes) | closed 2026-09-25 |
| [`../crosscheck/soilwise_metadata_backfill_report.md`](../crosscheck/soilwise_metadata_backfill_report.md) | first backfill report (Sep 11, Solr crosscheck) | closed |

## cleanup/ — messy-text cleanup

| File | What | Status |
| :-- | :-- | :-- |
| [`cleanup/messy_data_report.md`](cleanup/messy_data_report.md) | original scan of the Sep 25 export (8 issue types) | reference |
| [`cleanup/messy_data_cleanup_report.md`](cleanup/messy_data_cleanup_report.md) | rules + v1 dry-run numbers | superseded by v4 |
| [`cleanup/cleanup_scripts_review.md`](cleanup/cleanup_scripts_review.md) | review of the v1 scripts, 3 bugs, fix checklist | reference |
| [`cleanup/handover_messy_cleanup.md`](cleanup/handover_messy_cleanup.md) | handover of the dry-run phase (written before the apply script existed) | historical |
| [`cleanup/messy_cleanup_applied_report.md`](cleanup/messy_cleanup_applied_report.md) | what the v4 apply wrote, rollback, what remains | **applied 2026-10-01** |

## abstracts/

| File | What | Status |
| :-- | :-- | :-- |
| [`abstracts/abstract_from_title_backfill_report.md`](abstracts/abstract_from_title_backfill_report.md) | empty/placeholder abstracts ← title: every SQL statement, verification, undo | **applied 2026-10-01** |

## url/ — `records.url` and `distributions`

| File | What | Status |
| :-- | :-- | :-- |
| [`url/url_table_plan.md`](url/url_table_plan.md) | the `records.url` column: tiers, rules, DDL, scripts, run order, open items; **§9 = source-catalogue pass (2026-10-07)** | **applied** (2026-10-02 rules + D/E; 2026-10-07 source pass) |
| [`url/url_check_report.md`](url/url_check_report.md) | generated HTTP check of the 21,363 rule-derived URLs | generated 2026-10-02 |
| [`url/source_url_check_report.md`](url/source_url_check_report.md) | generated HTTP check of the 1,562 source-pass URLs (after the `&amp;` repair: 98.0% ok) | generated 2026-10-07 |
| [`url/url_vs_accessurl_plan.md`](url/url_vs_accessurl_plan.md) | plan to compare `records.url` with `distributions.access_url` | comparison **done**; fix part see next row |
| [`url/url_vs_accessurl_report.md`](url/url_vs_accessurl_report.md) | generated comparison + HTTP report (32,531 distinct URLs) | generated 2026-10-07; numbers are from **before** the 10,561 inserted `distributions` rows |
| [`url/distributions_url_backfill_plan.md`](url/distributions_url_backfill_plan.md) | draft: fill missing + dedupe + decode `access_url` | **draft, not executed; partly superseded** (the missing-URL part was done differently — see its status note) |

Generated reports (`*_check_report.md`, `url_vs_accessurl_report.md`) are overwritten by their scripts; edit the
script, not the report.
