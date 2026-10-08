#!/usr/bin/env python3
"""Scan free-text metadata fields for messy/malformed text. READ-ONLY.

Flags (regex) records whose fields contain emoji, pipes, backslashes, control
characters, mojibake, HTML/XML tags, literal URL-encoding or irregular spacing.
All patterns come from ``cleanup_rules.py`` so the scan can never disagree with
what ``dry_run.py`` is able to fix.

Usage:
    python3 find_messy.py                      # writes messy_records.csv
    python3 find_messy.py --out other.csv

Output columns: id, identifier, title (first 200 chars), issues
(``{"abstract": ["html_tag", "multi_space"], ...}`` as JSON).

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
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_rules import scan_text  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Fields the scan looks at. Note: title/abstract fall back to the top-level
# column when raw_metadata has no such key.
SCAN_FIELDS = [
    "title", "abstract", "persons", "organizations", "contact_persons",
    "contact_organizations", "subjects", "matched_subjects", "license",
    "view_authors", "view_contacts", "soilmission", "project_acronyms",
    "projects", "sources",
]


def flatten_value(v):
    """Any jsonb value -> list of strings (dicts/lists recursed, others str())."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        out = []
        for item in v:
            out.extend(flatten_value(item))
        return out
    if isinstance(v, dict):
        out = []
        for item in v.values():
            out.extend(flatten_value(item))
        return out
    return [str(v)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(PROJECT_ROOT / "output" / "scan" / "messy_records.csv"),
                    help="output CSV path (default: output/scan/messy_records.csv)")
    ap.add_argument("--env", default=str(PROJECT_ROOT / "crosscheck" / ".env"))
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    args = ap.parse_args()

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        sys.exit(f"refusing to overwrite {out_path}; pass --force if you mean it.")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    load_dotenv(args.env)
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )
    cur = conn.cursor(name="messy_scan")  # server-side cursor: streams, low memory
    cur.itersize = 2000
    cur.execute("select id, identifier, title, abstract, raw_metadata from records")

    results = []
    count = 0
    for id_, identifier, title, abstract, raw in cur:
        count += 1
        raw = raw or {}
        rec_issues = {}  # field -> sorted list of issues
        for field in SCAN_FIELDS:
            val = raw.get(field)
            if val is None:
                if field == "title":
                    val = title
                elif field == "abstract":
                    val = abstract
                # all other fields have no top-level column; raw.get() already ran
            field_issues = set()
            for t in flatten_value(val):
                field_issues.update(scan_text(t))
            if field_issues:
                rec_issues[field] = sorted(field_issues)
        if rec_issues:
            results.append({
                "id": str(id_),
                "identifier": identifier,
                "title": (title or "")[:200],
                "issues": json.dumps(rec_issues, ensure_ascii=False),
            })

    print(f"Scanned {count} records, {len(results)} flagged")

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "identifier", "title", "issues"])
        w.writeheader()
        w.writerows(results)
    print("written to", out_path)

    # summary counts per field/issue
    c: Counter = Counter()
    for r in results:
        for field, iss_list in json.loads(r["issues"]).items():
            for iss in iss_list:
                c[(field, iss)] += 1
    print("\nfield / issue / records:")
    for (field, iss), n in sorted(c.items(), key=lambda x: -x[1]):
        print(f"  {field:26s} {iss:14s} {n}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
