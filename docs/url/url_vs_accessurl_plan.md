# Plan — compare `public.records.url` vs `public.distributions.access_url`

> **Update 2026-10-08:** the fix part (§8) was not executed as written. 10,561 `distributions` rows were inserted instead (percent-encoded, `landing page`/`text/html`) — see `distributions_url_backfill_plan.md` (status note) and the root `README.md`, Phase 5.

> **Status (2026-10-07): DRAFT for the fix part; comparison + HTTP check DONE.**
> Structural comparison run and the full HTTP check completed (32,531/32,531 URLs,
> 100%): see `docs/url/url_vs_accessurl_report.md`. **Read-only throughout — nothing
> written to the database.** Note: the DB was migrated mid-session out of `public`
> into `validate`; the scripts now auto-detect the schema (`DB_SCHEMA` in
> `crosscheck/.env` updated to `validate`).
> Implementation started after the scoping answers below; §8 fixes remain
> **gated** (pending sign-off).

---

## 1. The question

Two columns in the `public` schema hold "the URL of a record":

| | `records.url` | `distributions.access_url` |
| :-- | :-- | :-- |
| shape | one nullable value per record | one-to-many child table (`record_id`), avg **2.13** rows/record, max **343** |
| rows with a value | 21,577 of 25,129 records | 36,141 rows, **all** non-empty |
| distinct values | 21,345 | 23,397 |
| format | plain URL `https://…` | **100 % percent-encoded** `https%3A%2F%2F…` (0 rows start with `http`) |

We want to know: how many values/records are covered by each, where they
overlap, and **which of the two actually works** (HTTP-wise).

### 1.1 Recon numbers (read-only, 2026-10-07)

* Raw string comparison → **0 matches**: an artefact of the percent-encoding,
  not a real difference.
* After URL-decoding `access_url`: **12,178** distinct values occur in both
  columns (**12,183** after normalisation, see §3).
* Per record (records that have both a `url` ≥1 distribution):
  **12,318** have at least one distribution whose decoded URL equals
  `records.url` exactly (+4 more only match after normalisation).
* Coverage per record: **14,452** both · **7,125** only `records.url` ·
  **2,544** only distributions · **1,008** neither (25,129 total).
* `distributions.access_url` is not only landing pages: it contains downloads
  (`.zip/.pdf/.png/.tiff`), OGC services (WMS/WFS/WCS/Atom), API endpoints and
  real landing pages — hence the full-set comparison (§3).

---

## 2. Decisions taken (scoping answers)

| # | Question | Decision |
| :-- | :-- | :-- |
| 1 | HTTP check scope | **Check distributions only; reuse `output/url/url_check_cache.csv`** (21,159 entries, Oct 2). 12,137 of the 23,397 decoded access URLs are already cached → **11,260 new HTTP checks**. (Optional: 186 `records.url` values are also not in the cache — noted, not part of the base run.) |
| 2 | Comparison rule | **Decoded + normalised**: `unquote()` first, then trim, lowercase scheme+host, drop trailing `/`. Report **exact (decoded) and normalised matches separately**. |
| 3 | Multi-URL handling | **Compare full sets**: per record, the set `{records.url}` vs the full set of decoded+normalised access URLs of that record. |
| 4 | Deliverables | **Plan MD (this file) + one script + report MD + CSVs.** |
| 5 | Database | **Read-only now, fix later**: the plan includes a gated implementation section (§8) with SQL/rollback, executed only after explicit sign-off. |

---

## 3. Comparison definitions

Normalisation used everywhere: `n(u) = lowercase-scheme-and-host(strip(unquote(u)))`,
trailing `/` (and trailing `.`/`,` on the path) removed. Both the **exact
decoded** value and the **normalised** value are kept so the report can show
both numbers (recon: 12,178 exact vs 12,183 normalised distinct overlap).

Per record, with `U = n(records.url)` (NULL or 1 value) and
`D = {n(access_url) … }` (0..n values), exactly one category:

| category | definition | recon count* |
| :-- | :-- | --: |
| `neither` | U NULL and D empty | 1,008 |
| `url_only` | U set, no distributions with a URL | 7,125 |
| `dist_only` | U NULL, ≥1 distribution URL | 2,544 |
| `sets_equal` | D == {U} exactly (one distribution, same URL) | (in 12,318) |
| `url_in_dist_extra` | U ∈ D but |D| > 1 (record URL is one of many) | (in 12,318) |
| `url_not_in_dist` | both present, U ∉ D (disjoint) | (remainder of 14,452) |

\* recon counts for the first three are final; the last three are computed by
the script (recon only measured "any match": 12,318 exact).

Also reported: distinct-value overlap (`|R ∩ D|` exact and normalised) and,
per category, the HTTP outcome breakdown of each side (§4).

---

## 4. "Which one works" — HTTP check

* Source of URLs: decoded+normalised **`distributions.access_url` only**
  (23,397 distinct), minus what the cache already has → **11,260 to check**.
* Reuse `scripts/url/check_urls.py` machinery by import (`check()`,
  `classify_status()`, cache format `url,http_status,final_url,error,method`) —
  the cache file is **appended to in the same schema**, so `check_urls.py`
  keeps working unchanged afterwards.
* Resumable (cache = resume point), `--workers 48 --per-host 12` defaults,
  same as the Phase 4 run. Expected duration ~15–30 min for 11k URLs.
* Status classes (same as `docs/url/url_check_report.md`): `ok` (2xx after
  redirects) = **works**; `blocked` (401/403/429) = server answered but
  refuses scripts — reported **separately, not counted as broken**;
  `client_error`, `server_error`, `error` (timeout/SSL/DNS) = does not work
  from this network.
* Caveats carried over from Phase 4: `maps.bonares.de` (1,018 URLs) is
  unreachable from this network → "untested, not proven broken";
  `blocked` ≠ broken.

---

## 5. Deliverables

| file | what | DB writes? |
| :-- | :-- | :-- |
| `docs/url/url_vs_accessurl_plan.md` | this plan | — |
| `scripts/url/compare_url_accessurl.py` | one read-only script: load → normalise → compare → HTTP-check (cache-backed) → write CSVs + report | **no** (read-only session) |
| `docs/url/url_vs_accessurl_report.md` | generated results report (numbers + tables) | — |
| `output/url/url_vs_dist_per_record.csv` | one row per record: `record_id, identifier, records_url, n_dist_urls, category, url_http, best_dist_http` | — |
| `output/url/url_vs_dist_urls.csv` | one row per distinct normalised URL: `url, in_records, in_distributions, http_status, status_class, final_url` | — |
| `output/url/url_check_cache.csv` | **appended** (~11,260 new rows, existing schema) | — |

CSVs written with plain (unpadded) columns; note the CSV-aligner plugin caveat
from Phase 4 (turn it off for `output/url/`).

---

## 6. Script design (`scripts/url/compare_url_accessurl.py`)

```
read-only load    SELECT id, identifier, url FROM public.records
                  SELECT record_id, access_url FROM public.distributions
normalise         unquote + trim + lowercase scheme/host + strip trailing /. ,
compare           per-record category (§3) + distinct-set overlap
http              distinct normalised dist URLs not in cache -> check (append)
outputs           2 CSVs + docs/url/url_vs_accessurl_report.md
```

Flags: `--no-http` (structural pass only), `--workers`, `--per-host`,
`--timeout`, `--limit` (smoke test), `--report-only` (rebuild report from
cache, no network). DB connection exactly like the other scripts
(`crosscheck/.env`, `load_dotenv`, `connect_timeout`), session opened
**read-only** (`conn.set_session(readonly=True)`) so a stray write is
impossible.

Guardrails: never modifies either table; the only file touched in common with
past work is the append-only HTTP cache (same schema, same key = URL).

---

## 7. Run order

```
python3 scripts/url/compare_url_accessurl.py                                # structural pass (no network) — DONE 2026-10-07
python3 scripts/url/compare_url_accessurl.py --http --workers 48 --per-host 12   # + HTTP check of the 11,260 uncached dist URLs
# review docs/url/url_vs_accessurl_report.md, sign off
```

---

## 8. GATED — possible follow-up fixes (NOT part of this task, pending sign-off)

Read-only pass first. Only after the report is reviewed, and each item
separately approved:

1. **Decode `distributions.access_url` in place** (make it a plain URL like
   `records.url`). **SUPERSEDED by `docs/url/distributions_url_backfill_plan.md`**
   (2026-10-07): that plan decodes the column together with fill + dedupe in one
   transaction — follow it instead of a standalone decode.
2. **Backfill `records.url` for the 2,544 `dist_only` records** — only from a
   *landing-page-like* distribution (not a .zip/WMS link), never guessed;
   fill-only (`WHERE url IS NULL`), before-image, one transaction.
3. The 1,008 `neither` records stay out of scope (same as Phase 4 NULLs).

Rollback for everything: restore from the before-image CSVs; no column is
added or dropped.

---

## 9. Open items

1. Whether the 186 `records.url` values missing from the cache get checked too
   (cheap, ~186 requests) — default: **yes, included in the run**.
2. Whether `blocked` should count as "works" in the headline number
   (default: **no**, reported as its own class).
3. Item §8.1 must be raised with the harmonizer owners before any decode fix.
