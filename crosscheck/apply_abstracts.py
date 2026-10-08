#!/usr/bin/env python3
"""
Write the abstracts recovered by fetch_sources.py (source_abstract_crosscheck.csv)
into public.records, skipping rows on the exclusion list (Copernicus review-comment
threads and boilerplate/site-tagline false positives — see handover.md).

    python3 apply_abstracts.py                     # dry run
    python3 apply_abstracts.py --apply              # writes
    python3 apply_abstracts.py --include-unverified --apply

The WHERE clause re-checks the column is still empty/placeholder at write time,
same guard apply_field.py uses, so this is safe to re-run.
"""

import argparse
import csv
import json
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

PLACEHOLDERS = ["xx", "n/a", "na", "none", "null", "-", "--", ".", "no abstract",
                "not available", "no description", "description"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="source_abstract_crosscheck.csv")
    ap.add_argument("--exclude", default="exclusion_list.json")
    ap.add_argument("--schema", default=os.getenv("DB_SCHEMA", "public"))
    ap.add_argument("--table", default=os.getenv("DB_TABLE", "records"))
    ap.add_argument("--min-len", type=int, default=20)
    ap.add_argument("--include-unverified", action="store_true",
                     help="also write recovered_unverified rows")
    ap.add_argument("--apply", action="store_true",
                     help="actually write; without it this is a dry run")
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        sys.exit(f"CSV not found: {args.csv}")

    excluded = set()
    if os.path.exists(args.exclude):
        excluded = set(json.load(open(args.exclude)).keys())
        print(f"Loaded {len(excluded)} excluded ids from {args.exclude}")

    wanted_statuses = {"recovered"}
    if args.include_unverified:
        wanted_statuses.add("recovered_unverified")

    rows = list(csv.DictReader(open(args.csv)))
    payload, skipped_excluded, skipped_empty = [], 0, 0
    for r in rows:
        if r["status"] not in wanted_statuses:
            continue
        if r["id"] in excluded:
            skipped_excluded += 1
            continue
        val = (r.get("source_abstract") or "").strip()
        if len(val) < args.min_len:
            skipped_empty += 1
            continue
        payload.append((val, r["id"]))

    print(f"{len(payload)} rows to write "
          f"({skipped_excluded} excluded, {skipped_empty} too short/empty)")
    for value, _id in payload[: args.show]:
        print(f"  {_id}  {value[:90]}")
    if len(payload) > args.show:
        print(f"  ... and {len(payload) - args.show} more")

    if not payload:
        print("\nNothing to do.")
        return

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )

    ph_list = ",".join("%s" for _ in PLACEHOLDERS)
    where_missing = (f"(abstract IS NULL OR length(btrim(abstract)) < %s "
                      f"OR lower(btrim(abstract)) IN ({ph_list}))")
    update_sql = (f"UPDATE {args.schema}.{args.table} "
                  f"SET abstract = %s, updated_at = now() "
                  f"WHERE id = %s AND {where_missing}")

    try:
        params = [(v, i, args.min_len, *PLACEHOLDERS) for v, i in payload]
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, update_sql, params, page_size=200)
        conn.commit()
        print(f"\nCommitted. {len(payload)} statements sent.")

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {args.schema}.{args.table} WHERE {where_missing}",
                [args.min_len] + PLACEHOLDERS)
            print(f"Rows still missing abstract: {cur.fetchone()[0]}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
