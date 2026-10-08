#!/usr/bin/env python3
"""
Write titles recovered from the Solr export back into the records table.

    python3 apply_titles.py                 # dry run, shows what would change
    python3 apply_titles.py --apply         # writes, single transaction

Only rows whose title is NULL, empty, or 'Untitled' are touched, and the WHERE
clause re-checks that at write time, so a row someone else fixed meanwhile is
left alone. HTML tags and padding whitespace are stripped from the value.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

TITLE_FIELDS = ("title", "titles", "dc_title", "name", "label")
PLACEHOLDERS = {"untitled", "no title", "n/a", "na", "none", "null", "-", "xx"}
DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                "http://dx.doi.org/", "doi:", "doi.org/")


def normalize(ident):
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
    return re.sub(r"\s+", "", low.rstrip("/")) or None


def id_variants(ident):
    out = []

    def add(v):
        k = normalize(v)
        if k and k not in out:
            out.append(k)

    if ident is None:
        return out
    s = str(ident).strip()
    add(s)
    if "---" in s:
        add(s.replace("---", "://"))
    base = out[0] if out else ""
    m = re.match(r"^(10\.\d{4,9})-(.+)$", base)
    if m:
        add(f"{m.group(1)}/{m.group(2)}")
    m = re.match(r"^(?:https?)?-*doi\.org-(10\.\d{4,9})-(.+)$", base)
    if m:
        add(f"{m.group(1)}/{m.group(2)}")
    return out


def clean_title(val):
    """Flatten lists, strip HTML tags, collapse whitespace."""
    if isinstance(val, list):
        val = next((v for v in val if v and str(v).strip()), None)
    if val is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(val))
    s = s.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", s).strip()


def usable(s):
    return bool(s) and len(s) >= 3 and s.lower().rstrip(". ") not in PLACEHOLDERS


def extract_docs(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("response"), dict):
            return payload["response"].get("docs", [])
        for key in ("docs", "records", "results", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValueError("Could not locate a document list in the JSON file")


def salvage_docs(text):
    start = text.find('"docs"')
    start = text.find("[", start) if start != -1 else text.find("[")
    if start == -1:
        raise ValueError("No document array found")
    dec = json.JSONDecoder()
    docs, i, n = [], start + 1, len(text)
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
    return docs


def load_solr(path, max_doc_ids=6):
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        docs = extract_docs(json.loads(raw))
    except json.JSONDecodeError as exc:
        docs = salvage_docs(raw)
        print(f"WARNING: export truncated ({exc.msg}); salvaged {len(docs)} docs",
              file=sys.stderr)
        if not docs:
            raise
    del raw

    index = defaultdict(list)
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        values, primary = [], []
        for field in ("identifier", "identifiers", "doi", "id", "url"):
            val = doc.get(field)
            if val is None:
                continue
            vals = val if isinstance(val, list) else [val]
            values.extend(vals)
            if vals and field != "url":
                primary.append(vals[0])
        if max_doc_ids and len(values) > max_doc_ids:
            values = primary
        for v in values:
            for key in id_variants(v):
                index[key].append(doc)
    return docs, index


def doc_title(doc):
    for field in TITLE_FIELDS:
        s = clean_title(doc.get(field))
        if usable(s):
            return s
    return ""


SELECT_SQL = """
    SELECT id, identifier, title
    FROM {schema}.{table}
    WHERE title IS NULL
       OR btrim(title) = ''
       OR lower(btrim(title)) = 'untitled'
    ORDER BY id
"""

UPDATE_SQL = """
    UPDATE {schema}.{table}
    SET title = %s, updated_at = now()
    WHERE id = %s
      AND (title IS NULL OR btrim(title) = '' OR lower(btrim(title)) = 'untitled')
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.getenv("SOLR_JSON", "Sep8_SolrData.json"))
    ap.add_argument("--schema", default=os.getenv("DB_SCHEMA", "public"))
    ap.add_argument("--table", default=os.getenv("DB_TABLE", "records"))
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--no-touch-updated-at", action="store_true",
                    help="leave updated_at alone")
    args = ap.parse_args()

    if not os.path.exists(args.json):
        sys.exit(f"JSON file not found: {args.json}")

    docs, index = load_solr(args.json)
    print(f"Loaded {len(docs)} Solr docs, {len(index)} distinct identifiers")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )

    update = UPDATE_SQL.format(schema=args.schema, table=args.table)
    if args.no_touch_updated_at:
        update = update.replace(", updated_at = now()", "")

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_SQL.format(schema=args.schema, table=args.table))
            rows = cur.fetchall()
        print(f"{len(rows)} untitled rows found\n")

        payload, skipped = [], []
        for row in rows:
            seen, best = set(), ""
            for key in id_variants(row["identifier"]):
                for d in index.get(key, []):
                    if id(d) in seen:
                        continue
                    seen.add(id(d))
                    t = doc_title(d)
                    if t and len(t) > len(best):
                        best = t
            if best:
                payload.append((best, row["id"]))
            else:
                skipped.append(row["identifier"])

        for title, _id in payload[:10]:
            print(f"  {title[:95]}")
        if len(payload) > 10:
            print(f"  ... and {len(payload) - 10} more")

        print(f"\n{len(payload)} rows to update, {len(skipped)} with no usable "
              f"title in Solr")
        for ident in skipped:
            print(f"  skipped: {ident}")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            return
        if not payload:
            print("\nNothing to do.")
            return

        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, update, payload, page_size=200)
            changed = cur.rowcount
        conn.commit()
        print(f"\nCommitted. {len(payload)} statements sent.")

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {args.schema}.{args.table} "
                f"WHERE title IS NULL OR btrim(title) = '' "
                f"OR lower(btrim(title)) = 'untitled'"
            )
            print(f"Untitled rows remaining: {cur.fetchone()[0]}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
