# data/ — inputs and extracts

Read-only inputs / snapshots; nothing here is produced by the pipelines in `scripts/` except where noted.

| File | What it is | Rows |
| :-- | :-- | --: |
| `records_202609231630_empty_abstract.csv` | 2026-09-23 snapshot of records with no abstract | — |
| `source_no_abstract_1930.csv` | records whose source gave no abstract (Sep 25 source recovery) | 1,930 |
| `empty_abstracts_2133.csv` | the 2,133 empty/NULL abstracts found 2026-10-01 (all later filled from title) | 2,133 |
| `empty_urls_2026-10-07.csv` | export of the 3,552 `records` with an empty `url` (all columns, 175 MB) — input of the 2026-10-07 source-catalogue pass. Was `empty urls.csv` in the project root | 3,552 |

**Caveat:** an editor CSV-aligner plugin padded `empty_urls_2026-10-07.csv` with spaces (that is why it is 175 MB).
Read it with whitespace stripped (`csv.reader(..., skipinitialspace=True)` then `.strip()` every cell). Turn the plugin off
for `data/` and `output/`.
