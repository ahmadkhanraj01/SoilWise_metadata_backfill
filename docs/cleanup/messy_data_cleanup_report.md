# Messy Data Cleanup — findings and approach

**Date:** 2026-09-30
**Source:** live `msp-kh-dev` database, `records` table (25,130 rows total), fields inside
`raw_metadata` jsonb plus the top-level `title`/`abstract` columns.
**Status:** Dry run complete and manually verified. **Not yet applied to the DB** — pending
final go-ahead.

This follows up on `messy_data_report.md` (which scanned a Sep 25 Solr export that no longer
exists on disk). This pass re-scans the *live* DB directly, so the counts differ from that
report, and separates real corruption from false-positive pattern matches.

## 1. What was scanned

All free-text fields the original report covered: `title`, `abstract`, `persons`,
`organizations`, `contact_persons`, `contact_organizations`, `subjects`, `matched_subjects`,
`view_authors`, `view_contacts`. Checked for: emoji, pipe `|`, backslash `\`, control
characters, mojibake (encoding artifacts), leaked HTML/XML tags, literal URL-encoding, and
irregular spacing.

**5,750 of 25,130 records** matched at least one raw pattern. Breakdown (`output/scan/messy_records.csv`):

| Field | Issue | Records |
| :--- | :--- | ---: |
| abstract | html_tag (JATS/XML markup, e.g. `<jats:title>`, `<jats:p>`) | 3,421 |
| abstract | multi_space | 2,749 |
| title | multi_space | 305 |
| title | html_tag (`<sub>`, `<i>`) | 223 |
| abstract | pipe `\|` | 92 |
| view_contacts | multi_space | 82 |
| subjects | html_tag | 59 |
| contact_organizations | multi_space | 53 |
| abstract | backslash | 50 |
| view_contacts | backslash | 47 |
| contact_persons | multi_space / pipe | 29 / 29 |
| view_contacts | pipe | 29 |
| persons / view_authors | pipe | 26 each |
| subjects | multi_space / pipe | 21 / 14 |
| title | pipe | 11 |
| abstract | emoji / url_encoded / mojibake | 9 each |
| (long tail: organizations, control_char, etc.) | | 1–7 each |

## 2. Separating real corruption from false positives

A raw pattern match isn't the same as broken data. Pulling actual samples from the DB showed
several categories that **looked** messy but weren't:

- **`pipe` in `subjects` and `title`** — intentional separator, e.g. `"soil incubation |
  microorganisms"`, or YouTube titles like `"PREPSOIL Interview | Niels Halberg"`. Left alone.
- **`pipe`/`backslash` in `abstract` body text** — legitimate content: LaTeX math (`\Delta`,
  `\pm`), R code fragments (`strsplit(i, "\\.")`), markdown escapes (`Gd15N\_Random\_Forest`),
  grant numbers with a literal slash-like separator (`LRB17\1019`). Left alone (only 1 of ~76
  matches was genuine corruption — see below).
- **`backslash` in `view_contacts`/`view_authors`** — these fields store JSON-encoded strings
  (e.g. `{"person":"","organization":"...\"Data Analysis & Simulation\""}`); the backslash is
  just correct JSON escaping and disappears on parse. Not corruption.
- **`url_encoded` in abstracts** — `%XX` sequences sitting inside embedded URLs, not stray
  encoding artifacts. Left alone.

**What turned out to be real, confirmed against samples:**

- Leaked `<jats:...>`/`<sub>`/`<i>` tags in abstract/title/subjects — genuine markup leakage
  from source APIs.
- Irregular/doubled spacing throughout.
- A literal `"|"` as the *entire* value of a persons/contact_persons/view_authors/
  view_contacts entry (~85 records) — a placeholder for a missing name, not real data.
- Mojibake: UTF-8 text re-decoded as Latin-1/CP1252, e.g. `SÃ¸rmo, Erlend` (should be `Sørmo,
  Erlend`), `Ã…sli, Geir Wold` (should be `Åsli, Geir Wold`), `Â\xa0` before words (stray
  non-breaking-space artifact).
- Double-escaped quotes in plain-string fields, e.g. `contact_persons` value literally
  containing `Latvian State Forest Research Institute \\"Silava\\"` (raw backslash characters
  in the text, not JSON escaping) — should read `..."Silava"`.
- One abstract (`10.1016/j.geoderma.2017.10.049`) has a name double-mangled to literal text
  `S\xF6derstr\xF6m` instead of `Söderström` — a one-off, flagged for manual fix rather than
  automated (too rare a pattern to safely generalize a rule from).
- One abstract (`10.1023/b:biog.0000005324.37711.63`) has stray `\x7f` (DEL) control characters
  in place of what was probably a superscript minus sign (`kg ha⁻¹ yr⁻¹` → `kg ha\x7f1 yr\x7f1`)
  — also a one-off, flagged for manual review.

## 3. Cleaning approach (dry run)

For each record, applied in order, only where genuinely matched:

1. **Strip HTML/XML tags** in `title`, `abstract`, `subjects` — replaced with a space (not
   deleted outright) so words don't merge (`AbstractUndisturbed` → `Abstract Undisturbed`),
   then space cleanup.
2. **Unescape stray backslash-quotes** (`\+"` → `"`) in plain-string fields.
3. **Fix mojibake** using `ftfy`, with quote-curling/ligature/character-width normalization
   turned off so it only reverses actual encoding corruption and never touches legitimately
   curly quotes, dashes, or ligatures. A secondary fallback (manual CP1252-as-UTF8 round-trip)
   catches the rarer cases `ftfy`'s heuristics miss (e.g. `Åsli`), and is only kept if it
   provably reduces the mojibake marker count.
4. **Drop literal `"|"` placeholder entries** from persons/organizations/contact_persons/
   contact_organizations/matched_subjects lists, and clear `"person"`/`"organization"` to `""`
   inside `view_authors`/`view_contacts` JSON strings when they're exactly `"|"`.
5. **Collapse multi-space** to single space (after tag-stripping, everywhere).

Explicitly **not** touched: `pipe`/`backslash` inside `subjects`/`title`/`abstract` body text,
`url_encoded` sequences, and the two one-off corruption cases (Söderström, the DEL-character
one) — these need a human call, not a blanket rule.

## 4. Result of dry run

- **6,412 of 25,130 records changed** (some records touched on more than one field).
- Per-field change counts:

| Field | Records changed |
| :--- | ---: |
| abstract | 5,699 |
| title | 576 |
| subjects | 449 |
| view_contacts | 224 |
| contact_persons | 161 |
| contact_organizations | 66 |
| view_authors | 35 |
| persons | 28 |
| organizations | 8 |

- Full record-level diff: `output/dryrun/messy_cleanup_dryrun.csv` (one row per record, `changes` column as
  JSON).
- Flat, spreadsheet-friendly diff: `output/dryrun/messy_cleanup_before_after.csv` (one row per field change,
  separate `before`/`after` columns) — **7,246 rows**.
- Manually spot-checked across every affected field (see conversation) — no unintended content
  loss found; only tag/whitespace/mojibake/placeholder noise removed.

## 5. Not yet done

- No DB writes have happened. This is purely the dry-run/diff stage.
- Awaiting sign-off on `output/dryrun/messy_cleanup_before_after.csv` before running the actual `UPDATE`
  pass (in a transaction, with a row-count check after).
