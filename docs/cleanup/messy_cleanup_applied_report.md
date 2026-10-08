# Messy-text cleanup: what was changed and what remains

**Applied:** 2026-10-01 to `msp-kh-dev`, table `public.records`
**Source diff:** `output/dryrun/messy_cleanup_v4_before_after.csv` (signed off: "clean it but make sure no data loss")
**Script:** `scripts/cleanup/apply_cleanup.py`

## 1. Result

| | |
| :--- | ---: |
| Records changed | **6,538** |
| Field-level changes | **9,090** |
| Committed | yes, one transaction, ~26 min |
| Audit before commit | all 6,538 records identical to before-image plus only the intended changes |
| Stale / missing records at write time | 0 / 0 |

### No-data-loss safeguards
- `verify_diff.py` passed first (no residual tags, no introduced mojibake, no token-level content loss).
- Each UPDATE was guarded on the live value still equalling the CSV `before` value, and had to hit exactly 1 row.
- Before-image of every touched record (title, abstract, raw_metadata): `output/apply/messy_cleanup_v4_backup_run2.jsonl`.
- No `raw_metadata` keys were created (`jsonb_set` with create_missing = false).
- `updated_at` was **not** changed.
- Undo: `python3 scripts/cleanup/apply_cleanup.py --restore output/apply/messy_cleanup_v4_backup_run2.jsonl --apply`

## 2. What was changed, by field

`column` = top-level `records.<field>`; `raw_metadata` = key inside the jsonb. Title and abstract are cleaned in both places, each copy independently.

| Field | Where | Changes | What was done (most common first) |
| :--- | :--- | ---: | :--- |
| `abstract` | column and/or raw_metadata | 7,541 rows over 5,833 records (column 1,942, raw_metadata 3,658, both 1,941) | multi-space collapsed; HTML/JATS tags removed; HTML entities decoded; emoji removed (14 abstracts) |
| `title` | column and/or raw_metadata | 576 (column 14, both 562) | multi-space; HTML tags; entities; whitespace-only; mojibake (5); emoji (1) |
| `subjects` | raw_metadata | 449 | Unicode NFC normalization (341); HTML tags (59); entities (26); multi-space (21) |
| `view_contacts` | raw_metadata | 225 | multi-space; NFC; placeholder `"\|"` cleared; entities; linebreaks |
| `contact_persons` | raw_metadata | 162 | NFC; multi-space; placeholder/edge pipes dropped; entities; stray quotes |
| `contact_organizations` | raw_metadata | 66 | multi-space; entities; linebreaks; placeholder pipes dropped |
| `view_authors` | raw_metadata | 35 | placeholder `"\|"` cleared; multi-space |
| `persons` | raw_metadata | 28 | placeholder `"\|"` entries dropped (26); multi-space |
| `organizations` | raw_metadata | 8 | multi-space; placeholder pipe dropped |
| **Total** | | **9,090** | |

By primary change type: html_tag 3,702 · multi_space 3,254 · entity_decoded 1,286 · nfc 465 · whitespace_only 157 · placeholder_drop 112 · mojibake 60 · stray_quote 21 · emoji_removed 11 · bom_removed 10 · linebreak 10 · pipe_strip 2.

Note: ✔ / ✅ / ⚠ are dingbats, not strict emoji, and make up most of the removed symbols.

## 3. What remains (post-apply scan, `output/scan/messy_records_postapply.csv`)

Scan of the live table: **25,130 records, 203 flagged** (was about 5,750 before). No HTML tags, multi-spaces, mojibake or control characters remain in the scanned fields.

| Field | Issue | Records | Status |
| :--- | :--- | ---: | :--- |
| `abstract` | pipe | 92 | kept on purpose: separators in body text |
| `view_contacts` | backslash | 47 | not reviewed; worth a quick look |
| `abstract` | backslash | 34 | kept on purpose: LaTeX/R/markdown |
| `subjects` | pipe | 14 | kept on purpose |
| `title` | pipe | 11 | kept on purpose (e.g. "PREPSOIL Interview \| Niels Halberg") |
| `abstract` | url_encoded | 8 | kept on purpose: `%xx` in real URLs/prose |
| `title` | backslash | 3 | kept |
| `subjects` | backslash | 1 | kept |
| `view_authors` | backslash | 1 | kept |
| `abstract` | emoji | 1 | **not cleaned**: record `ade2367a-d134-4c57-8272-5ea9fbeb54c2` (`10.5281/zenodo.16026838`); not in the applied diff, likely changed after the dry run or a symbol outside the rule |

### Deliberately not cleaned (unchanged from the plan)
- Pipes in `subjects` / `title` / `abstract` body text.
- LaTeX / R / markdown backslashes in abstracts.
- `%xx` inside real URLs and abstract prose.
- Two one-off corruption cases (`S\xF6derstr\xF6m` name, stray `\x7f`) needing a human decision.
- `license`, `soilmission`, `projects`, `project_acronyms`, `sources`: scanned, not cleaned.
- One pre-existing mojibake-looking marker (`Ângela`), a legitimate name.

## 4. Open items
1. Decide on the one remaining emoji record, and look at the 47 `view_contacts` backslashes.
2. If the Solr/indexing sync relies on `updated_at`, the changed records need it bumped (not done).
3. Optional: full dry run again (about 20 min) to confirm 0 pending changes.
4. Delete the duplicate `output/apply/messy_cleanup_v4_backup.jsonl` left by the killed first attempt (identical to `_run2`).
