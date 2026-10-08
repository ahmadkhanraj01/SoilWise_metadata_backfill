#!/usr/bin/env python3
"""
Write values recovered from the Solr export back into the records table,
for any column, with the same title-agreement guard as check_missing.py.

    python3 apply_field.py --field abstract                    # dry run
    python3 apply_field.py --field abstract --apply            # writes
    python3 apply_field.py --field abstract --include-unverified --apply

Only rows whose value is NULL, empty, a known placeholder, or shorter than
--min-len are touched, and the WHERE clause re-checks that at write time.

By default a candidate is written only when the Solr doc's title agrees with
the DB title above --min-agreement. Rows with no DB title to compare against
are held back unless --include-unverified is passed.
"""

import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

FIELD_CANDIDATES = {
    "abstract": ("abstract", "description", "dc_description", "summary", "notes"),
    "title": ("title", "titles", "dc_title", "name", "label"),
}

DEFAULT_PLACEHOLDERS = {
    "abstract": ["xx", "n/a", "na", "none", "null", "-", "--", ".", "no abstract",
                 "not available", "no description", "description"],
    "title": ["untitled", "no title", "n/a"],
}

DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                "http://dx.doi.org/", "doi:", "doi.org/")


# ---------------------------------------------------------------- identifiers

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


# ---------------------------------------------------------------- text

def clean_text(val):
    if isinstance(val, list):
        val = next((v for v in val if v and str(v).strip()), None)
    if val is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(val))   # strip tags first, then unescape,
    s = html.unescape(s)                     # so &lt;0.5%&gt; survives
    s = s.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", s).strip()


def is_missing(val, placeholders, min_len):
    s = clean_text(val)
    if len(s) < min_len:
        return True
    return s.lower().rstrip(". ") in placeholders


TOKEN_RE = re.compile(r"[a-z0-9]+")


def title_tokens(s):
    return {t for t in TOKEN_RE.findall(clean_text(s).lower()) if len(t) > 3}


def title_agreement(a, b):
    ta, tb = title_tokens(a), title_tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return -1.0
    return len(ta & tb) / min(len(ta), len(tb))


# ---------------------------------------------------------------- solr

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


def doc_value(doc, candidates):
    for field in candidates:
        s = clean_text(doc.get(field))
        if s:
            return s
    return ""


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", default="abstract")
    ap.add_argument("--json", default=os.getenv("SOLR_JSON", "Sep8_SolrData.json"))
    ap.add_argument("--schema", default=os.getenv("DB_SCHEMA", "public"))
    ap.add_argument("--table", default=os.getenv("DB_TABLE", "records"))
    ap.add_argument("--min-len", type=int, default=20)
    ap.add_argument("--min-agreement", type=float, default=0.34)
    ap.add_argument("--max-doc-ids", type=int, default=6)
    ap.add_argument("--placeholder", action="append", default=None)
    ap.add_argument("--include-unverified", action="store_true",
                    help="also write rows whose DB title is blank, so the match "
                         "could not be checked")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--no-touch-updated-at", action="store_true")
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.field):
        sys.exit("--field must be a plain column name")
    if not os.path.exists(args.json):
        sys.exit(f"JSON file not found: {args.json}")

    placeholders = set(DEFAULT_PLACEHOLDERS.get(args.field, []))
    placeholders.update(p.lower().strip() for p in (args.placeholder or []))
    candidates = FIELD_CANDIDATES.get(args.field, (args.field,))

    docs, index = load_solr(args.json, args.max_doc_ids)
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

    f = args.field
    ph_list = ",".join("%s" for _ in placeholders) or "NULL"
    where_missing = (f"({f} IS NULL OR length(btrim({f})) < %s "
                     f"OR lower(btrim({f})) IN ({ph_list}))")
    select_sql = (f"SELECT id, identifier, title, {f} AS value "
                  f"FROM {args.schema}.{args.table} "
                  f"WHERE {where_missing} ORDER BY id")
    update_sql = (f"UPDATE {args.schema}.{args.table} "
                  f"SET {f} = %s, updated_at = now() "
                  f"WHERE id = %s AND {where_missing}")
    if args.no_touch_updated_at:
        update_sql = update_sql.replace(", updated_at = now()", "")

    ph_sorted = sorted(placeholders)

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(select_sql, [args.min_len] + ph_sorted)
            rows = cur.fetchall()
        rows = [r for r in rows if is_missing(r["value"], placeholders, args.min_len)]
        print(f"{len(rows)} rows with missing {f}\n")

        payload, unverified, rejected, nothing = [], [], [], 0

        for row in rows:
            seen, scored = set(), []
            for key in id_variants(row["identifier"]):
                for d in index.get(key, []):
                    if id(d) in seen:
                        continue
                    seen.add(id(d))
                    val = doc_value(d, candidates)
                    if not val or is_missing(val, placeholders, args.min_len):
                        continue
                    agree = title_agreement(
                        row["title"], doc_value(d, FIELD_CANDIDATES["title"]))
                    scored.append((agree, len(val), val, d))
            if not scored:
                nothing += 1
                continue
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            agree, _, value, best = scored[0]
            solr_title = doc_value(best, FIELD_CANDIDATES["title"])

            if agree < 0.0:
                unverified.append((value, row["id"], row["identifier"]))
            elif agree < args.min_agreement:
                rejected.append((row["identifier"], round(agree, 2),
                                 clean_text(row["title"]), solr_title))
            else:
                payload.append((value, row["id"]))

        if args.include_unverified:
            payload.extend((v, i) for v, i, _ in unverified)

        for value, _id in payload[: args.show]:
            print(f"  {value[:100]}")
        if len(payload) > args.show:
            print(f"  ... and {len(payload) - args.show} more")

        note = (" (included)" if args.include_unverified
                else " (held back, use --include-unverified)")
        print(f"\n{len(payload)} rows to write")
        print(f"{len(unverified)} unverified{note}")
        print(f"{len(rejected)} rejected on title disagreement")
        print(f"{nothing} with no usable value in Solr")

        for ident, agree, db_t, solr_t in rejected[: args.show]:
            print(f"\n  rejected {ident} [{agree}]")
            print(f"    db   : {db_t[:80]}")
            print(f"    solr : {solr_t[:80]}")
        if len(rejected) > args.show:
            print(f"\n  ... and {len(rejected) - args.show} more rejected")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            return
        if not payload:
            print("\nNothing to do.")
            return

        params = [(v, i, args.min_len, *ph_sorted) for v, i in payload]
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, update_sql, params, page_size=200)
        conn.commit()
        print(f"\nCommitted. {len(payload)} statements sent.")

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {args.schema}.{args.table} WHERE {where_missing}",
                [args.min_len] + ph_sorted)
            print(f"Rows still missing {f}: {cur.fetchone()[0]}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()