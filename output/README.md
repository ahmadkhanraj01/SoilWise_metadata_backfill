# output/ — generated artifacts

Everything here is produced by `scripts/` (defaults point here). Safe to regenerate; the `apply/` and
`url/*before_image*` / `*inserted_ids*` files are the **undo material** — keep them.

| Folder | Produced by | Contents |
| :-- | :-- | :-- |
| `scan/` | `cleanup/find_messy.py` | `messy_records*.csv` — flagged records + issue types (`_postapply` = re-scan after the apply) |
| `dryrun/` | `cleanup/dry_run.py`, `analyse_diff.py` | `messy_cleanup_v{2,3,4}_{dryrun,before_after,changes,summary}.csv`, plus the v1 pair; **v4 is the one that was applied** |
| `apply/` | `cleanup/apply_cleanup.py`, `abstracts/fill_abstract_from_title.py` | before-image backups (`*_backup*.jsonl`) — restore with `--restore` |
| `logs/` | all | run logs (`v3_run.log`, `v4_run.log`, `apply_v4.log`, `check_urls*.log`, `check_source_urls*.log`, `find_urls_external.log`, `url_compare_refresh.log`, …) |
| `url/` | `scripts/url/` | see below |

## output/url/

| File | From | What |
| :-- | :-- | :-- |
| `url_preview.csv`, `url_preview_with_DE.csv` | `build_url_column.py` | rules-only classification (`record_id, identifier, identifier_type, url`) without / with verified D/E |
| `url_candidates_DE.csv` | `find_urls_external.py` | tier D/E search result per record with evidence |
| `url_status.csv`, `url_check_cache.csv`, `url_check_status.json` | `check_urls.py`, `url_check_ui.py` | HTTP status per record / per distinct URL (resume point) / UI snapshot |
| `url_before_image.csv` | `build_url_column.py --apply` | state before the 2026-10-02 write (undo: `DROP COLUMN url`) |
| `url_vs_dist_per_record.csv`, `url_vs_dist_urls.csv` | `compare_url_accessurl.py` | `records.url` vs `distributions.access_url`, per record / per URL |
| `source_url_check.csv`, `source_url_check_pass2.csv` | 2026-10-07 source pass | per record: source, candidate URL, check result (pass 1: catalogue API / GitHub / OpenAIRE; pass 2: OpenAIRE id match, data.europa.eu id/title) |
| `source_urls_preview.csv`, `source_url_status.csv`, `source_url_check_cache.csv` | `check_urls_source.py` | HTTP check of the 1,562 written URLs |
| `source_urls_before_image.csv` | `apply_source_urls.py` | the 1,562 rows written (id, url_before, url_new, method) |
| `source_urls_unescape_before_image.csv` | `fix_source_urls_unescape.py` | the 473 rows repaired (`&amp;` → `&`) |
| `source_distributions_inserted_ids.csv`, `missing_distributions_inserted_ids.csv` | `add_source_distributions.py`, `add_missing_distributions.py` | ids of the 1,307 + 9,254 `distributions` rows inserted (undo = delete these ids) |
