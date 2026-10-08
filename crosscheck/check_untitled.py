#!/usr/bin/env python3
"""
Cross-check records whose title is 'Untitled' (or blank) against a Solr JSON
export, matching on identifier, and report the titles Solr actually has.

Usage:
    python check_untitled.py
    python check_untitled.py --json Sep8_SolrData.json --limit 500
    python check_untitled.py --emit-sql fixes.sql
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------- normalizing

DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
    "doi.org/",
)


def normalize(ident):
    """Collapse identifier variants so DOI/URL/case differences still match."""
    if ident is None:
        return None
    s = str(ident).strip()
    if not s:
        return None
    low = s.lower()
    for p in DOI_PREFIXES:
        if low.startswith(p):
            low = low[len(p):]
            break
    low = low.rstrip("/")
    low = re.sub(r"\s+", "", low)
    return low or None


def is_blank_title(t):
    return t is None or not str(t).strip() or str(t).strip().lower() == "untitled"


# ---------------------------------------------------------------- solr loading

def extract_docs(payload):
    """Accept the common Solr export shapes and return a flat list of docs."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("response"), dict):
            return payload["response"].get("docs", [])
        for key in ("docs", "records", "results", "items", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    raise ValueError("Could not locate a document list in the JSON file")


def salvage_docs(text):
    """
    Parse a truncated JSON export object-by-object, keeping every complete
    document that appears before the break point.
    """
    start = text.find('"docs"')
    start = text.find("[", start) if start != -1 else text.find("[")
    if start == -1:
        raise ValueError("No document array found in file")

    dec = json.JSONDecoder()
    docs = []
    i = start + 1
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] != "{":
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        docs.append(obj)
    return docs, i


def load_solr(path):
    """Return {normalized_identifier: [doc, ...]}."""
    with open(path, "r", encoding="utf-8") as fh:
        first = fh.read(1)
        fh.seek(0)
        if first in ("{", "["):
            raw = fh.read()
            try:
                docs = extract_docs(json.loads(raw))
            except json.JSONDecodeError as exc:
                docs, stopped = salvage_docs(raw)
                pct = 100.0 * stopped / len(raw)
                print(
                    f"WARNING: {os.path.basename(path)} is truncated "
                    f"({exc.msg} at char {exc.pos}).\n"
                    f"         Salvaged {len(docs)} complete docs covering "
                    f"{pct:.1f}% of the file. Results are partial -- "
                    f"re-export to be sure.\n",
                    file=sys.stderr,
                )
                if not docs:
                    raise
            del raw
        else:  # JSON Lines fallback
            docs = []
            for lineno, line in enumerate(fh, 1):
                line = line.strip().rstrip(",")
                if not line or line in ("[", "]"):
                    continue
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"WARNING: skipped malformed line {lineno}", file=sys.stderr)

    index = defaultdict(list)
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for field in ("identifier", "identifiers", "doi", "id", "url", "alternateIdentifier"):
            raw = doc.get(field)
            if raw is None:
                continue
            values = raw if isinstance(raw, list) else [raw]
            for v in values:
                key = normalize(v)
                if key:
                    index[key].append(doc)
    return docs, index


def doc_title(doc):
    for field in ("title", "titles", "dc_title", "name", "label"):
        val = doc.get(field)
        if isinstance(val, list):
            val = next((v for v in val if v and str(v).strip()), None)
        if val and str(val).strip():
            return str(val).strip()
    return None


# ---------------------------------------------------------------- db

def fetch_untitled(conn, schema, table, limit):
    sql = f"""
        SELECT id, identifier, title, resource_type, harvest_date
        FROM {schema}.{table}
        WHERE title IS NULL
           OR btrim(title) = ''
           OR lower(btrim(title)) = 'untitled'
        ORDER BY harvest_date NULLS LAST, id
    """
    if limit:
        sql += f"\nLIMIT {int(limit)}"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def sql_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.getenv("SOLR_JSON", "Sep8_SolrData.json"))
    ap.add_argument("--schema", default=os.getenv("DB_SCHEMA", "public"))
    ap.add_argument("--table", default=os.getenv("DB_TABLE", "records"))
    ap.add_argument("--limit", type=int, default=0, help="0 = all rows")
    ap.add_argument("--csv", default="untitled_crosscheck.csv")
    ap.add_argument("--emit-sql", default=None, help="write UPDATE statements here")
    args = ap.parse_args()

    if not os.path.exists(args.json):
        sys.exit(f"JSON file not found: {args.json}")

    docs, index = load_solr(args.json)
    print(f"Loaded {len(docs)} Solr docs, {len(index)} distinct identifiers\n")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )

    try:
        rows = fetch_untitled(conn, args.schema, args.table, args.limit)
    finally:
        conn.close()

    print(f"Found {len(rows)} untitled/blank rows in {args.schema}.{args.table}\n")

    recovered, still_blank, missing = [], [], []

    for row in rows:
        key = normalize(row["identifier"])
        matches = index.get(key, []) if key else []
        title = next((t for t in (doc_title(d) for d in matches) if t), None)

        entry = {
            "id": row["id"],
            "identifier": row["identifier"],
            "db_title": row["title"],
            "solr_title": title or "",
            "resource_type": row["resource_type"],
            "solr_matches": len(matches),
            "status": "recovered" if title else ("solr_blank" if matches else "not_in_solr"),
        }
        if title:
            recovered.append(entry)
        elif matches:
            still_blank.append(entry)
        else:
            missing.append(entry)

    # -------- console report
    width = max((len(r["identifier"] or "") for r in recovered), default=0)
    print("=" * 100)
    print(f"TITLES RECOVERED FROM SOLR ({len(recovered)})")
    print("=" * 100)
    for r in recovered:
        print(f"{(r['identifier'] or ''):<{width}}  ->  {r['solr_title']}")

    print(f"\nIn Solr but title also empty there: {len(still_blank)}")
    for r in still_blank[:20]:
        print(f"  {r['identifier']}")
    if len(still_blank) > 20:
        print(f"  ... and {len(still_blank) - 20} more")

    print(f"\nIdentifier not present in Solr export: {len(missing)}")
    for r in missing[:20]:
        print(f"  {r['identifier']}")
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more")

    print(f"\nSummary: {len(recovered)} recoverable / "
          f"{len(still_blank)} blank in Solr / {len(missing)} absent "
          f"(of {len(rows)})")

    # -------- csv
    all_rows = recovered + still_blank + missing
    if all_rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nWrote {args.csv}")

    # -------- optional SQL (review before running; nothing is written to the DB here)
    if args.emit_sql and recovered:
        with open(args.emit_sql, "w", encoding="utf-8") as fh:
            fh.write("BEGIN;\n")
            for r in recovered:
                fh.write(
                    f"UPDATE {args.schema}.{args.table} "
                    f"SET title = {sql_quote(r['solr_title'])} "
                    f"WHERE id = {sql_quote(r['id'])} "
                    f"AND (title IS NULL OR btrim(title) = '' "
                    f"OR lower(btrim(title)) = 'untitled');\n"
                )
            fh.write("-- COMMIT;\n")
        print(f"Wrote {args.emit_sql} ({len(recovered)} statements, COMMIT commented out)")


if __name__ == "__main__":
    main()