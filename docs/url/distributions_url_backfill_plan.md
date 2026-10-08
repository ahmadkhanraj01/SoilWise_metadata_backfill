# Plan — fill + dedupe `validate.distributions.access_url` (all URLs, no duplicates)

> **Update 2026-10-08 — partly superseded.** After this draft was written, 10,561 `distributions` rows were inserted
> (1,307 for the source-catalogue URLs + 9,254 so that every `records.url` has a matching distribution; see
> `url_table_plan.md` §9 and the root `README.md`, Phase 5). They were written **percent-encoded** (matching the
> existing rows) with `name='landing page'` / `format='text/html'` (`'download'` for `downloadPublic` links), **not**
> decoded as this draft proposes. Consequences: goal 1 is met; the "+9,266 rows" and "37,393 rows after" figures are
> stale (the table now has 46,702 rows); goal 2 (dedupe, −8,014) and goal 3 (decode everything) are still **not done**
> — and decoding would now mix the two conventions unless it also covers the 10,561 new rows.

> **Status (2026-10-07): DRAFT — not executed.** Read-only recon done; numbers below
> are exact (measured against `validate`, the schema the DB was migrated to on
> 2026-10-07). Awaiting sign-off, then dry run → review → `--apply`.
> **Decisions taken:** fill missing **and** dedupe · **decode everything** and
> store plain URLs. One transaction, before-image, undo included.

---

## 1. Goal

After this runs:

1. Every record's distributions contain that record's `records.url`
   (no guessing — the value is copied as stored).
2. No record has two distribution rows whose URLs are equal after
   normalisation (decode + host-lowercase + trailing `/. , ;` strip).
3. `access_url` is stored **plain** (`https://…`), like `records.url` — no more
   percent-encoding.
4. Net effect: `distributions.access_url` holds **every URL in the database**
   (the full 32,531-value union of both columns), per record, without duplicates.

Not in scope (stays gated): back-filling `records.url` from distributions
(the 2,544 `dist_only` records), and the 1,008 records that have no URL anywhere.

---

## 2. Exact numbers (recon, read-only, against `validate`)

| measure | value |
| :-- | --: |
| distribution rows now | 36,141 |
| rows whose value changes on decode | 36,141 (100% encoded) |
| decode result: all become `http(s)://…` | yes, 0 exceptions |
| duplicate extras within a record (normalised key) | **−8,014 rows** |
| …of which raw-byte-identical groups today | 5,573 groups |
| records whose `records.url` is missing from own distributions | **+9,266 rows** |
| **row count after** | **36,141 − 8,014 + 9,266 = 37,393** |
| distinct normalised values in the column after | **32,531 = full union** ✓ |

### 2.1 Post-state per-record categories (predicted)

| category | now | after |
| :-- | --: | --: |
| `neither` (no URL anywhere) | 1,008 | 1,008 |
| `url_only` (in records, **not** in distributions) | 7,125 | **0** |
| `dist_only` (in distributions, not in records) | 2,544 | 2,544 |
| `sets_equal` | 11,821 | 18,946 |
| `url_in_dist_extra` | 490 | 2,631 |
| `url_not_in_dist` (disjoint) | 2,141 | **0** |

(Verification target for §6; `url_only` + `url_not_in_dist` → 0 is the success gate.)

### 2.2 Schema facts that shape the script

* **No unique constraint** on `(record_id, access_url)` → nothing stops future
  duplicates; only PK on `id` (uuid, **no default** → script must supply
  `gen_random_uuid()`, available: PG 14.24 ✓).
* FK `record_id → records.id ON DELETE CASCADE` (we delete distributions only).
* `name` NOT NULL (no default) · `format`, `access_url`, `description` nullable ·
  `created_at`/`updated_at` default `now()` · `description` NULL on all 36,141 rows.
* **No triggers, no views, no rules** depend on `distributions`.
* Majority pattern for a landing-page row: `name='landing page'`, `format='text/html'`
  (12,463 / 12,497 rows) → used for the inserts.

---

## 3. Definitions

* **decode** = `urllib.parse.unquote(strip(v))` — percent-decode + trim only.
  Values are **not** otherwise rewritten (no host lowercasing of stored data).
* **normalised key** (comparison/dedupe only, never stored):
  decode → lowercase scheme+host → strip trailing `/ . , ;`.
* **dedupe keep-rule**: keep the row with the earliest `created_at`, tie →
  smallest `id`; delete the rest within the same record. Cross-record duplicates
  (same URL shared by many records — parent dataset pages etc.) are **kept**;
  this is a child table, duplication across records is legitimate.
* **insert value** = `records.url` exactly as stored (it already satisfies
  `^https?://\S+$`), skipped when the normalised value is already present.

---

## 4. Execution (new script `scripts/fill_distributions.py`)

Pattern mirrors `apply_cleanup.py` / `build_url_column.py`: **dry run by default,
`--apply` to write.**

### 4.1 Dry run (`python3 scripts/fill_distributions.py`)

1. Read-only load: `distributions (id, record_id, access_url, created_at, name,
   format, description)` + `records (id, url)`.
2. Compute deletes / decodes / inserts from this snapshot.
3. Write previews (refuse to overwrite):
   * `output/apply/dist_dedupe_deletes.csv` — `id, record_id, access_url_decoded, kept_id`
   * `output/apply/dist_inserts.csv` — `record_id, identifier, records_url`
   * `output/apply/dist_decodes.csv` — `id, access_url_before, access_url_after`
4. Print the §2 counts; **abort if they drift** from the plan by >0 (a concurrent
   writer means re-recon first).

### 4.2 Apply (`--apply`) — ONE transaction

```
BEGIN;
pg_advisory_xact_lock(<fixed key>);          -- block a concurrent harmonizer write
SELECT id, record_id, access_url, created_at FROM distributions FOR UPDATE;
  → recompute deletes/decodes/inserts INSIDE the txn from this locked snapshot
    (no staleness race; counts asserted again here)
before-image: full table (all columns) → output/apply/dist_before_image_<ts>.csv
              (refuse if exists)
DELETE  … USING keep-rule CTE                      → assert == expected deletes
UPDATE  … SET access_url = <decoded>, updated_at = now()
        WHERE value changes                       → assert == expected decodes
INSERT  (id=gen_random_uuid(), record_id, name='landing page',
         format='text/html', access_url=<records.url>, description=NULL)  → assert == 9,266
post-checks inside txn:
  • count(*) == 37,393 (recomputed from the locked snapshot)
  • 0 records with two rows equal under the normalised key
  • 0 access_url NOT matching '^https?://'
  • public/validate.records untouched: count + md5(id||url||…) unchanged
  • name/format/description/record_id of every surviving pre-existing row unchanged
      (compare against before-image; any drift → ROLLBACK)
COMMIT  (any assertion fails → ROLLBACK, nothing partially applied)
```

### 4.3 Undo

`python3 scripts/fill_distributions.py --undo output/apply/dist_before_image_<ts>.csv`
→ one transaction: delete all current distribution rows, re-insert from the image
(exact original encoded values, ids, timestamps). Assert final count == 36,141.

---

## 5. Run order

```
python3 scripts/fill_distributions.py                       # dry run + previews
# review preview CSVs + counts vs §2, sign off
python3 scripts/fill_distributions.py --apply               # one transaction
python3 scripts/url/compare_url_accessurl.py                    # post-state report (read-only)
python3 scripts/url/compare_url_accessurl.py --http             # optional: HTTP of any new URL values
```

---

## 6. Verification gates (all must pass)

1. Row count == 37,393 · `url_only == 0` and `url_not_in_dist == 0` in the
   re-run comparison report · distinct normalised values == 32,531.
2. `select count(*) … group by record_id, <normalised key> having count(*)>1` → 0.
3. `access_url !~ '^https?://'` → 0 rows.
4. `records` table count and md5 unchanged (we never write it).
5. Sample 10 random records by hand (encoded before / plain after / insert added).
6. `docs/url/url_vs_accessurl_report.md` regenerated (categories in §2.1).

---

## 7. Risks / guardrails

* **Harmonizer (`soilwise-he/md-harmonization`) writes `distributions`** and
  itself inserts a `https://doi.org/…` distribution for DOI identifiers.
  → **Check its insert logic before `--apply`**: if it does not dedupe, its next
  harvest may re-add rows (possibly percent-encoded again) and collide with our
  9,266 inserts. Mitigations: advisory lock during the txn; re-run this script
  after any harvest; longer term fix it there (this also closes §8.1 of
  `url_vs_accessurl_plan.md` — the decode now happens here instead).
* No unique constraint → the script cannot rely on the DB to prevent duplicates;
  the post-check inside the txn is the guard.
* Cross-record duplicate URLs are intentionally kept (child table semantics).
* `name`/`format` values for new rows (`landing page` / `text/html`) are a
  choice, not derived from the URL — change before apply if unwanted.
* Consumers (`mv_records`/pycsw or anything reading `distributions`): no views
  in this DB reference it, but an external consumer could exist — open item.
* Everything is reversible from the before-image; `records` is never touched.

---

## 8. Open items

1. Read the harmonizer's `distributions` write path (dedupe? encoding?) before sign-off.
2. Confirm no external consumer expects percent-encoded `access_url`.
3. Optional hardening after apply: partial unique index
   `UNIQUE (record_id, access_url)` — only safe once the column is uniformly
   plain **and** the harmonizer is fixed to write plain + dedupe.
