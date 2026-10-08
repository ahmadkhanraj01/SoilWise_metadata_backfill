#!/usr/bin/env python3
"""Dry run of the messy-metadata cleanup. READ-ONLY -- never writes to the DB.

Walks every record, applies the cleaning rules from ``cleanup_rules.py`` and
emits a diff instead of an UPDATE. Run it, review the CSV, then (only after
sign-off) build the apply pass from the same rows.

Usage:
    python3 dry_run.py                          # -> messy_cleanup_dryrun.csv
                                                #    messy_cleanup_before_after.csv
    python3 dry_run.py --out-prefix messy_cleanup_v2
    python3 dry_run.py --limit 500              # smoke test on 500 records

Outputs
-------
<prefix>_dryrun.csv         one row per changed record:
                            id, identifier, changes = JSON list of
                            {target, field, before, after}
<prefix>_before_after.csv   one row per change: id, identifier, target, field,
                            before, after  -- the review/sign-off artifact.

``target`` says where the cleaned value belongs:

    both            column and raw_metadata hold the identical value, so a
                    single row covers both writes
    column          the top-level records.title / records.abstract column only
    raw_metadata    a key that already exists in raw_metadata only

Design decisions (see ../cleanup_scripts_review.md for the bugs they fix)
------------------------------------------------------------------------
1. HTML tags are stripped twice -- once before ftfy and once after -- because
   ftfy's entity unescaping can rebuild markup that pass 1 already removed
   (``<jats:p>&amp;lt;p&amp;gt;`` -> ``<p>``).
2. The top-level title/abstract columns are cleaned as well as raw_metadata,
   each copy **independently**. They differ for ~7k records (backfilled
   abstracts live only in the column), so they must never be synced -- only
   cleaned with the same rules.
3. raw_metadata keys are **never created**: if raw_metadata has no
   ``abstract`` key, only the column row is emitted.
4. Pipes / LaTeX / URL-encoded sequences in body text are left alone
   (legitimate content, report section 2).

This performs only SELECTs. It never writes to the database.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_rules import (  # noqa: E402
    JSON_LIST_FIELDS,
    PLAIN_LIST_FIELDS,
    TEXT_TOP_FIELDS,
    clean_json_list,
    clean_list_html,
    clean_list_plain,
    clean_plain_text,
    HTML_STRIP_LIST_FIELDS,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TARGET_COL = "column"           # top-level title / abstract column
TARGET_RAW = "raw_metadata"     # existing key inside raw_metadata
TARGET_BOTH = "both"            # identical value in both places


def serialize(v) -> str:
    """CSV cell: strings as-is, lists/dicts as compact JSON."""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-prefix", default=str(PROJECT_ROOT / "output" / "dryrun"
                                                / "messy_cleanup"),
                    help="output files are <prefix>_dryrun.csv and "
                         "<prefix>_before_after.csv (default dir: output/dryrun)")
    ap.add_argument("--env", default=str(PROJECT_ROOT / "crosscheck" / ".env"))
    ap.add_argument("--limit", type=int, default=0,
                    help="only scan the first N records (smoke test)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing output files")
    args = ap.parse_args()

    prefix = Path(args.out_prefix)
    dry_path = prefix.with_name(prefix.name + "_dryrun.csv")
    flat_path = prefix.with_name(prefix.name + "_before_after.csv")
    existing = [str(p) for p in (dry_path, flat_path) if p.exists()]
    if existing and not args.force:
        sys.exit(f"refusing to overwrite: {', '.join(existing)}\n"
                 "pick another --out-prefix, or pass --force if you mean it "
                 "(the default prefix overwrites the v1 sign-off artifacts).")
    prefix.parent.mkdir(parents=True, exist_ok=True)

    load_dotenv(args.env)
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )
    cur = conn.cursor(name="dryrun", cursor_factory=psycopg2.extras.RealDictCursor)
    cur.itersize = 2000
    cur.execute("select id, identifier, title, abstract, raw_metadata from records")

    record_rows = []   # one per changed record
    flat_rows = []     # one per (record, target, field)
    n_scanned = 0
    by_target: Counter = Counter()
    by_field: Counter = Counter()

    for row in cur:
        n_scanned += 1
        if args.limit and n_scanned > args.limit:
            break
        raw = row.get("raw_metadata") or {}
        if not isinstance(raw, dict):
            raw = {}
        identifier = row.get("identifier")
        changes = []  # {target, field, before, after}

        def add(target: str, field: str, before, after) -> None:
            changes.append({"target": target, "field": field,
                            "before": before, "after": after})

        # --- title / abstract: column and raw_metadata cleaned independently ---
        for field in TEXT_TOP_FIELDS:
            col_val = row.get(field)
            raw_val = raw.get(field)
            col_new = (clean_plain_text(col_val, do_html=True)
                       if isinstance(col_val, str) and col_val else col_val)
            raw_new = (clean_plain_text(raw_val, do_html=True)
                       if isinstance(raw_val, str) and raw_val else raw_val)
            col_dirty = isinstance(col_val, str) and bool(col_val) and col_new != col_val
            raw_dirty = isinstance(raw_val, str) and bool(raw_val) and raw_new != raw_val
            if col_dirty and raw_dirty and col_val == raw_val:
                add(TARGET_BOTH, field, col_val, col_new)
            else:
                if col_dirty:
                    add(TARGET_COL, field, col_val, col_new)
                if raw_dirty:
                    add(TARGET_RAW, field, raw_val, raw_new)

        # --- jsonb-only list fields -----------------------------------------
        for field in HTML_STRIP_LIST_FIELDS:
            val = raw.get(field)
            if isinstance(val, list):
                newlist, changed = clean_list_html(val)
                if changed:
                    add(TARGET_RAW, field, val, newlist)

        for field in PLAIN_LIST_FIELDS:
            val = raw.get(field)
            if isinstance(val, list):
                newlist, changed = clean_list_plain(val)
                if changed:
                    add(TARGET_RAW, field, val, newlist)

        for field in JSON_LIST_FIELDS:
            val = raw.get(field)
            if isinstance(val, list):
                newlist, changed = clean_json_list(val)
                if changed:
                    add(TARGET_RAW, field, val, newlist)

        if changes:
            for ch in changes:
                flat_rows.append({
                    "id": str(row.get("id")),
                    "identifier": identifier,
                    "target": ch["target"],
                    "field": ch["field"],
                    "before": serialize(ch["before"]),
                    "after": serialize(ch["after"]),
                })
                by_target[ch["target"]] += 1
                by_field[ch["field"]] += 1
            record_rows.append({
                "id": str(row.get("id")),
                "identifier": identifier,
                "changes": json.dumps(changes, ensure_ascii=False),
            })

    scanned = min(n_scanned, args.limit) if args.limit else n_scanned
    print(f"scanned={scanned} changed_records={len(record_rows)} "
          f"field_changes={len(flat_rows)}")
    print("by target:", dict(by_target.most_common()))
    print("by field :", dict(by_field.most_common()))

    with open(dry_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "identifier", "changes"])
        w.writeheader()
        w.writerows(record_rows)
    print("written", dry_path)

    with open(flat_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "identifier", "target", "field",
                                          "before", "after"])
        w.writeheader()
        w.writerows(flat_rows)
    print("written", flat_path)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
