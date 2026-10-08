# SoilWise metadata backfill: `title` and `abstract` crosscheck

**Date:** 2026-09-11
**Database:** `msp-kh-stage`, table `public.records`
**Reference source:** `Sep8_SolrData.json` (Solr export, 8 Sep 2026, 92,432,383 bytes)

---

## 1. Problem

Two metadata columns in `public.records` had large gaps:

| Column | Symptom | Rows |
|---|---|---|
| `title` | literal string `Untitled` | 184 |
| `abstract` | empty string | 4,178 |
| `abstract` | `[NULL]` | 110 |
| `abstract` | junk value `xx` | 20 |

The `raw_metadata` column on affected rows showed `"title": ""` with
`"sources": ["openaire"]`, so the values were already missing at harvest time
rather than lost later in the pipeline.

The Solr index held the same records and, in many cases, still had the values.
The task was to match DB rows to Solr docs on `identifier` and report what could
be recovered.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `check_untitled.py` | Crosscheck `title` against the Solr export, report and optional SQL |
| `check_missing.py` | Generalised version, works on any column, tuned for `abstract` |
| `apply_titles.py` | Write recovered titles back to the DB in one transaction |
| `.env.example` | Connection template |

All three read the same `.env`. Dependencies: `psycopg2-binary`, `python-dotenv`.

---

## 3. Results

### 3.1 Titles

| Outcome | Rows |
|---|---|
| Recovered from Solr | 181 |
| Present in Solr but blank there too | 3 |
| Identifier absent from export | 0 |
| **Total** | **184** |

Applied to the database. Remaining untitled rows: 3.

Not recoverable from this export, they need Zenodo/DataCite or the original
harvest payload:

- `10.46909/alse-582175`
- `10.5281/zenodo.19235000`
- `10.5281/zenodo.19679080`

Composition of the 184: 176 `Dataset`, 6 `Journal Article`, 1 `Other`,
1 `Project`. 147 of the identifiers are Zenodo DOIs (`10.5281`).

Match quality: 171 of 184 identifiers matched exactly one Solr doc. The
13 two-match cases all paired consistently (AGU DOIs to AGU titles, IEEE to
IEEE), so they are duplicate deposits of the same record, not collisions.

### 3.2 Abstracts

The crosscheck was run twice. The second run came after the titles were
applied, which made 133 previously uncheckable matches verifiable. All 133
passed the guard.

| Outcome | First run | After titles applied |
|---|---|---|
| Recovered and title-verified | 275 | 408 |
| Recovered but unverifiable (DB title was blank) | 139 | 6 |
| Rejected, title disagreed with the Solr doc | 47 | 47 |
| Present in Solr but also placeholder or too short | 102 | 102 |
| Blank in Solr as well | 3,088 | 3,088 |
| Identifier absent from the export | 807 | 807 |
| **Total** | **4,458** | **4,458** |

408 abstracts applied to the database. The 6 unverified rows were held back.

State after the write, confirming the expected remainder of 4,050:

| Condition | Rows |
|---|---|
| `abstract IS NULL` | 98 |
| empty string | 3,783 |
| shorter than 20 chars | 169 |
| populated | 22,294 |

Rows missing an abstract went from 4,458 to 4,050.

---

## 4. Findings

### 4.1 The export is truncated

`json.load` failed at char 91,929,171 of 92,432,383. The file ends mid-string
inside an abstract, with no closing brackets, so the writing process died before
flush. Roughly 503 KB, about 0.5%, is missing.

Handled by a salvage parser that walks the array with `raw_decode` and keeps
every object completed before the break. It reports how much it rescued on
stderr. All 184 title rows matched regardless, so the truncation cost nothing
for that pass.

For future exports, page with a cursor and restrict the field list. That avoids
the failure mode and cuts the file from 92 MB to a few MB:

```bash
curl -s 'http://SOLR_HOST:8983/solr/CORE/select' \
  --data-urlencode 'q=*:*' \
  --data-urlencode 'fl=id,identifier,title,description' \
  --data-urlencode 'rows=5000' \
  --data-urlencode 'sort=id asc' \
  --data-urlencode 'cursorMark=*' \
  --data-urlencode 'wt=json' -o page_0001.json
```

Then follow `nextCursorMark` until it stops changing.

### 4.2 Identifier matching needs variants

Identifiers are stored inconsistently across sources: bare DOIs
(`10.5281/zenodo.19182285`), prefixed DOIs (`http://doi.org/10.1016/...`,
`https://doi.org/...`), catalogue URLs, bare UUIDs, OpenAIRE internal IDs
(`50|r3c4b2081b22::...`), and slugified DOIs where separators became dashes
(`https---doi.org-10.20387-bonares-40kc-2661`).

Normalisation strips DOI prefixes and case. Slug de-mangling reconstructs
`10.20387/bonares-40kc-2661` from the dashed form. Both the index and the
lookup try every variant.

### 4.3 Identifier collisions produce wrong values

The first abstract run reported 459 recoveries. Inspection showed at least one
was wrong: DOI `10.1007/s10584-012-0650-y` (a GCAM climate modelling paper)
received an abstract about soil organic carbon.

A verification guard was added: candidate docs are scored by title-token overlap
against the DB title, the best-scoring candidate wins, and anything below
`--min-agreement 0.34` is rejected with its value discarded. That moved 47 rows
out of the recovered pile.

Manual review of all 47 confirmed 46 were genuine mismatches, several of them
absurd:

| DB title | Solr title offered |
|---|---|
| Legacy Effects Of Drought On Plant Growth And The Soil Food Web | Holmium laser enucleation of the prostate in Jehovah's Witness patients |
| A Link Between Plant Diversity, Elevated Co2 And Soil Nitrate | Photonic Weyl points due to broken time-reversal symmetry in magnetized semiconductor |
| Mitigating Global Warming Potentials Of Methane And Nitrous Oxide Gases From Rice Paddies | An integrated method for calculating DEM-based RUSLE LS |

One rejection was a false positive: `10.26114/sir.iung.2017.51.04`, where the DB
title is Polish (`Stały monitoring gleb użytków rolnych Polski`) and the Solr
title is its English translation. Token overlap is near zero for
cross-language pairs.

The threshold was left at 0.34. Lowering it to rescue that single row would
readmit genuine mismatches sitting at 0.22 to 0.27 that merely share soil
vocabulary.

Two Solr docs matched multiple unrelated DB DOIs, which suggests some docs carry
a reference or related-works list in their identifier fields. A `--max-doc-ids`
guard now indexes such docs on their primary identifiers only.

Separately, records like the urology and photonics papers above have no business
in a soil catalogue. Their presence is a data quality issue in the Solr index
itself, independent of this matching work.

### 4.4 Most missing abstracts cannot be fixed from Solr

3,088 blank in Solr plus 807 absent from the export is 3,895 of 4,458, about
87%. Sample identifiers in those buckets are national catalogue records rather
than DOIs: GeoNetwork, `geopunt.be`, `kartkatalog.geonorge.no`, `wur.nl`,
`esdac.jrc.ec.europa.eu`, plus 807 bare UUIDs.

Those descriptions were never harvested from the source catalogues. No
crosscheck against this export will recover them.

---

## 5. Method

1. Load the Solr export, salvaging past truncation if needed.
2. Index every doc under all variants of every identifier field, skipping
   over-shared docs.
3. Select DB rows where the target column is NULL, empty, a known placeholder,
   or shorter than `--min-len`.
4. For each row, gather candidate docs across identifier variants, score by
   title agreement, take the best.
5. Bucket the outcome: recovered, unverified, rejected, placeholder in source,
   blank in source, absent.
6. Write a CSV of all buckets, optionally emit reviewable SQL.

Nothing in the crosscheck scripts writes to the database. Emitted SQL ships with
`COMMIT` commented out.

`apply_titles.py` re-derives matches rather than reading the CSV, so there is no
stale-file risk. Its `UPDATE` repeats the untitled condition in the `WHERE`
clause, so a row fixed by someone else in the meantime is skipped rather than
overwritten, and the script is safe to re-run.

Values are cleaned before writing: HTML tags stripped, non-breaking and
zero-width spaces removed, whitespace collapsed. This mattered for
`10.1109/TIM.2025.3637952`, which arrived as
`<i>LC</i>` followed by padding spaces.

---

## 6. Root cause

The gaps are upstream of the database. For the titles, 176 of 184 are typed
`Dataset` and 147 are Zenodo DOIs, all with `"sources": ["openaire"]` and an
empty title in `raw_metadata`, while the titles exist in Solr and at Zenodo.
The OpenAIRE harvester is dropping the title field for this record class.

For the abstracts, the dominant pattern is national catalogue records whose
descriptions were never pulled at all.

Both mean the next harvest recreates the same gaps. The backfill applied here is
a one-off correction, not a fix.

---

## 7. Status and open items

**Done**

- 181 titles recovered and written. 3 remain untitled.
- Abstract crosscheck complete, results in `abstract_crosscheck.csv`.

**Open**

- Apply the 275 verified abstracts.
- Rerun the abstract crosscheck now that titles are populated. Most of the 139
  unverifiable rows became verifiable.
- Fix `10.26114/sir.iung.2017.51.04` by hand, the cross-language false
  rejection.
- Spot-check accepted abstracts in the 0.34 to 0.6 agreement band before
  trusting the full set.
- Source titles for the 3 remaining records from Zenodo or DataCite.
- Raise the OpenAIRE title mapping with the harvester owners.
- Raise the missing national catalogue descriptions separately.
- Re-export from Solr with a cursor and a restricted field list.
- Investigate out-of-scope records in the Solr index (urology, photonics).

---

## 8. Usage reference

```bash
pip install psycopg2-binary python-dotenv
cp .env.example .env        # fill in DB_PASSWORD

# titles
python3 check_untitled.py --json Sep8_SolrData.json
python3 apply_titles.py                     # dry run
python3 apply_titles.py --apply             # writes

# abstracts
python3 check_missing.py --field abstract
python3 check_missing.py --field abstract --show 0 --emit-sql abstracts.sql

# any other column
python3 check_missing.py --field license --min-len 1
```

Useful flags for `check_missing.py`:

| Flag | Default | Effect |
|---|---|---|
| `--min-len` | 20 | Values shorter than this count as missing |
| `--placeholder` | per-field list | Extra junk value to treat as missing, repeatable |
| `--min-agreement` | 0.34 | Minimum title overlap to trust a match, 0 disables |
| `--max-doc-ids` | 6 | Docs with more identifiers are indexed on primaries only |
| `--show` | 25 | Rows printed, 0 for all |