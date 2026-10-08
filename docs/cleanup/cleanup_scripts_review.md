# Cleanup scripts — review findings & TODO

**Date:** 2026-09-30
**Scope:** review of the scripts behind `messy_data_report.md` and `messy_data_cleanup_report.md`
**Status:** findings only — **nothing applied to the DB**, no scripts modified yet.

---

## 0. Where the scripts are

**Now: `scripts/` in this repo** (fixed + documented — see `scripts/README.md`).

Originally they lived in a session scratchpad under `/tmp/claude-1000/...`, which has
**since been wiped** — the three core scripts were rewritten with the fixes below and the
seven inspection helpers were reconstructed from this session's log:

```
scripts/
    cleanup_rules.py     shared regexes + cleaning functions (single source of truth)
    find_messy.py        scan  → output/scan/messy_records.csv
    dry_run.py           dry run → <prefix>_dryrun.csv + <prefix>_before_after.csv
    verify_diff.py       gate  → exit 1 if the diff is not safe to apply
    db_check.py, db_check2.py, inspect_types.py, test_parse.py,
    inspect_singles.py, inspect_backslash.py, sample_issues.py   (helpers)
    README.md
```

Still no apply script — consistent with the report's "dry run only, not yet applied".

---

## 1. What checked out ✅

| Claim in `messy_data_cleanup_report.md` | Verified |
| :--- | :--- |
| 6,412 of 25,130 records changed | ✅ exact |
| 7,246 rows in `output/dryrun/messy_cleanup_before_after.csv` | ✅ exact |
| Per-field change counts (abstract 5,699 / title 576 / subjects 449 / …) | ✅ all match |
| No unintended content loss | ✅ token-level diff of all 6,275 scalar changes — only "lost" tokens are HTML-entity artifacts (`lt`, `gt`, `8211`, …) being correctly resolved; no real words disappear |
| Mojibake fixes correct | ✅ `FAOâ€™s`→`FAO’s`, `SÃ¸rmo`→`Sørmo`, `Â `→U+00A0 |
| No residual multi-space / mojibake in cleaned values | ✅ 0 |
| 16 stray `\"…\"` fixes in abstracts are legitimate | ✅ all are double-escaped quotes inside text, not LaTeX/R code |
| No DB writes have happened | ✅ `dry_run.py` only writes CSV |

---

## 2. Bug 1 — HTML tags get re-created *after* stripping (38 records)

`clean_plain_text()` runs **strip → unescape → ftfy → collapse**. ftfy's `unescape_html="auto"`
only preserves entities when a *literal* `<` is present in its input. By the time ftfy runs,
`strip_html()` has already removed the `<jats:p>` guard, so ftfy decodes entities at full depth
and rebuilds tags:

```
before: <jats:p>&amp;lt;p&amp;gt;The Arctic has been warming ...
after:  <p>The Arctic has been warming ...          ← raw tag reintroduced

before: ... air–sea CO&lt;sub&gt;2&lt;/sub&gt; fluxes ...   (title)
after:  ... air–sea CO<sub>2</sub> fluxes ...
```

**Impact:** 9 titles + 29 abstracts still contain raw HTML tags after the pass, plus 1 flagged
record that was never touched at all (scanner vs cleaner regexes differ —
scanner `<[a-zA-Z/][^>]{0,60}>` vs cleaner `</?[a-zA-Z][a-zA-Z0-9:_-]*(?:\s[^>]*)?/?>`).

**Fix (pick one):**

1. Strip again after ftfy: `collapse_space(strip_html(fix_mojibake(...)))` — simplest, and the
   pass-1 strip is still needed so `unescape_html="auto"` doesn't get confused by real markup.
2. Or call ftfy first with `TextFixerConfig(unescape_html=True)` → then strip tags → then a
   second ftfy pass for mojibake only.

Also align the scanner's and the cleaner's tag regexes so a flagged record is always a record
the cleaner can fix.

---

## 3. Bug 2 — the top-level `title` / `abstract` columns are never written

`dry_run.py` reads `raw_metadata[field]` first (falling back to the column only when the jsonb
key is absent) and builds **only `new_raw`**. Nothing syncs the columns (confirmed: no triggers
on `records`). But `messy_data_cleanup_report.md` §1 says the columns are in scope.

Dirty top-level columns that would remain dirty after the pass as written:

| Column | Issue | Records left dirty |
| :--- | :--- | ---: |
| `title` | HTML tags | 223 |
| `title` | multi-space | 306 |
| `abstract` | multi-space | 2,895 |
| `abstract` | mojibake | 12 |

Context: the two copies already differ for **7,074 abstracts / 1,746 titles**; the top-level
`abstract` is tag-free (3,420 records have tags *only* in `raw_metadata`), and
`crosscheck/apply_abstracts.py` writes `SET abstract = %s` **only to the column**
(1,696 abstracts exist only there).

**Rule for the apply pass:** clean **each copy independently** (run the same cleaner on the
column and on the jsonb key). Never sync one into the other — raw→column would overwrite
backfilled abstracts, column→raw would fabricate jsonb content.

---

## 4. Bug 3 — the diff doesn't say *where* the change goes

For **114 records** (14 titles, 100 abstracts) `raw_metadata` has no such key, so the "before"
value in the CSV comes from the top-level column — applying it as-is would **insert a brand-new
key into `raw_metadata`** that never existed.

`output/dryrun/messy_cleanup_before_after.csv` has columns `id, identifier, field, before, after` — no
`target`. Regenerate with a `target` column (`column` | `raw_metadata.<field>`) and decide
explicitly whether new keys should be created.

---

## 5. Undocumented changes (report understates the effect)

- **NFC normalization — 764 changes** (`Miličić`, `Lozano-Fondón`, `Özge`, `Sandén`:
  decomposed → composed). Good, but unmentioned; it is the main reason **713 records changed
  that `find_messy.py` never flagged** (6,412 changed vs 5,750 flagged).
- **HTML-entity decoding by ftfy** — `&amp;`→`&`, `&ndash;`→`–`, `&gt;`→`>`,
  `&nbsp;`→U+00A0, plus `restore_byte_a0` producing **6,081 non-breaking spaces**.
  Desirable, but it is also what triggers Bug 1, and §3 of the report ("only tag/whitespace/
  mojibake/placeholder noise removed") doesn't cover it.

Update the report's §3/§4 wording after the fix.

---

## 6. Minor code notes

- `clean_json_list()`: guard `isinstance(obj, dict)` after `json.loads` — a JSON item that
  parses to a list/str can hit `obj[k]` and raise.
- `clean_list_plain()`: drops `''` entries but only sets `changed = True` for non-empty drops,
  so an empty-entry-only change is computed and then silently discarded (field not written).
- Scanner's emoji regex includes arrows/symbols (`U+2190–21FF`, `U+2B00–2BFF`) → false
  "emoji" flags; narrow it if the scan is re-run.
- Scanner `MULTISPACE_RE = "  +"` vs cleaner `[ \t]{2,}` — tab/space combos are cleaned but
  never flagged (part of the changed-but-unflagged population).

---

## 7. TODO (in order)

- [x] **Copy the scratchpad scripts into the repo** (`scripts/`) — they lived in `/tmp`
      and have since been wiped; 7 inspection helpers were reconstructed, core three
      rewritten with fixes. See `scripts/README.md`.
- [x] **Fix Bug 1** — `cleanup_rules.clean_plain_text()` strips HTML tags again after
      ftfy; scanner and cleaner now share `cleanup_rules.py`.
- [x] **Fix Bug 2** — `dry_run.py` cleans the top-level `title`/`abstract` columns
      independently (never syncs the two copies).
- [x] **Fix Bug 3** — every diff row carries `target = both | column | raw_metadata`;
      jsonb keys are never created (the 114 rows are now `column` rows).
- [x] **Re-run dry run** → `output/dryrun/messy_cleanup_v2_dryrun.csv` + `output/dryrun/messy_cleanup_v2_before_after.csv`
      (v1 artifacts kept as-is), plus `output/scan/messy_records_v2.csv`.
- [x] **Re-verify** → `python3 scripts/cleanup/verify_diff.py output/dryrun/messy_cleanup_v2_before_after.csv`
      = **PASS**: 0 residual tags, 0 residual multi-space, 0 introduced mojibake,
      0 invalid targets, 0 content-loss suspects. 6,532 records / 9,083 rows.
- [ ] **Review the diff by hand** (`output/dryrun/messy_cleanup_v2_before_after.csv`, sort by
      `target`/`field`) — especially the 1,951 `column` rows, which are new.
- [ ] **Write the apply script** (does not exist yet): transactional, per-target updates
      (`UPDATE records SET <col> = %s` and `jsonb_set`/per-key update), reading the v2
      flat CSV, with
      - row-count check (expect 6,532 records / 9,083 rows),
      - before-image backup taken immediately before the write,
      - `COMMIT` only after counts match, plus a post-apply re-run of
        `scripts/cleanup/find_messy.py` + `scripts/cleanup/verify_diff.py`.
- [ ] **Update `messy_data_cleanup_report.md`** §3–§4 with the v2 numbers and with
      NFC normalization / HTML-entity decoding now documented as in-scope side effects.
- [ ] Get sign-off on the final before/after CSV, then run against `msp-kh-dev`.

---

## Reference: files involved

| File | Role |
| :--- | :--- |
| `messy_data_report.md` | Sep 25 Solr scan (older export, no longer on disk) — note: file appears truncated at "Did it get better or worse since July?" |
| `messy_data_cleanup_report.md` | Live-DB rescan + dry-run findings (the report this review checks) |
| `output/scan/messy_records.csv` | scanner output, 5,750 flagged records |
| `output/dryrun/messy_cleanup_dryrun.csv` | record-level diff, 6,412 records |
| `output/dryrun/messy_cleanup_before_after.csv` | flat diff, 7,246 rows — **the sign-off artifact** |
| `crosscheck/.env` | DB credentials used by all scripts |
| `crosscheck/apply_abstracts.py` | precedent for the apply pass (`SET abstract = %s, updated_at = now()`) |
