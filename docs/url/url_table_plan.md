# Plan — `records.url`: derive a URL from `identifier` (+ raw_metadata, + verified lookups)

> **Status (2026-10-07): 23,139 urls written, 1,990 NULL** (2026-10-02: rules + verified D/E = 21,577; 2026-10-07: +1,562 from the source-catalogue pass, see §9). Before-images: `output/url/url_before_image.csv`, `output/url/source_urls_before_image.csv`.
> **Target table is `validate.records` in database `msp-kh-dev`** (not `public.records`; see §2).
> Rollback: `ALTER TABLE records DROP COLUMN url`. Everything below the
> results section describes what `--apply` did.

**Decisions taken:** a single new nullable column `url` on `records` (no new
table) · NULL where no public resolver / no verified URL exists · every added
URL is either rule-derived or evidence-verified, never guessed.

---

## 1. Results so far (25,130 records)

### 1.1 Who gets a URL

| tier | type | source of the URL | records | confidence |
| :-- | :-- | :-- | --: | :-- |
| base | `doi_url` | identifier is already a doi.org URL (normalised) | 8,579 | high |
| base | `doi` | `https://doi.org/<doi>` built from identifier | 7,330 | high |
| base | `url` | identifier is already a URL (normalised) | 3,001 | high |
| **A** | `landing_page` | `raw_metadata.distribution.landingPage.url` (MCF records) | 1,360 | high |
| **B** | `bonares_landing` | BONARES first link (`format=link-0`, host `maps.bonares.de`) | 1,018 | high (host unreachable from our network, see 1.3) |
| **C** | `oai_handle` | `oai:<host>:<handle>` → `https://hdl.handle.net/<handle>` | 75 | medium |
| **D** | `tier_d` | OpenAIRE Graph API title search, title identical **and** year equal | 150 verified | high |
| **E** | `tier_e` | a page-type link whose page answers 2xx **and contains the record title** | 64 verified | high |
| | | **rules only (base + A + B + C)** | **21,363 (85.0%)** | |
| | | **rules + verified D/E** | **21,577 (85.9%)** | |

Stays NULL (rules only): 3,767 = `uuid` 1,892 · `other` 1,086 · `oai_recolecta` 789.
After merging D/E: 3,553 NULL.

"Already had a URL" = the identifier itself is a URL: 11,580 records (46.1%).
(The `url` column does not exist yet, so nothing in the database "already had" one.)

### 1.2 Tiers D and E in detail (`output/url/url_candidates_DE.csv`)

| tier | candidates run | accepted (`verified=yes`) | other |
| :-- | --: | --: | :-- |
| D (OpenAIRE, records with `sources=openaire`) | 937 | 150 | 27 title-matched but URL answers 403/429 ("blocked", **not** merged), 16 URL does not respond, 744 no URL |
| E (records with `links`) | 2,605 | 64 | 2,541 no URL |

Main reasons for no URL: E: 1,099 have no page-type link (only downloads / OGC services
/ images); ~750 have candidates but the title is not on the page; ~150 time out or refuse the connection.
D: 629 matched but no DOI and not exactly one instance URL; 58 no exact title+year
match; 38 ambiguous (several different DOIs for the same title+year).
Note: some accepted E URLs are ArcGIS REST service pages (the service page does
contain the title); they are service descriptions, not human landing pages.

### 1.3 Do the URLs work? (HTTP check of the 21,363 rule URLs, 21,159 distinct)

| class | records | share |
| :-- | --: | --: |
| ok (2xx after redirects) | 14,110 | 66.0% |
| blocked (401/403/429: server answered, bot-blocked or login-gated) | 5,231 | 24.5% |
| client_error (404, 400, 410 …) | 681 | 3.2% |
| server_error (5xx) | 23 | 0.1% |
| error (no answer: timeout / SSL / DNS) | 1,318 | 6.2% |

* **1,018 of the 1,318 errors are tier B**: `maps.bonares.de` does not answer from
  this network at all (even `https://maps.bonares.de/` times out in 25 s; `www.bonares.de`
  answers 200). These URLs are therefore **untested, not proven broken**. Re-check from
  another network before relying on tier B.
* 576 `ec.europa.eu/…/downloadPublic?documentIds=` URLs (identifier is already such a URL)
  return 400 → they are download links that are rejected without a session; the
  identifier is real, the URL just does not open for a script.
* Tier A: 1,358 of 1,360 ok. Tier C: 18 ok, 13 blocked, 10 4xx, 33 no answer (27 SSL
  errors on repository handle servers), 1 5xx → **weakest tier**.
* DOI URLs: 4,686 ok, 2,449 blocked (publishers refuse scripts), only 26 4xx.
* 8 DOI URLs are shared by 2 records each (`10.1038/nature01136` and
  `https://doi.org/10.1038/nature01136.` with a trailing dot) → likely duplicate records.
* 70 URLs in total are shared by >1 record. Most are BONARES records (child tables /
  embargo placeholders) pointing at the parent dataset page.

### 1.4 Not covered / decided against

* `oai_recolecta` (OpenAIRE ids `50|…`): the OpenAIRE search API ignores `resultID`,
  and `explore.openaire.eu/...?articleId=` returns 200 for any id (SPA), so it cannot
  be verified → only found via D (title+year).
* ~2,500 records whose `links` mix downloads/WMS/GitHub sources: choosing one would be a
  guess → only found via E when a page verifies.
* 393 `uuid` records mention a DOI somewhere in metadata (often a *cited* paper, not the
  record's own) → not used.
* ISBN/ISSN, bare digit strings, hex ids, regional catalogue ids (`r_lombar:…`),
  free-text slugs → NULL.

---

## 2. Findings that affect the design

* **Schema name.** The harmonizer (`soilwise-he/md-harmonization`) uses `metadata.records`;
  our scripts use unqualified `records` (connection `search_path`). Confirm they are the
  same table before `--apply`.
* **Harmonizer impact: none for a new column.** Its only write (`process.py`, `INSERT INTO
  records (…) … ON CONFLICT (identifier) DO UPDATE SET …`) uses an explicit column list and
  sets only those columns, so `url` is neither required nor overwritten. Its only read of
  `records` selects named columns. No `SELECT *`/positional insert/`COPY` found. No view
  depends on `records` in our database (checked by `build_url_column.py`).
* The harmonizer already has `distributions` and `alternate_identifiers` tables and itself
  adds a `https://doi.org/…` distribution for DOI identifiers; `records.url` is a separate
  single "main link" and does not replace them.
* If `identifier` is re-harvested/cleaned, `url` goes stale until the script is re-run
  with `--refresh`.
* An editor CSV-aligner plugin re-formatted `output/url/*.csv` with padded columns,
  which broke reading them; `check_urls.py` now tolerates it, but turn the plugin off
  for `output/url/`.

---

## 3. Rules (single module: `scripts/url/url_rules.py`)

`classify(identifier, raw_metadata=None) -> (type, url)`. Top to bottom, first match wins.
Input is `identifier` after `strip()`.

| # | type | match | URL |
| :-- | :-- | :-- | :-- |
| 1 | `doi_url` | `https?://(dx.)?doi.org/10.xxxx/…` | `https://doi.org/<doi>` |
| 2 | `doi` | `[doi:]10.xxxx/…` | `https://doi.org/<doi>` |
| 3 | `url` | `https?://\S+` | normalised (scheme+host lowercased) |
| 4 | `arxiv` | `arxiv:<id>` | `https://arxiv.org/abs/<id>` |
| 5 | `handle` | `hdl:<h>` | `https://hdl.handle.net/<h>` |
| 6 | `urn` | `urn:nbn:xx:…` | `https://nbn-resolving.org/<urn>` |
| 7 | `oai_handle` (C) | `oai:<host>:<digits>/<suffix>` | `https://hdl.handle.net/<handle>` |
| 8 | `uuid` / `oai_recolecta` / `isbn` / `issn` / `other` | no resolver | NULL, **unless** raw_metadata below |
| 9 | `landing_page` (A) | uuid/other + `distribution.landingPage.url` | that URL |
| 10 | `bonares_landing` (B) | uuid/other + source `bonares`, first link `link-0` on `maps.bonares.de` | that URL |

DOI rules: only the `10.` prefix is case-folded, suffix untouched except percent-encoding
of illegal characters (space `<>"{}|\^`` ` ``#?`), trailing `. , ;` and unbalanced `)`
stripped, always `https://doi.org/`. URL rules: `strip()`, lowercase scheme/host only,
strip trailing `.` `,`. Identifier rules always win over raw_metadata. Tiers D/E are not
in this module (they need network access) and are merged from a CSV.

---

## 4. DDL (proposed, not executed)

```sql
ALTER TABLE records ADD COLUMN IF NOT EXISTS url text;
ALTER TABLE records ADD CONSTRAINT records_url_format_ck
    CHECK (url IS NULL OR url ~ '^https?://[^\s]+$') NOT VALID;
-- after the backfill succeeds:
ALTER TABLE records VALIDATE CONSTRAINT records_url_format_ck;
```

(`public.records` / `metadata.records`: see §2.) Nullable, no default → metadata-only
change, instant, no table rewrite. Only `url` is added; nothing in `raw_metadata` or any
other column is modified. Rollback: `ALTER TABLE records DROP COLUMN url`.

---

## 5. Scripts (all in `scripts/`)

| file | what it does | writes DB? |
| :-- | :-- | :-- |
| `url_rules.py` | classifier: identifier (+ optional raw_metadata) → type + URL | no |
| `build_url_column.py` | recon + classify + verify gate + preview CSV; `--candidates CSV` merges verified D/E; `--apply` adds the column and fills it | only with `--apply` |
| `check_urls.py` | HTTP HEAD (GET fallback) every URL in the preview, cached/resumable; writes `url_status.csv` + `docs/url/url_check_report.md`; `--preview` selects the input | no |
| `find_urls_external.py` | tiers D (OpenAIRE title+year) and E (verified page links) → `url_candidates_DE.csv` | no |
| `test_url_rules.py` | 31 plain-assert cases for the rules | no |

`build_url_column.py --apply` safeguards: refuses unless the verify gate passes; writes a
before-image CSV (refuses to overwrite); ONE transaction: add column + NOT VALID CHECK,
stage values in a TEMP table, fill-only (`url IS NULL`, `--refresh` to overwrite), re-check
`identifier` per row, assert updated == expected, `count(*)` unchanged and an md5 of every
row minus `url` unchanged (any other column touched → ROLLBACK); then post-checks and
`VALIDATE CONSTRAINT`. Verify gate: all urls match the format, DOI suffix intact, no-resolver
types stay NULL, tier_d/tier_e rows carry a URL; informational: `other` count, shared URLs.

### Files produced (`output/url/`)

| file | content |
| :-- | :-- |
| `url_preview.csv` | rules-only classification: `record_id, identifier, identifier_type, url` |
| `url_preview_with_DE.csv` | same + verified D/E merged |
| `url_status.csv` | one row per record: id, identifier, type, url_source, url, http_status, status_class, final_url, error |
| `url_check_cache.csv` | one row per distinct URL checked |
| `url_candidates_DE.csv` | D/E search result per record with evidence and verification |
| `docs/url/url_check_report.md` | generated HTTP-check report |

---

## 6. Run order

```
python3 scripts/url/test_url_rules.py
python3 scripts/url/find_urls_external.py                     # D + E  -> url_candidates_DE.csv   (~15 min)
python3 scripts/url/build_url_column.py --candidates output/url/url_candidates_DE.csv \
        --preview output/url/url_preview_with_DE.csv      # dry run, verify gate
python3 scripts/url/check_urls.py --preview output/url/url_preview_with_DE.csv \
        --workers 48 --per-host 12                        # HTTP status of everything (~30-60 min)
# review docs/url/url_check_report.md + url_status.csv, sign off
python3 scripts/url/build_url_column.py --candidates output/url/url_candidates_DE.csv --apply
```

Without D/E, drop `--candidates`. Re-run the whole sequence after any identifier cleanup;
`--refresh` overwrites derived values when the rules change.

---

## 7. Risks / guardrails

* The only change to `records` is the new `url` column; no existing column is updated or
  deleted, no rows added or removed (asserted in the transaction).
* No-resolver records stay NULL; D/E URLs only come with evidence (title match), never
  from a search URL or a guess.
* `blocked` (403/429) does not mean broken: publishers refuse scripts. Do not treat it as a
  failure; `error` and `client_error` are the groups to look at.
* Tier B is unverified over HTTP from our network; tier C is the weakest rule.
* A fill-only update never overwrites a manually set `url`.

---

## 8. Open items

1. Re-check `maps.bonares.de` (tier B) and the 33 tier-C SSL errors from another network.
2. Decide whether the 27 title-matched but HTTP-blocked D records (403 from the
   publisher) should be merged too (currently excluded; evidence is the title+year match).
3. ~~Confirm `records` vs `metadata.records` target~~ — answered 2026-10-07: the table is
   `validate.records` (db `msp-kh-dev`; no `public.records`). Still open: that `mv_records`/pycsw
   consumers do not need the column.
4. Decide what to do with the ~3,550 records that stay NULL (mostly internal ids).
5. Optional: add `identifier_type` as a second nullable column (not included).

---

## 9. Source-catalogue pass (2026-10-07) — records still NULL after §1

Input: the 3,552 NULL-url records (`data/empty_urls_2026-10-07.csv`; the CSV-aligner plugin padded it to 175 MB, read with whitespace stripped).
Idea: find the record on its **source catalogue** (`raw_metadata.sources`) and take that URL.

| source | method | written | evidence |
| :-- | :-- | --: | :-- |
| isric | `data.isric.org/geonetwork/srv/eng/catalog.search#/metadata/<uuid>` | 218 | catalogue API `/api/records/<uuid>` returned the record **and** its title |
| eea | `sdi.eea.europa.eu/catalogue/...#/metadata/<uuid>` (uuid from thumbnail) | 149 | same |
| islandr | `geonetwork.greendecision.eu/...` | 85 | same |
| inspire | catalogue base + uuid taken from the thumbnail (`/api/records/<uuid>`) | 41 | same |
| ejpsoil | GitHub `ejpsoildatahub` source-record page | 255 | page answers 200 |
| openaire | OpenAIRE Graph API `search=<title>`, hit whose id equals the record id → DOI / instance URL / explore page | 560 | id match (556 instance URL, 4 explore page) |
| inspire, ngr, data.europa.eu, ejpsoil | data.europa.eu search API, hit id contains the record identifier → `data.europa.eu/data/datasets/<id>` | 254 | id match |
| | **total written** | **1,562** | |

Not written: 285 title-only matches on data.europa.eu (exact title, one hit; needs a spot-check),
FAO 1,012 (`data.apps.fao.org/map/catalog` returned 502 throughout), BONARES 102 (host unreachable, no
`mid=` link), 96 ambiguous + 163 no-match INSPIRE, 225 OpenAIRE not found, ~60 misc.

**Safeguards and post-checks.** `scripts/url/apply_source_urls.py`: fill-only (`url IS NULL`), before-image,
one transaction, rowcount + `count(*)` asserted. Post-check against the pre-write backup
`records_backup_ogc_types_20261007_152216`: `url` changed on exactly the 1,562 rows, no non-empty url overwritten,
no other column changed on those rows, 0 format violations, `records_url_format_ck` validated. (7,216 other rows differ from that backup in `resource_type`/`updated_at` only —
a separate resource-type change made at 10:22 UTC, before this write.) Note: the first apply did **not** include the
md5-of-other-columns assertion of `build_url_column.py`.

**HTTP check** (`scripts/url/check_urls_source.py` -> `output/url/source_url_status.csv`, `docs/url/source_url_check_report.md`):
after the fix below, 1,530 ok (98.0%), 20 blocked (403), 0 client errors, 1 server error, 11 no answer.

**Defect found and fixed.** 473 OpenAIRE URLs (`ec.europa.eu/.../downloadPublic?documentIds=...`) were written with the
HTML entity `&amp;` and returned 400. Fixed with `scripts/url/fix_source_urls_unescape.py` (`html.unescape`, only rows still holding the written value;
before-image `output/url/source_urls_unescape_before_image.csv`); they now return 200. These are document download links, not landing pages.
OpenAIRE `openaire_instance` URLs are repository/download URLs, not guaranteed landing pages.

Open: retry FAO; re-check BONARES from another network; decide on the 285 title-only data.europa.eu matches.
