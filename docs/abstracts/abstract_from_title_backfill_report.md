# Backfill report: `abstract` ← `title`

**Table:** `public.records` (25,130 rows) · **DB:** `msp-kh-dev`
**Date:** 2026-10-01 · **Script:** `scripts/abstracts/fill_abstract_from_title.py`

Every record whose abstract was empty, NULL or a placeholder got its **top-level
`records.abstract`** set to its own `records.title`. Nothing else was written —
`raw_metadata`, `title`, `updated_at` and all other columns are untouched.

---

## 1. Results

| pass | condition on `abstract` | rows written |
| :--- | :--- | ---: |
| 1 | `IS NULL` (89) or `= ''` (2,044) | **2,133** |
| 2 | placeholders `xx`(19) `x`(4) `-`(4) `xxx`(2) `NA`(1) `2022`(1) | **31** |
| | **total** | **2,164** |

Post-state: **0** rows with `abstract IS NULL`, `abstract = ''` or a placeholder
value. `raw_metadata` still holds the original values for all 2,164 (verified).

Nothing was skipped — all 2,164 records had a non-empty title.

---

## 2. SQL — finding the targets

Row counts by abstract state (run before any write):

```sql
-- overall breakdown
SELECT
  count(*) FILTER (WHERE abstract IS NULL)                              AS is_null,
  count(*) FILTER (WHERE abstract = '')                                 AS empty_str,
  count(*) FILTER (WHERE abstract IS NOT NULL AND abstract <> ''
                   AND btrim(abstract) = '')                            AS ws_only,
  count(*) FILTER (WHERE lower(btrim(abstract)) IN
                   ('null','none','nan','n/a','na','undefined','-'))    AS literal_word
FROM public.records;
-- is_null=89  empty_str=2044  ws_only=0  literal_word=5

-- the exact target set of pass 1 (this is what the UPDATE uses)
SELECT count(*) FROM public.records
WHERE abstract IS NULL OR abstract = '';
-- 2133

-- what the placeholder scan had left (candidates for pass 2)
SELECT abstract, count(*) FROM public.records
WHERE abstract IS NOT NULL AND abstract <> ''
  AND btrim(abstract) !~ '[[:alpha:]]{4,}'
GROUP BY abstract ORDER BY 2 DESC;
-- xx=19  x=4  -=4  xxx=2  NA=1  2022=1  (plus 'DOS-3212 A-B-C-D', legit)

-- confirm every target has a usable title (skipped would be 0)
SELECT
  count(*)                                                          AS targets,
  count(*) FILTER (WHERE title IS NULL)                             AS title_null,
  count(*) FILTER (WHERE title = '')                                AS title_empty,
  count(*) FILTER (WHERE title IS NOT NULL AND btrim(title) <> '')  AS title_ok
FROM public.records
WHERE abstract IS NULL OR abstract = '';
-- targets=2133  title_null=0  title_empty=0  title_ok=2133
```

Target set used by the script (`--placeholders` swaps in the second form):

```sql
-- pass 1
WHERE (abstract IS NULL OR abstract = '')
  AND title IS NOT NULL AND btrim(title) <> ''

-- pass 2  (placeholders bound as an array parameter)
WHERE (abstract IS NULL OR abstract = ''
       OR lower(btrim(abstract)) = ANY(%s))     -- {'xx','x','xxx','-','na','2022'}
  AND title IS NOT NULL AND btrim(title) <> ''
```

---

## 3. SQL — the write

Select-and-backup step (before-image, one JSONL line per record):

```sql
SELECT id, identifier, title, abstract, abstract IS NULL AS is_null
FROM public.records
WHERE (abstract IS NULL OR abstract = '')
  AND title IS NOT NULL AND btrim(title) <> ''
ORDER BY identifier;
```

The write itself — **one statement, one transaction**, `abstract` is the only
column assigned:

```sql
-- pass 1
UPDATE public.records
SET abstract = title
WHERE (abstract IS NULL OR abstract = '')
  AND title IS NOT NULL AND btrim(title) <> '';

-- pass 2
UPDATE public.records
SET abstract = title
WHERE (abstract IS NULL OR abstract = ''
       OR lower(btrim(abstract)) = ANY(%s))
  AND title IS NOT NULL AND btrim(title) <> '';
```

Guards around it (in `fill_abstract_from_title.py`):

* the `WHERE` is **re-evaluated live** at write time, so re-running is safe
  (already-filled rows fall out of the target set);
* `UPDATE` row count must equal the dry-run count (2,133 / 31) or the
  transaction is **rolled back**;
* no `updated_at = now()` — the user asked for `abstract` only, so the column
  is deliberately left alone (unlike `crosscheck/apply_abstracts.py`, which does
  touch it).

Shell commands used:

```bash
# pass 1 — dry run, then apply
python3 scripts/abstracts/fill_abstract_from_title.py
python3 scripts/abstracts/fill_abstract_from_title.py --apply

# pass 2 — dry run (full 31-row before/after shown), then apply
python3 scripts/abstracts/fill_abstract_from_title.py --placeholders "xx,x,xxx,-,NA,2022" --show 40
python3 scripts/abstracts/fill_abstract_from_title.py --placeholders "xx,x,xxx,-,NA,2022" --apply
```

---

## 4. SQL — verification (post-write)

```sql
-- 1. nothing empty or placeholder is left
SELECT count(*) FROM public.records
WHERE (abstract IS NULL OR abstract = '')
   OR lower(btrim(abstract)) IN ('xx','x','xxx','-','na','2022');
-- 0

-- 2. the touched rows really carry their title
SELECT count(*) FROM public.records
WHERE id = ANY(:ids::uuid[]) AND abstract = title;
-- 2133 / 31

-- 3. raw_metadata was NOT written: it still holds the original value
SELECT count(*) FROM public.records
WHERE id = ANY(:ids::uuid[])
  AND coalesce(raw_metadata->>'abstract','') IN ('xx','x','xxx','-','NA','2022');
-- 31 (and for pass 1: raw abstract still '' or key missing, 2,133 / 0 non-empty)

-- 4. updated_at untouched (no other column written)
SELECT count(*) FROM public.records
WHERE id = ANY(:ids::uuid[]) AND updated_at < '2026-10-01';
-- 31 (and 2,133 for pass 1)

-- 5. whole-table sanity
SELECT count(*) FROM public.records WHERE abstract IS NULL OR abstract = '';  -- 0
SELECT count(*) FROM public.records;                                          -- 25130
```

`:ids` = the `id` list from the corresponding backup JSONL.

---

## 5. Undo

Restore is exact — the backups record whether the old value was `NULL` or `''`
(or which placeholder), so the pre-run state comes back byte for byte:

```bash
python3 scripts/abstracts/fill_abstract_from_title.py \
  --restore output/apply/abstract_from_title_placeholders_backup.jsonl --apply   # pass 2
python3 scripts/abstracts/fill_abstract_from_title.py \
  --restore output/apply/abstract_from_title_backup.jsonl --apply                # pass 1
```

The SQL it runs (parameterized per record):

```sql
UPDATE public.records SET abstract = %s WHERE id = %s;
-- %s = the recorded old value (NULL, '' or the placeholder), then the record id
```

---

## 6. Artifacts

| File | What |
| :--- | :--- |
| `scripts/abstracts/fill_abstract_from_title.py` | the script (dry-run by default, `--apply`, `--restore`) |
| `data/empty_abstracts_2133.csv` | pre-state list of pass 1: `id, identifier, title, abstract_state, raw_metadata_abstract` |
| `output/apply/abstract_from_title_backup.jsonl` | before-image, 2,133 records |
| `output/apply/abstract_from_title_placeholders_backup.jsonl` | before-image, 31 records |
| `scripts/README.md` | script docs (row in the files table + quick-start step 6) |

---

## 7. Notes

* **`raw_metadata` is intentionally out of sync now** — it still holds `''` /
  `xx` / `2022` for these 2,164 records, per the instruction "only change
  abstract column". Cleaning the raw side is a job for the messy-cleanup pass
  (`scripts/cleanup/apply_cleanup.py`), not this one.
* 344 of the original 2,133 are DOIs; the rest are Zenodo/other identifiers, so
  a source-based backfill (e.g. `crosscheck/fetch_sources.py`) could later
  replace these title-copies with real abstracts — the backups make that safe to
  redo.
* 297 further records already had `abstract = title` before this work; they were
  not touched (they were never in the target set).
* Other placeholder-looking values found but **left alone**: `DOS-3212 A-B-C-D`
  (looks like a deliberate project code) and `2022`-style year strings other
  than the one in pass 2 — only the 31 listed values were treated as empty.
