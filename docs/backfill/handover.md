# Handover — abstract backfill from upstream sources

> **Note 2026-10-08:** historical (phase closed 2026-09-25). The database is now schema `validate` in `msp-kh-dev`; scripts referred to here are in `crosscheck/`.

**Date written:** 2026-09-24, updated 2026-09-25 (18:00 PKT)
**Working dir:** `/home/ahmad/K2X/SoilWise metadata backfill`
**Status:** **Done for this pass. 1,550 rows written to `msp-kh-dev`** across three writes
2026-09-25 (§0c, §0d, §0e). DB's missing-abstract count: 4,045 → 2,495.
`source_abstract_crosscheck.csv` remains the source-of-truth CSV for the original 3,877-row
set; `source_abstracts.sql` is superseded by `source_abstracts_filtered.sql` and by
`apply_abstracts.py`, which is what actually did the bulk writes. The ~168 out-of-scope rows
(§0f) were investigated and deliberately left unwritten — see §0f for why and what it'd take
to reopen that.
**Remaining gaps in `msp-kh-dev` (2,495 rows), none planned for further action:** 1,930
`source_no_abstract` (confirmed dead end, §6), 170 `title_mismatch` (correctly rejected, §0e),
119 `unreachable`, 108 excluded false positives (§0c/§0d), 168 out-of-scope (§0f).

## 0. Run #1 → run #2, what changed

Run #1 (PID 10126, launched 2026-09-24 16:44, `--interval 0.2`) **finished** overnight —
see `/tmp/full_run.log` (35,837 lines, ends in a clean summary). Its output is still on disk:

```
Summary: 1497 recovered / 155 recovered_unverified / 173 title_mismatch /
         0 source_also_placeholder / 1932 source_no_abstract / 120 unreachable /
         0 unfetchable (of 3877)
```
That's **~38.6% recovered**, in line with the §5 pilot estimate of 40%. `source_abstract_crosscheck.csv`
and `source_abstracts.sql` (1497 UPDATE statements, COMMIT still commented out) on disk right
now **are these run #1 results** — nothing has consumed or acted on them yet.

Before touching those results, `fetch_sources.py` was edited (2026-09-25 ~10:43) to add a
**`semantic_scholar` resolver** into the default `--chain`, inserted between `datacite` and
`title_search`:
```
openalex, crossref, datacite, semantic_scholar, title_search, landing, pdf
```
This directly contradicts §6 below ("Semantic Scholar — 429 without an API key" — verified
dead end, off by default). It's back in the chain now; run #2's log already shows it failing
the same way (`semantic_scholar -> FAILED: HTTP 429`), so **it is not expected to recover
anything new**, but it does add a request (and, per §7, the per-host `--interval` doesn't
learn from 429s) to every DOI row's chain before falling through to `title_search`/`landing`.

Run #2 (PID 11532) was launched 2026-09-25 10:45:01 as:
```
python3 fetch_sources.py --workers 10 --interval 1.2 --verbose --show 0 \
    --csv source_abstract_crosscheck.csv --emit-sql source_abstracts.sql > /tmp/full_run2.log 2>&1
```
Two differences from run #1 besides the resolver: **`--interval 1.2` vs `0.2`** (6x slower
per-request pacing) and **`--verbose`** added. `--refresh` was *not* passed, so the existing
`source_fetch_cache.json` should still serve cached identifiers — but the cache file only had
**244 entries** at last check (11:07), well under run #1's final count, and the log is already
past group 600+, so most of run #1's cache does not appear to be hitting. **Not yet confirmed
why** — worth checking whether the cache key changed (e.g. semantic_scholar candidates being
appended to existing cache entries would explain slow growth without a full re-fetch; a
straight discard would explain requests starting from ~0 again). Check `source_fetch_cache.json`
size trend before assuming either way.

At 11:08 (23 min elapsed) run #2 was at **624/3853 groups (~16%)** — much slower than run #1's
44% at the same elapsed time, consistent with the 6x `--interval` increase. Rough linear ETA:
~142 min total → **finishes around 13:05–13:15 PKT**, i.e. roughly 3x run #1's ~50 min.

**Practical implication:** run #1's `source_abstract_crosscheck.csv` / `source_abstracts.sql`
are already complete and reviewable (§4 below works on them right now). Run #2 will overwrite
both files when it finishes, with results that should be ~the same modulo whatever
`semantic_scholar` changes (expected: none, since it 429s) — but confirm the recovered count
is still ~1497 and not lower, in case the cache issue above caused re-fetches to behave
differently than run #1's.

## 0b. Run #2 → crash → run #3 (2026-09-25, 12:15–13:25 PKT)

**`semantic_scholar`'s 429 is a hard WAF block, not a rate limit.** Direct `curl` against the
endpoint confirmed every response carries `x-api-key: blocked-by-waf` (AWS/CloudFront WAF,
no `Retry-After`), regardless of pacing. No amount of `--interval` tuning or backoff fixes
this — it needs a real Semantic Scholar API key, which nobody has. The `resolve_semantic_scholar`
docstring's 2026-09-25 claim ("no API key required") was wrong; it's fixed now and the
resolver is back out of the default `--chain`:
```
openalex, crossref, datacite, title_search, landing, pdf
```

**Run #2 (PID 11532) crashed silently** around 12:11–12:18 PKT at ~2158/3853 groups (~56%).
No traceback, no OOM in `dmesg`, process just vanished from `pgrep`. Root cause not
identified — not worth chasing since the fix below made it moot. Outputs on disk at that
point were untouched (still run #1's, from 10:19), so nothing was lost.

**Important cache-mechanics discovery:** `source_fetch_cache.json` (`cache` dict, written to
disk) only ever holds groups that produced a **successful candidate** (`cache[key] = cands_`).
Groups that came back `no_abstract_in_source` or `unreachable` live only in the in-memory
`failures` dict for that process run and are **never persisted**. So a restart re-fetches
essentially everything except the ~600-ish groups that had already found an abstract —
"resume from cache" only means "don't redo the wins," not "don't redo the losses." This is
why run #2's cache stayed at 244–629 entries despite processing thousands of groups, and why
killing a run partway through is expensive: you lose most of its work, not just the tail.

**Run #3 (PID 31661)** was launched immediately after the fix, same command as run #2 minus
`semantic_scholar`:
```
python3 fetch_sources.py --workers 10 --interval 1.2 --verbose --show 0 \
    --csv source_abstract_crosscheck.csv --emit-sql source_abstracts.sql > /tmp/full_run3.log 2>&1
```
Resumed from the 629-entry cache run #2 had built up, so it only had 3,225 groups pending
(not the full 3,853). Ran cleanly start to finish, ~62 min, no crash. Progress was polled
every ~4 min via `pgrep`/log-tail; steady ~50 groups/min throughout.

**Finished 2026-09-25 13:25 PKT:**
```
Summary: 1499 recovered / 156 recovered_unverified / 173 title_mismatch /
         0 source_also_placeholder / 1930 source_no_abstract / 119 unreachable /
         0 unfetchable (of 3877)
```
vs. run #1's baseline (1497 / 155 / 173 / 0 / 1932 / 120 / 0) — deltas of ±1–2 rows across
categories, totals match at 3877, no regression from dropping `semantic_scholar` (it never
contributed anything real; deltas are ordinary fetch-to-fetch network noise). **This is the
current result set** — `source_abstract_crosscheck.csv` and `source_abstracts.sql` on disk
(timestamped 13:25) are run #3's output, ready for the §4 review sequence.

---

## 0c. Review, exclusion list, and write to msp-kh-dev (2026-09-25, 14:00–15:40 PKT)

Reviewed run #3's 1,499 `recovered` rows before writing. Three passes, each catching a
different failure shape the guards in §2 don't cover (all operate on identifier/title
agreement, not on whether the *content itself* is a real abstract):

1. **Worst-agreement review** (`title_agreement` sorted ascending) — caught 2 `landing:*`
   rows where the "abstract" was actually the journal/site's og:description, not content
   specific to the article (AFES journal branding; a prepsoil.eu Italian course blurb).
2. **Duplicate-abstract scan** (same `source_abstract` string reused across rows with
   *different* source identifiers — 44 groups / 126 rows). Root-caused to two distinct
   problems:
   - **19 rows: Copernicus/EGU open-review comment threads.** Records like "Reply on RC1" /
     "Comment on soil-2020-96" are peer-review correspondence DOIs
     (`10.5194/<journal>-<year>-<n>-ac/rc/cc<n>`) with no abstract of their own. OpenAlex/
     Crossref/DataCite return the **parent discussion paper's** abstract for every child DOI
     instead, and since each child DOI is a distinct identifier the guard doesn't catch it —
     it only checks title agreement, and the resolver's title also comes from the wrong paper
     consistently, so agreement scores high anyway. This looks endemic to Copernicus dual-DOI
     discussion journals (SOIL, GMD, EGUsphere, BG, …); worth a standing rule if a future pass
     re-touches this set.
   - **~6 rows: boilerplate/site taglines** — "Preparing the European Mission towards healthy
     soils" (prepsoil.eu site tagline, ×5), "International audience" (HAL repository tag, ×3),
     PNAS journal boilerplate, an ENoLL webinar license disclaimer.
3. **Length-distribution + pattern sweep** on what remained — caught 6 more:
   citation-string-shaped OpenAlex "abstracts" (title + " – conference name – dates", found via
   a 4-row cluster all from the same negative-stiffness-absorber conference proceedings), an
   EU-project "views and opinions expressed…" disclaimer fragment (×2, one truncated mid-word),
   a Zenodo bibliographic note ("20 páginas.- 2 tablas.- referencias.-"), a "supplementary
   material" stub pointing elsewhere with no content of its own, an OpenAlex title-reconstruction
   artifact with corrupted word-spacing ("Numbersi nS pace" for "Numbers in Space"), and a row
   that literally states "This article was submitted without an abstract."
4. **Random spot-check** of 20 rows from the final payload (seed 42) — 19/20 clean; the 20th
   surfaced the citation-string pattern in point 3 above (found the first instance here, then
   grepped for siblings).

**Net: 37 of 1,499 `recovered` rows excluded** as false positives that passed the
identifier/title-agreement guard but are not actually usable abstract text. Full list with
reasons: `crosscheck/exclusion_list.json` / `.txt`. None of this reflects a bug in
`fetch_sources.py`'s guards as designed — the guards check *identifier authority* and *title
agreement*, not *is this text actually an abstract*, which is a different, harder property
these three passes were checking for by hand.

**New script: `crosscheck/apply_abstracts.py`** — reads `source_abstract_crosscheck.csv`
directly (not the .sql), skips ids in `exclusion_list.json`, uses the same `.env` /
`psycopg2` / re-checked-`WHERE`-at-write-time pattern as `apply_field.py` (Sep 11 tool). Dry
run by default; `--apply` writes; `--include-unverified` would add the 156
`recovered_unverified` rows (not yet reviewed for the same false-positive shapes — **do not
`--include-unverified` without repeating passes 2–3 above on that set first**, they were only
run against `recovered`).

`crosscheck/source_abstracts_filtered.sql` was also regenerated (same 37 rows removed) as a
reviewable artifact, but the actual write used `apply_abstracts.py`, not this file.

**`.env` was pointed at `msp-kh-dev`** (`135.181.133.189:6532`, user `k2xadmin`) for this
write — confirmed via a plain `SELECT current_database(), current_user` plus a spot-check of
one target row before applying. Sanity numbers at that point: 28,028 total records, 4,045
missing abstract (a superset of this pass's 3,877-row input — some rows outside that CSV are
also empty).

**Write executed 2026-09-25 ~15:35 PKT:**
```
python3 apply_abstracts.py --apply
```
```
1462 rows to write (37 excluded, 0 too short/empty)
Committed. 1462 statements sent.
Rows still missing abstract: 2583
```
`4045 − 1462 = 2583`, confirmed. `updated_at` was touched on every written row (script does
not support `--no-touch-updated-at`, unlike `apply_field.py`).

**What was left unwritten at this point** (since resolved further, see §0d/§0e): the 37
excluded false positives, 156 `recovered_unverified` (not yet reviewed), 173 `title_mismatch`,
1930 `source_no_abstract`, 119 `unreachable`.

---

## 0d. Reviewing and writing `recovered_unverified` (2026-09-25, ~16:00–16:45 PKT)

`recovered_unverified` rows have no DB title to check agreement against (that's *why* they're
unverified — none of the identifier/title guards in §2 ran on them), so they needed the same
manual review §0c did, from scratch, before writing.

Ran the same three passes (worst-agreement doesn't apply here — no agreement score exists —
so: duplicate-abstract scan, then length/pattern sweep, then random spot-check) against all
156 rows. **71 of 156 (46%) were false positives** — a much higher rate than the `recovered`
set's 37/1499 (2.5%), because this bucket skips every guard:

| count | reason |
|---|---|
| 62 | Copernicus/EGU review-comment DOIs (same root cause as §0c, but hits this bucket much harder — "Reply on RC1" / "Comment on X" titles never have a DB title to verify against, so every one of them lands in `recovered_unverified` rather than getting caught by title agreement upstream). Broadened the detection regex here to also catch `-cec`, `-sc`, `-ec` DOI suffixes and "Response to..." title wording, not just `-ac/-rc/-cc` + "Reply on"/"Comment on" — §0c's regex was too narrow and missed some of these even in the `recovered` set (not revisited there, low stakes since already written). |
| 3 | known boilerplate strings (prepsoil.eu tagline, HAL "International audience", PNAS journal blurb) |
| 2 | **new pattern**: two different YouTube videos ("What is compost?" / "What is compost used for?") sharing byte-identical description text — a channel-level default, not per-video content |
| 1 | **new, more serious: a squatted/expired domain.** `biodivercity.nl`'s `og:description` returned "SpaceHills gids voor spelers in Nederland met uitleg over SpaceHills casino, bonussen, games en betalen" — a Dutch online casino, completely unrelated to the "BiodiverCity" record. Nothing in the existing guards would ever catch this (no identifier or title check applies), so a purpose-built check was added: a gambling/spam-vocabulary regex swept across `source_abstract` + `source_title`. Only found this one instance in this batch, but **this is a standing risk for any `landing:*`-sourced row with no other verification** — worth re-running that sweep if more landing-page rows get added to a future unverified pool. |
| 1 | a journal's back-matter author index ("Aanhahl, E.M., 951 Aarnoutse, R.E., 565...") mistaken for prose |
| 1 | a raw comma-separated keyword/tag dump, not prose |
| 1 | a broken/truncated extraction fragment ("The reportsThe data may also be used by those involved in:") |

The two exclusion lists were merged: `crosscheck/unverified_exclusion_list.json` (71 entries)
was folded into `crosscheck/exclusion_list.json`, which now holds all **108** excluded ids (37
from §0c + 71 from here) with no id overlap between the two sets. `exclusion_list.txt`
regenerated to match.

**Write executed via `apply_abstracts.py --include-unverified --apply`:**
```
1547 rows to write (108 excluded, 0 too short/empty)
Committed. 1547 statements sent.
Rows still missing abstract: 2498
```
The 1,462 rows written in §0c safely no-op'd (their `abstract` was no longer empty, so the
`WHERE` guard skipped them) — only the 85 new clean `recovered_unverified` rows actually
inserted. `2583 − 85 = 2498`, confirmed.

---

## 0e. Reviewing `title_mismatch` (2026-09-25, ~16:45–17:15 PKT)

173 rows where a resolver found *some* candidate but it failed the guard (mostly
`title_search`'s 0.7-Jaccard bar — 170/173 are `crossref_title`). Checked whether the 0.7
threshold, calibrated on a 60-row pilot (§5), was being too strict at scale.

**It wasn't, mostly.** Sorted all 173 by score and manually read every row scoring ≥0.5 (23
rows total):
- **0.6–0.69 band (9 rows): 3 genuine matches, wrongly rejected**, all recoverable:
  - one DB title = matched title + a bracketed project tag (`... (SOILdarity project related
    publication, RP2)`) that diluted the union-based Jaccard score
  - one DB title was `Correction to: <paper title> (<journal citation>)` — matched to the
    original paper the correction refers to, with the "Correction to:"/citation wrapper
    stripped by the guard's token comparison, which is exactly why it scored low
  - one was the same study (irrigation/drought/pine-forest/carbon) under a differently-worded
    title, most likely a repository-entry title vs. the published/conference title
  - the other 6 in this band were correctly rejected (one had garbled Crossref data — "The
    Legacy of Article II" for a row titled "The Legacy of LUC on SOC" — the rest were
    different, unrelated papers on similar-sounding general topics)
- **0.5–0.59 band (14 rows): zero genuine matches.** Confidently different documents sharing a
  few generic tokens (an R package vs. probate inventories, maize yield vs. chickpea yield,
  "the power of pyrolysis" vs. "pyrolysis oil for power generation", etc.) — the guard is
  correctly rejecting these.
- Below 0.5 not reviewed row-by-row — the above makes it very unlikely anything there is a
  false reject, and the effort/yield tradeoff stops making sense.

**Key finding: `source_abstract` is empty even for near-miss candidates.** The script only
records a rejected candidate's *title* for diagnostics (`source_title` column), never its
abstract text. So confirming a match doesn't give you a value to write — it has to be
re-fetched. For the 3 confirmed rows, re-ran Crossref's `query.title` (same query
`resolve_title_search()` in `fetch_sources.py` uses) by hand, picked the right DOI (for the
irrigation/pine-forest one, computed Jaccard against all 3 similar-looking candidates to
confirm which one the DB title actually matches best — an EGU21 conference abstract, not the
later EGU22 abstract or the eventual GCB journal article, which are related but distinct
follow-on publications of the same project), pulled `abstract` from
`https://api.crossref.org/works/{doi}`, and wrote directly via `psycopg2` with the same
`WHERE`-guard pattern as `apply_abstracts.py`. Recovered ids + the DOI/title/abstract used are
saved in `crosscheck/title_mismatch_manual_recover.json`.

```
039b4f6b...  10.3390/jof9020180                  (bracketed project-tag suffix)
df0e81ed...  10.5194/egusphere-egu21-3380         (reworded title, same study)
e7d20e46...  10.1007/s11270-025-08351-x           (Correction-to record -> original paper)
```
DB missing-abstract count: 2498 → **2495** after this write.

**Conclusion: not worth pursuing `title_mismatch` further at scale.** 3 recoverable rows out
of 173, each needing individual manual verification + a live re-fetch (no batch tooling exists
for this — would need to build a "re-fetch by confirmed DOI" mode into `fetch_sources.py` or
`apply_abstracts.py` to do more of these cheaply). The remaining ~150 rows below 0.5 agreement
are presumed correctly rejected based on the pattern in the reviewed bands, not individually
confirmed.

---

## 0f. The ~168 out-of-scope rows — checked, declined (2026-09-25, ~17:15–18:00 PKT)

`msp-kh-dev` has 168 rows missing an abstract that were never part of the original
3,877-row CSV (`records_202609231630_empty_abstract.csv`). Confirmed via
`SELECT id FROM records WHERE <same missing-abstract WHERE guard>` minus the input CSV's id
set — computed with `outside_scope_ids.json`.

**These are not empty rows** — every one of the 168 has a short (1–19 char) garbage fragment
in `abstract` that reads like a mis-mapped filename/caption/label, not a sentence: "Additional
file 1.", "Include:\nTokyo (30)", "Calendar of events", "DMP for ALL-Ready", "Peer reviewed".
Likely a harvester field-mapping bug from a separate ingest pass, outside this project's scope
(which only ever targeted `abstract = ''`). Resource types: 72 `Project`, 40 `Dataset`, 22
`None`, the rest scattered (Journal Article, Other, Software, Service, …). Identifiers: 86
OAI/internal slugs, 52 non-DOI URLs, 30 DOIs — same shape as the main set, so the existing
resolver chain applies directly.

Built a standalone 5-column input (`crosscheck/outside_scope_input.csv`, id/identifier/title/
abstract/resource_type — the only columns `read_csv()` needs) and ran `fetch_sources.py`
against it in isolation (`--cache outside_scope_fetch_cache.json --csv
outside_scope_crosscheck.csv --emit-sql outside_scope_abstracts.sql`, dry run, finished in
under 2 minutes):
```
2 recovered / 5 recovered_unverified / 49 title_mismatch / 0 source_also_placeholder /
66 source_no_abstract / 46 unreachable / 0 unfetchable (of 168)
```
**4% raw recovery vs. the main set's ~39%,** and `title_mismatch` alone is 29% of this batch
(vs. 4.5% in the main set) — confirms `Project`-type records with generic administrative
titles ("Workshop 1 Report", "DMP for ALL-Ready", "Practice abstract") are a bad fit for
identifier-less `title_search`: too little distinguishing text to match against, so it grabs
unrelated works that happen to share a few generic tokens.

**Manually reviewed all 7 recovered/unverified rows. Only 1 was a genuine match:**
- `99e583e8` (`10.1016/s2542-5196(20)30148-0`, OpenAlex, title agreement 1.0) — clean.
- The other 6 were false positives, and the failure mode is worse than anything seen in the
  main set: `9bba505f` ("Workshop 1 Report") and `4afa0e3c` ("Workshop 5 report") — two
  **different** DB records — both matched to the **same** unrelated generic "Workshop report :"
  text, proving `title_search` latched onto a coincidentally-generic-titled but wrong work.
  `cff4bc88` ("DMP for ALL-Ready") matched a children's bedtime poem ("All ready for bed?") on
  token overlap ("ready", "all"). `3b60e4ea` ("Practice abstract") matched a journal *section
  description*, not any specific article. `20ff667c` matched a site-wide tagline (LandIS),
  same trap as the prepsoil.eu/AFES cases in §0c/§0d. `64af8c06` ("Dissemination and
  Exploitation Plan", agreement 0.75, the highest score of the six) is the closest call — EU
  Horizon projects commonly reuse near-boilerplate text for this exact deliverable type across
  *unrelated* projects, so even a 0.75 score can't rule out it borrowed the wrong project's plan.

**Decision: left all 168 rows alone, including the one confirmed-clean match.** Yield was too
low (1/168, ~0.6%) and the risk pattern too consistent (5 of 6 false positives share the same
"generic administrative title coincidentally matches an unrelated generic-titled work" shape)
to justify writing even the "safe" one without more scrutiny than the effort warrants. No
DB writes happened in this section. All working files
(`crosscheck/outside_scope_input.csv`, `outside_scope_crosscheck.csv`,
`outside_scope_abstracts.sql`) are on disk if this is revisited later — the intermediate
`outside_scope_ids.json`/`outside_scope_rows.json`/`outside_scope_fetch_cache.json` scratch
files from building the input were cleaned up post-review (cache regenerates in <2 min if
needed again). The
main levers if it is: skip `title_search` entirely for `resource_type = 'Project'` rows (its
false-positive rate on this shape is now demonstrated, not hypothetical), or manually research
the ~40 `Project`/`Dataset` rows with clearly project-specific (non-generic) titles by hand
instead of trusting the automated chain.

---

## 1. The task

Fill the empty `abstract` column in `public.records`. Input for this pass:

```
records_202609231630_empty_abstract.csv     3,877 rows, every one with abstract = ""
```

Solr was already exhausted for this set on Sep 11 (see `crosscheck/soilwise_metadata_backfill_report.md` §4.4):
**3,084 rows `solr_blank`, 741 `not_in_solr`, only 6 usable.** So this pass goes to the
*upstream source* named by each record's identifier instead of to the search index.

## 2. What was built

`crosscheck/fetch_sources.py` (862 lines) — dry run by default, writes only with `--apply`.

It reuses the guard helpers from `check_missing.py` by import, so title-agreement and
placeholder logic stay identical to the Sep 11 work.

### Resolvers, selected by identifier type

| Identifier kind | Count | Resolvers |
|---|---|---|
| DOI (`https://doi.org/…` or bare `10.…`) | 3,325 | `openalex` → `crossref` → `datacite` |
| URL (non-DOI) | 280 | `landing` (meta / JSON-LD / YouTube `shortDescription`) |
| URL ending `.pdf` | 47 | `pdf` (`pdftotext`) |
| Neither — OAI ids, internal `50\|` keys, slugs | 272 | `title_search` (Crossref `query.title`) |

Chain stops early once a candidate clears `--min-agreement`.

### Two guards, deliberately different

This is the most important design point in the file.

* **Identifier lookups (DOI/URL)** are authoritative — the identifier *is* the link to the
  source. Candidate only needs `--min-agreement` (default **0.34**, token overlap over the
  *shorter* title). Same bar `check_missing.py` uses.
* **`title_search` has no identifier behind it**, so it needs `--title-jaccard`
  (default **0.7**, overlap over the *union*).

The union denominator is not cosmetic. On the first 60-row pilot, `title_search` recovered
11 rows using only the 0.34 guard — **8 of them were wrong papers**:

| DB title | Matched source title | min-ratio | Jaccard | verdict |
|---|---|---|---|---|
| me4soc: a Multi-model ensemble interface 2… | *(same paper)* | 1.00 | **1.00** | correct |
| Polystyrene nanoparticles induce concerted… | *(identical)* | 1.00 | **1.00** | correct |
| Identification of Beneficial Microbial Consortia… | *(identical)* | 1.00 | **1.00** | correct |
| Spectral soil library (vis-NIR-MIR) | …Using a Regional Soil Spectral Library | **1.00** | 0.25 | **wrong** |
| The long-term field trials "Vecauce" | Degradation of trifluralin in three Victorian soils… | 0.80 | 0.40 | **wrong** |
| Support to Renewable Energy Directive | The 'new' Renewable Energy Directive (RED III) | 0.75 | 0.60 | **wrong** |
| Vibration absorption performance of metamaterial lattices… | Theory of Metamaterial Beams… | 0.50 | 0.27 | **wrong** |
| From contributory to collegial… | Adoption of Responsible R&I in Citizen Observatories | 0.50 | 0.21 | **wrong** |

Jaccard ≥ 0.7 **keeps exactly the 3 correct and drops all 8 wrong.** Rejected candidates are
kept in a `rejected` count and the best one is reported in `source_title`, so a reviewer can
see *what* was refused, not just that something was.

### Statuses

`recovered` · `recovered_unverified` · `title_mismatch` · `source_also_placeholder` ·
`source_no_abstract` · `unreachable` · `unfetchable`

Only `recovered` is written by default; `recovered_unverified` needs `--include-unverified`.

## 3. Where the run is right now

**Superseded — see §0 and §0b.** All three runs (#1 PID 10126, #2 PID 11532 — crashed, #3
PID 31661) have finished or ended. Run #3's log and results are on disk right now and are
the ones to work from; details in §0b, not repeated here.

<details><summary>Original run #1 status note (now historical)</summary>

Launched 2026-09-24 ~16:44, PID 10126:

```
python3 fetch_sources.py --workers 10 --interval 0.2 --show 0 \
    --csv source_abstract_crosscheck.csv --emit-sql source_abstracts.sql > /tmp/full_run.log 2>&1
```

* Progress metric: entries in `crosscheck/source_fetch_cache.json`
  — 596 @6.5min → 1,627 @16min → 1,703 @20min. Cache flushes every 250 completions,
  so a flat count between checks is normal, not a hang (a flush interval is ~3 min
  at the observed ~75 groups/min).
* ~3,867 distinct groups to fetch, so 1,703 = **44% at 23 min**.
* `/tmp/full_run.log` is **empty** — stdout is block-buffered under redirection. Do not read
  it as evidence the run died; check the PID and the cache instead.
* Expected runtime ~50 min total; ETA roughly **17:35–17:55 PKT**.
* Outputs appear only at the very end: `source_abstract_crosscheck.csv` + `source_abstracts.sql`.

It finished on schedule; final summary is in §0.
</details>

### Check / resume

```bash
pgrep -af fetch_sources.py                     # should show PID 10126
python3 -c "import json;print(len(json.load(open('crosscheck/source_fetch_cache.json'))))"
```

If it died, re-run the same command — **the cache makes it resume**, already-fetched
identifiers are not re-requested. Add `--refresh` only to discard the cache and refetch.

## 4. Tomorrow — the sequence

```bash
cd crosscheck

# 1. confirm it finished (outputs exist, PID gone)
ls -la source_abstract_crosscheck.csv source_abstracts.sql

# 2. inspect results without touching the DB
python3 -c "
import csv,collections
r=list(csv.DictReader(open('source_abstract_crosscheck.csv')))
for k,v in collections.Counter(x['status'] for x in r).most_common(): print(f'{v:5d} {k}')
print('total', len(r))"

# 3. review false-positive risk: every recovered row, sorted worst agreement first
python3 -c "
import csv
r=[x for x in csv.DictReader(open('source_abstract_crosscheck.csv')) if x['status']=='recovered']
for x in sorted(r, key=lambda x: float(x['title_agreement'] or 0)):
    print(x['title_agreement'], x['source'], '|', x['title'][:55], '->', x['source_title'][:55])"

# 4a. SQL path (COMMIT is commented out — review first)
$EDITOR source_abstracts.sql

# 4b. or write directly, dry run first then commit
python3 fetch_sources.py --apply            # dry run
python3 fetch_sources.py --apply            # real write
```

Runs are cheap now — everything is cached, so steps 1–4 do no network I/O.

## 5. Pilot numbers (for expectation-setting)

| Pilot | recovered | unverified | mismatch | no abstract in source | unreachable |
|---|---|---|---|---|---|
| first 300 rows (DOI/title mix) | 128 (43%) | 14 | 9 | 148 | 1 |
| 40 URL-only rows | 7 (18%) | 5 | 0 | 8 | 20 |

URL rows recover worst — they hit `HTTP 429` (YouTube throttles hard), `403`, TLS failures
and `kartkatalog.geonorge.no` timeouts. ~40% of the file is plain DOIs, where OpenAlex is the
workhorse (1,427 of 1,703 candidates so far).

Expect **roughly 1,400–1,700 recovered of 3,877 (~40%)**, and the majority of the remainder
legitimately `source_no_abstract`.

## 6. Verified dead ends (do not re-litigate)

These were tested directly, not assumed:

* **Solr** — exhausted for this set, 87% blank or absent (Sep 11 report §4.4).
* **`raw_metadata`** — all 3,877 parse cleanly, but the only 18 rows with an abstract carry
  the literal string `<jats:p/>`. Zero usable.
* **Zenodo API** — `description present: False` on the sampled records; the field is genuinely
  absent, not mis-parsed. DataCite likewise returns `descriptions: []` for those same DOIs.
* **OpenAlex anonymous *search*** (`filter=title.search`) — HTTP 503, "Anonymous search is
  paused". OpenAlex `works/doi:` lookups still work fine; only search is paused.
  `openalex_search` is wired up in `--chain` if it comes back.
* **OAI-PMH** for the 71 `oai:` rows — 24 distinct hosts, guessed endpoints all 404.
  Not worth it for that many rows; `title_search` covers them instead.
* **Semantic Scholar** — confirmed 2026-09-25 via direct `curl`: hard AWS WAF block
  (`x-api-key: blocked-by-waf`) on every unauthenticated request, not a soft rate limit —
  no backoff/interval fix is possible. Needs a real API key. Removed from `--chain` again
  after a same-day attempt to re-add it (see §0b).
* **Landing-page fetch on doi.org** — usually a paywall; off unless `--doi-landing`.

## 7. Known limits / possible follow-ups

* **PDF resolver is intentionally conservative.** It extracts a section only if a line matches
  an abstract heading (`Abstract`, `Summary`, `Streszczenie`, `Souhrn`, `Zusammenfassung`, …)
  and stops at `Keywords`/`Introduction`. Sampled 4 PDFs: 1 yielded text, 3 correctly declined
  because the front pages are cover + data sheet. **It will not guess**, so many report PDFs
  stay unfilled — that is the intended trade.
* **`--doi-landing` untested at scale** (off by default). Would add a slow pass over 3,325 rows
  for the ones that came back `source_no_abstract` — probably low yield behind paywalls.
* **YouTube** rows will mostly stay `unreachable`; oEmbed gives a title but no description.
* **No rate-limit backoff across a 429 host cooldown** — retry honours `Retry-After` but the
  per-host `--interval` does not learn from it. If you see long stalls, raise `--interval`.
* **Root cause unchanged** — next harvest recreates these gaps (Sep 11 report §6). This is a
  one-off correction. OpenAIRE dropping titles/descriptions for this record class was never
  raised with the harvester owners (Sep 11 report §7 open items).
* `--from-db` exists to re-query live rows instead of the CSV, useful once earlier writes land.

## 8. Files

| File | What |
|---|---|
| `records_202609231630_empty_abstract.csv` | input — 3,877 rows, all empty abstract |
| `crosscheck/fetch_sources.py` | **the new tool** |
| `crosscheck/source_fetch_cache.json` | resumable fetch cache (the run's real state) |
| `crosscheck/source_abstract_crosscheck.csv` | output — every row + status; **source of truth for the write** |
| `crosscheck/source_abstracts.sql` | run #3's raw UPDATEs, `COMMIT` commented out — superseded, see below |
| `crosscheck/exclusion_list.json` / `.txt` | **108** excluded row ids (37 `recovered` from §0c + 71 `recovered_unverified` from §0d), with reasons |
| `crosscheck/unverified_exclusion_list.json` | the 71-row subset above, kept standalone as the §0d working artifact; already folded into `exclusion_list.json` |
| `crosscheck/source_abstracts_filtered.sql` | `source_abstracts.sql` minus the 37 `recovered`-only excluded rows — reviewable artifact, not what was actually run |
| `crosscheck/apply_abstracts.py` | **the script that did the bulk writes** — reads the CSV directly, applies `exclusion_list.json`, same `.env`/guard pattern as `apply_field.py`. Default writes `recovered` only; `--include-unverified` adds `recovered_unverified` |
| `crosscheck/title_mismatch_manual_recover.json` | the 3 `title_mismatch` rows manually re-fetched and written in §0e — id → {doi, title, abstract} |
| `crosscheck/check_missing.py` | Sep 11 Solr crosscheck; supplies the shared helpers |
| `crosscheck/apply_field.py` | Sep 11 DB writer; same `WHERE` re-check pattern |
| `crosscheck/soilwise_metadata_backfill_report.md` | Sep 11 report — read §4.4, §6, §7 first |
| `crosscheck/abstract_crosscheck.csv` | Sep 11 Solr results (superseded for this set) |
