#!/usr/bin/env python3
"""Analyse a dry-run diff and emit a typed before/after CSV. READ-ONLY (files only).

Reads ``<prefix>_before_after.csv`` (from dry_run.py) and produces:

  <prefix>_changes.csv     same grain, one row per change:
                           id, identifier, field, target, change_types, primary,
                           before, after
                           -- this is the spreadsheet-friendly "before vs after,
                           and which field it came from" artifact.
  <prefix>_summary.csv     aggregated: field, target, change_type, rows, records

``change_types`` is a semicolon-joined list of everything that applies to that
row (a row can be tag-stripped *and* space-collapsed *and* entity-decoded);
``primary`` in the summary uses this priority:
    html_tag > placeholder_drop > stray_quote > mojibake > entity_decoded
    > bom_removed > linebreak > multi_space > nfc > whitespace_only > other

``mojibake`` covers both ftfy markers (â..) and raw CP1252 C1 bytes that were
mapped back (\\x96 -> en dash); ``entity_decoded`` includes hex entities
(&#x0025;); ``linebreak`` is CR -> LF normalisation.

Rows classed ``other`` are the ones worth eyeballing: no known rule explains
them.

Usage:
    python3 analyse_diff.py messy_cleanup_v2_before_after.csv
    python3 analyse_diff.py <csv> --samples 3
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_rules import (  # noqa: E402
    EMOJI_RE,
    HTML_TAG_RE,
    MOJIBAKE_RE,
    MULTISPACE_RE,
    STRAY_BACKSLASH_QUOTE_RE,
)

ENTITY_RE = re.compile(r"&#\d{1,4};?|&#x[0-9a-fA-F]{1,6};?|&[a-zA-Z][a-zA-Z0-9]{1,9};")
C1_RE = re.compile(r"[\x80-\x9f]")  # raw CP1252 control bytes = mojibake residue
PRIORITY = ["html_tag", "placeholder_drop", "pipe_strip", "emoji_removed", "stray_quote", "mojibake",
            "entity_decoded", "bom_removed", "linebreak", "multi_space",
            "nfc", "whitespace_only", "other"]


def _leaf_texts(cell: str) -> str:
    """The real strings inside a JSON cell.

    List cells are json.dumps'd, so a real carriage return shows up there as the
    two characters backslash-r (and twice-escaped again inside view_* objects).
    Joining the decoded leaves lets the checks see the actual characters while
    the raw cell keeps the escaped view -- counts are taken over both.
    """
    parts = [cell]
    if cell.lstrip().startswith(("[", "{")):
        def walk(obj) -> None:
            if isinstance(obj, str):
                parts.append(obj)
                if obj.lstrip().startswith(("{", "[")):
                    try:
                        walk(json.loads(obj))
                    except (ValueError, TypeError):
                        pass
            elif isinstance(obj, list):
                for x in obj:
                    walk(x)
            elif isinstance(obj, dict):
                for v in obj.values():
                    walk(v)
        try:
            walk(json.loads(cell))
        except (ValueError, TypeError):
            pass
    return "".join(parts)


def _count(rx: re.Pattern, s: str) -> int:
    return len(rx.findall(s))


def _dropped_placeholder(before: str, after: str) -> bool:
    """True when a list row lost a '|' / '' placeholder entry, or cleared one."""
    if not before.lstrip().startswith("["):
        return False
    try:
        lb, la = json.loads(before), json.loads(after)
    except (ValueError, TypeError):
        return False
    if not (isinstance(lb, list) and isinstance(la, list)):
        return False
    for x in lb:
        if x not in la and isinstance(x, str) and x.strip() in ("|", ""):
            return True
    # view_* JSON objects whose person/organisation was '|'
    for x, y in zip(lb, la):
        if x == y or not isinstance(x, str):
            continue
        try:
            ox, oy = json.loads(x), json.loads(y)
        except (ValueError, TypeError):
            continue
        if isinstance(ox, dict) and isinstance(oy, dict):
            for k in ("person", "organization"):
                if ox.get(k) == "|" and oy.get(k) == "":
                    return True
    return False


def _pipe_stripped(before: str, after: str) -> bool:
    """True when the row lost pipes (edge pipes on a name, or a dropped ``|`` entry)."""
    return _leaf_texts(before).count("|") > _leaf_texts(after).count("|")


def classify(before: str, after: str) -> list[str]:
    kinds: list[str] = []
    if _count(HTML_TAG_RE, before) > _count(HTML_TAG_RE, after):
        kinds.append("html_tag")
    if _dropped_placeholder(before, after):
        kinds.append("placeholder_drop")
    if _count(EMOJI_RE, before) > _count(EMOJI_RE, after):
        kinds.append("emoji_removed")
    if _pipe_stripped(before, after):
        kinds.append("pipe_strip")
    if (STRAY_BACKSLASH_QUOTE_RE.search(before)
            and before.count("\\") > after.count("\\")):
        # fewer backslashes, and the pattern that justifies removing them is present
        kinds.append("stray_quote")
    if (_count(MOJIBAKE_RE, before) > _count(MOJIBAKE_RE, after)
            or _count(C1_RE, before) > _count(C1_RE, after)):
        # ftfy markers, or raw CP1252 bytes mapped back (\\x96 -> en dash, â\\x80\\x99 -> ’)
        kinds.append("mojibake")
    if _count(ENTITY_RE, before) > _count(ENTITY_RE, after):
        kinds.append("entity_decoded")
    if before.count("\ufeff") > after.count("\ufeff"):
        kinds.append("bom_removed")
    if _leaf_texts(before).count("\r") > _leaf_texts(after).count("\r"):
        kinds.append("linebreak")
    if MULTISPACE_RE.search(before) and not MULTISPACE_RE.search(after):
        kinds.append("multi_space")
    if unicodedata.normalize("NFC", before) != before:
        kinds.append("nfc")
    if not kinds and _nowhitespace(before) == _nowhitespace(after):
        # pure whitespace difference of any shape: trailing space inside a list
        # item, collapsed runs, a dropped \r, stripped ends...
        kinds.append("whitespace_only")
    if not kinds:
        kinds.append("other")
    return kinds


def _nowhitespace(s: str) -> str:
    return re.sub(r"\s+", "", s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path", help="a <prefix>_before_after.csv from dry_run.py")
    ap.add_argument("--samples", type=int, default=2,
                    help="example rows to print per field")
    args = ap.parse_args()

    src = Path(args.csv_path)
    stem = src.name.replace("_before_after.csv", "")
    out_dir = src.parent
    changes_path = out_dir / f"{stem}_changes.csv"
    summary_path = out_dir / f"{stem}_summary.csv"

    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    if rows and "target" not in rows[0]:
        sys.exit("input has no 'target' column -- this is a v1 CSV; re-run dry_run.py")

    typed = []
    per_field: dict[str, Counter] = defaultdict(Counter)          # field -> primary -> rows
    per_field_recs: dict[str, set] = defaultdict(set)             # field -> records
    per_combo: dict[tuple, dict] = {}                             # (field,target,primary)
    samples: dict[str, list] = defaultdict(list)

    for r in rows:
        kinds = classify(r["before"], r["after"])
        primary = next(p for p in PRIORITY if p in kinds)
        typed.append({
            "id": r["id"], "identifier": r["identifier"],
            "field": r["field"], "target": r["target"],
            "change_types": ";".join(kinds),
            "primary": primary,
            "before": r["before"], "after": r["after"],
        })
        per_field[r["field"]][primary] += 1
        per_field_recs[r["field"]].add(r["id"])
        key = (r["field"], r["target"], primary)
        d = per_combo.setdefault(key, {"rows": 0, "records": set()})
        d["rows"] += 1
        d["records"].add(r["id"])
        if len(samples[r["field"]]) < args.samples:
            samples[r["field"]].append(typed[-1])

    # ---- write typed changes CSV ----
    with open(changes_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "identifier", "field", "target",
                                          "change_types", "primary", "before", "after"])
        w.writeheader()
        w.writerows(typed)
    print("written", changes_path, f"({len(typed)} rows)")

    # ---- write summary CSV ----
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["field", "target", "change_type",
                                          "rows", "records"])
        w.writeheader()
        for (field, target, primary), d in sorted(
                per_combo.items(), key=lambda x: (-x[1]["rows"], x[0])):
            w.writerow({"field": field, "target": target, "change_type": primary,
                        "rows": d["rows"], "records": len(d["records"])})
    print("written", summary_path, f"({len(per_combo)} rows)\n")

    # ---- report ----
    total_recs = len({r["id"] for r in rows})
    print(f"changes: {len(rows)} rows across {total_recs} records\n")

    print("per field (rows, records, primary breakdown):")
    print(f"  {'field':26s} {'rows':>6s} {'recs':>6s}  breakdown")
    for field, cnt in sorted(per_field.items(), key=lambda x: -sum(x[1].values())):
        total = sum(cnt.values())
        detail = ", ".join(f"{k}={v}" for k, v in cnt.most_common())
        print(f"  {field:26s} {total:6d} {len(per_field_recs[field]):6d}  {detail}")

    print("\nprimary change type across all rows:")
    allprim: Counter = Counter()
    for cnt in per_field.values():
        allprim.update(cnt)
    for k, v in allprim.most_common():
        print(f"  {k:20s} {v:6d}")

    print(f"\nexample changes ({args.samples} per field):")
    for field, items in samples.items():
        print(f"  --- {field} ---")
        for it in items:
            b = it["before"][:150].replace("\n", " ")
            a = it["after"][:150].replace("\n", " ")
            print(f"    [{it['identifier']}] {it['target']} ({it['change_types']})")
            print(f"      before: {b!r}")
            print(f"      after : {a!r}")

    others = [t for t in typed if t["primary"] == "other"]
    print(f"\nrows where no known rule explains the change (primary=other): {len(others)}")
    for t in others[:10]:
        print(f"    [{t['identifier']}] {t['field']}/{t['target']}")
        print(f"      before: {t['before'][:200]!r}")
        print(f"      after : {t['after'][:200]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
