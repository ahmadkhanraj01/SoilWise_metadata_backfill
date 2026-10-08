#!/usr/bin/env python3
"""
Cross-check records with a missing/placeholder field value against a Solr JSON
export, matching on identifier, and report what Solr actually has.

Works on any column; defaults tuned for `abstract`.

    python3 check_missing.py --field abstract
    python3 check_missing.py --field title  --placeholder Untitled --min-len 1
    python3 check_missing.py --field abstract --show 40 --emit-sql abstracts.sql

Placeholder junk (xx, n/a, ...) and values shorter than --min-len are treated
as missing. Nothing is written to the database.
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

# Solr field names to try, per DB column
FIELD_CANDIDATES = {
    "abstract": ("abstract", "description", "dc_description", "summary", "notes"),
    "title": ("title", "titles", "dc_title", "name", "label"),
}

DEFAULT_PLACEHOLDERS = {
    "abstract": ["xx", "n/a", "na", "none", "null", "-", "--", ".", "no abstract",
                 "not available", "no description", "description"],
    "title": ["untitled", "no title", "n/a"],
}

DOI_PREFIXES = (
    "https://doi.org/", "http://doi.org/",
    "https://dx.doi.org/", "http://dx.doi.org/",
    "doi:", "doi.org/",
)


# ---------------------------------------------------------------- normalizing

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
    """
    All plausible normalized forms of one identifier, so slugified DOIs
    (https---doi.org-10.20387-bonares-40kc-2661) match their canonical form.
    """
    out = []

    def add(v):
        k = normalize(v)
        if k and k not in out:
            out.append(k)

    if ident is None:
        return out
    s = str(ident).strip()
    add(s)

    # separators replaced by dashes during slugification
    if "---" in s:
        add(s.replace("---", "://"))

    base = out[0] if out else ""
    # a bare DOI whose slash became a dash: 10.20387-bonares-40kc-2661
    m = re.match(r"^(10\.\d{4,9})-(.+)$", base)
    if m:
        add(f"{m.group(1)}/{m.group(2)}")
    # doi.org host left over after the prefix strip
    m = re.match(r"^(?:https?)?-*doi\.org-(10\.\d{4,9})-(.+)$", base)
    if m:
        add(f"{m.group(1)}/{m.group(2)}")
    return out


TOKEN_RE = re.compile(r"[a-z0-9]+")


def title_tokens(s):
    return {t for t in TOKEN_RE.findall(clean_text(s).lower()) if len(t) > 3}


def title_agreement(a, b):
    """Overlap of the shorter token set. -1 when either side is unusable."""
    ta, tb = title_tokens(a), title_tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return -1.0
    return len(ta & tb) / min(len(ta), len(tb))


def clean_text(val):
    """Flatten list values, strip HTML tags and whitespace."""
    if isinstance(val, list):
        val = next((v for v in val if v and str(v).strip()), None)
    if val is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(val))
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def is_missing(val, placeholders, min_len):
    s = clean_text(val)
    if len(s) < min_len:
        return True
    return s.lower().rstrip(". ") in placeholders


# ---------------------------------------------------------------- solr loading

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
    """Parse a truncated export object-by-object, keeping complete docs."""
    start = text.find('"docs"')
    start = text.find("[", start) if start != -1 else text.find("[")
    if start == -1:
        raise ValueError("No document array found in file")
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
    return docs, i


def load_solr(path):
    with open(path, "r", encoding="utf-8") as fh:
        first = fh.read(1)
        fh.seek(0)
        if first in ("{", "["):
            raw = fh.read()
            try:
                docs = extract_docs(json.loads(raw))
            except json.JSONDecodeError as exc:
                docs, stopped = salvage_docs(raw)
                print(f"WARNING: {os.path.basename(path)} is truncated "
                      f"({exc.msg} at char {exc.pos}). Salvaged {len(docs)} docs "
                      f"covering {100.0 * stopped / len(raw):.1f}% of the file.\n",
                      file=sys.stderr)
                if not docs:
                    raise
            del raw
        else:
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
            for v in (raw if isinstance(raw, list) else [raw]):
                for key in id_variants(v):
                    index[key].append(doc)
    return docs, index


def doc_value(doc, candidates):
    for field in candidates:
        s = clean_text(doc.get(field))
        if s:
            return s
    return ""


# ---------------------------------------------------------------- db

def fetch_missing(conn, schema, table, field, placeholders, min_len, limit):
    """Filter broadly in SQL, then apply the precise rules in Python."""
    ph_list = ",".join("%s" for _ in placeholders) or "NULL"
    sql = f"""
        SELECT id, identifier, title, {field} AS value, resource_type, harvest_date
        FROM {schema}.{table}
        WHERE {field} IS NULL
           OR length(btrim({field})) < %s
           OR lower(btrim({field})) IN ({ph_list})
        ORDER BY harvest_date NULLS LAST, id
    """
    params = [min_len] + list(placeholders)
    if limit:
        sql += "\nLIMIT %s"
        params.append(limit)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def sql_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def preview(s, width=110):
    s = clean_text(s)
    return s if len(s) <= width else s[: width - 1] + "\u2026"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", default="abstract")
    ap.add_argument("--json", default=os.getenv("SOLR_JSON", "Sep8_SolrData.json"))
    ap.add_argument("--schema", default=os.getenv("DB_SCHEMA", "public"))
    ap.add_argument("--table", default=os.getenv("DB_TABLE", "records"))
    ap.add_argument("--min-len", type=int, default=20,
                    help="values shorter than this count as missing (default 20)")
    ap.add_argument("--placeholder", action="append", default=None,
                    help="extra junk value to treat as missing; repeatable")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-agreement", type=float, default=0.34,
                    help="min title-token overlap with the Solr doc to trust a "
                         "match (0 disables the check, default 0.34)")
    ap.add_argument("--show", type=int, default=25,
                    help="how many recovered rows to print (0 = all)")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--emit-sql", default=None)
    args = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.field):
        sys.exit("--field must be a plain column name")
    if not os.path.exists(args.json):
        sys.exit(f"JSON file not found: {args.json}")

    placeholders = set(DEFAULT_PLACEHOLDERS.get(args.field, []))
    placeholders.update(p.lower().strip() for p in (args.placeholder or []))
    candidates = FIELD_CANDIDATES.get(args.field, (args.field,))
    out_csv = args.csv or f"{args.field}_crosscheck.csv"

    docs, index = load_solr(args.json)
    print(f"Loaded {len(docs)} Solr docs, {len(index)} distinct identifiers")
    print(f"Solr fields tried for '{args.field}': {', '.join(candidates)}\n")

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
        rows = fetch_missing(conn, args.schema, args.table, args.field,
                             sorted(placeholders), args.min_len, args.limit)
    finally:
        conn.close()

    rows = [r for r in rows if is_missing(r["value"], placeholders, args.min_len)]
    print(f"{len(rows)} rows with missing/placeholder {args.field} "
          f"in {args.schema}.{args.table}\n")

    recovered, solr_blank, missing, no_gain = [], [], [], []
    mismatched, unverified = [], []

    for row in rows:
        seen, matches = set(), []
        for key in id_variants(row["identifier"]):
            for d in index.get(key, []):
                if id(d) not in seen:
                    seen.add(id(d))
                    matches.append(d)

        # Score every candidate: prefer the one whose title agrees with ours.
        scored = []
        for d in matches:
            val = doc_value(d, candidates)
            if not val:
                continue
            agree = title_agreement(row["title"], doc_value(d, FIELD_CANDIDATES["title"]))
            scored.append((agree, len(val), val, d))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

        value, agree, matched_title = "", -1.0, ""
        if scored:
            agree, _, value, best = scored[0]
            matched_title = doc_value(best, FIELD_CANDIDATES["title"])

        if value and agree >= 0.0 and agree < args.min_agreement:
            status, bucket = "title_mismatch", mismatched
            value = ""  # do not propose a value we cannot trust
        elif value and is_missing(value, placeholders, args.min_len):
            status, bucket = "solr_also_placeholder", no_gain
        elif value and agree < 0.0:
            status, bucket = "recovered_unverified", unverified
        elif value:
            status, bucket = "recovered", recovered
        elif matches:
            status, bucket = "solr_blank", solr_blank
        else:
            status, bucket = "not_in_solr", missing

        bucket.append({
            "id": row["id"],
            "identifier": row["identifier"],
            "title": clean_text(row["title"]),
            f"db_{args.field}": clean_text(row["value"]),
            f"solr_{args.field}": value,
            "solr_len": len(value),
            "resource_type": row["resource_type"],
            "solr_matches": len(matches),
            "solr_title": matched_title,
            "title_agreement": round(agree, 2) if agree >= 0 else "",
            "status": status,
        })

    # -------- console report
    print("=" * 100)
    print(f"{args.field.upper()} RECOVERED FROM SOLR ({len(recovered)})")
    print("=" * 100)
    shown = recovered if args.show == 0 else recovered[: args.show]
    for r in shown:
        print(f"\n{r['identifier']}  [{r['solr_len']} chars]")
        print(f"  title : {preview(r['title'])}")
        print(f"  {args.field:<6}: {preview(r[f'solr_{args.field}'])}")
    if len(recovered) > len(shown):
        print(f"\n... and {len(recovered) - len(shown)} more (see {out_csv}; "
              f"--show 0 prints all)")

    if mismatched:
        print("\n" + "=" * 100)
        print(f"REJECTED - review these {len(mismatched)} by hand")
        print("=" * 100)
        for r in mismatched[: (len(mismatched) if args.show == 0 else args.show)]:
            print(f"\n{r['identifier']}  [agreement {r['title_agreement']}]")
            print(f"  db   : {preview(r['title'])}")
            print(f"  solr : {preview(r['solr_title'])}")

    for label, bucket in (("REJECTED - title disagrees with Solr doc", mismatched),
                          ("Recovered but unverifiable (no title to compare)", unverified),
                          ("Present in Solr but also placeholder/short", no_gain),
                          ("In Solr but field empty there", solr_blank),
                          ("Identifier not present in Solr export", missing)):
        print(f"\n{label}: {len(bucket)}")
        for r in bucket[:10]:
            print(f"  {r['identifier']}")
        if len(bucket) > 10:
            print(f"  ... and {len(bucket) - 10} more")

    print(f"\nSummary: {len(recovered)} recoverable / {len(unverified)} unverified "
          f"/ {len(mismatched)} rejected / {len(no_gain)} placeholder in Solr "
          f"/ {len(solr_blank)} blank in Solr / {len(missing)} absent (of {len(rows)})")

    # -------- csv
    all_rows = recovered + unverified + mismatched + no_gain + solr_blank + missing
    if all_rows:
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nWrote {out_csv}")

    # -------- optional SQL, review before running
    if args.emit_sql and recovered:
        ph_sql = ",".join(sql_quote(p) for p in sorted(placeholders)) or "''"
        with open(args.emit_sql, "w", encoding="utf-8") as fh:
            fh.write("BEGIN;\n")
            for r in recovered:
                fh.write(
                    f"UPDATE {args.schema}.{args.table} "
                    f"SET {args.field} = {sql_quote(r[f'solr_{args.field}'])} "
                    f"WHERE id = {sql_quote(r['id'])} "
                    f"AND ({args.field} IS NULL "
                    f"OR length(btrim({args.field})) < {args.min_len} "
                    f"OR lower(btrim({args.field})) IN ({ph_sql}));\n"
                )
            fh.write("-- COMMIT;\n")
        print(f"Wrote {args.emit_sql} ({len(recovered)} statements, COMMIT commented out)")


if __name__ == "__main__":
    main()