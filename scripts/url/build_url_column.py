#!/usr/bin/env python3
"""Add + fill ``records.url`` from ``records.identifier`` (docs/url/url_table_plan.md).

    python3 scripts/url/build_url_column.py                  # dry run (read-only)
    python3 scripts/url/build_url_column.py --head-sample 20 # + HEAD spot-check of 20 URLs
    python3 scripts/url/build_url_column.py --apply          # ALTER + fill, one transaction
    python3 scripts/url/build_url_column.py --apply --refresh  # also overwrite derived values

Dry run (default, never writes to the database):
  * recon: does ``url`` exist yet, which views depend on ``records``
  * classifies every identifier with ``url_rules.classify`` (the single rules module)
  * prints type counts, random samples per type, every distinct shape in ``other``
  * runs the verify gate and writes ``output/url/url_preview.csv``

``--candidates output/url/url_candidates_DE.csv`` (from ``find_urls_external.py``)
additionally merges the externally found URLs, **only** rows with ``verified=yes``
and **only** for records the rules left without a URL (never overwrites a
rules-derived URL). They get type ``tier_d`` (OpenAIRE title+year match) or
``tier_e`` (page link whose page contains the record title).

``--apply`` refuses to run unless the verify gate passes. It then
  1. writes a before-image CSV (``output/url/url_before_image.csv``; refuses to overwrite),
  2. in ONE transaction: adds the nullable column + NOT VALID format CHECK, stages
     the derived values in a TEMP table, fills ``url`` (fill-only: ``url IS NULL``;
     ``--refresh`` overwrites changed values), asserts
       - rows updated == expected count,
       - ``count(*)`` unchanged,
       - every other column of every row byte-identical (md5 of the row minus ``url``),
     and rolls back on any mismatch,
  3. after commit: post-checks, then ``VALIDATE CONSTRAINT``.
Only ``records.url`` is ever written. ``raw_metadata`` and all other columns are untouched.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

from url_rules import URL_FORMAT_RE, classify, doi_suffix_intact

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "output" / "url"
CONSTRAINT = "records_url_format_ck"


def connect():
    load_dotenv(PROJECT_ROOT / "crosscheck" / ".env")
    return psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )


# --------------------------------------------------------------------------- #
# Recon (read-only)                                                            #
# --------------------------------------------------------------------------- #
def recon(cur) -> bool:
    """Print column status + dependants of ``records``. Returns True if url exists."""
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'records' AND column_name = 'url'
                     AND table_schema = current_schema()""")
    has_url = cur.fetchone() is not None
    print(f"records.url column: {'ALREADY EXISTS' if has_url else 'not present (will be added)'}")

    cur.execute("""SELECT DISTINCT v.relname, v.relkind
                   FROM pg_depend d
                   JOIN pg_rewrite r ON r.oid = d.objid
                   JOIN pg_class v ON v.oid = r.ev_class
                   JOIN pg_class t ON t.oid = d.refobjid
                   WHERE t.relname = 'records' AND t.relnamespace = current_schema()::regnamespace
                     AND v.oid <> t.oid""")
    deps = cur.fetchall()
    if deps:
        kinds = {"v": "view", "m": "materialized view"}
        print("views depending on records (a new column does not change them; "
              "`SELECT *` was expanded at creation):")
        for name, kind in deps:
            print(f"  - {name} ({kinds.get(kind, kind)})")
    else:
        print("no views depend on records")
    return has_url


# --------------------------------------------------------------------------- #
# Classify + verify                                                            #
# --------------------------------------------------------------------------- #
def shape(s: str) -> str:
    """Coarse shape of an identifier, to group the 'other' bucket."""
    s = re.sub(r"[A-Za-z]+", "a", str(s).strip())
    s = re.sub(r"\d+", "9", s)
    return s[:40]


def verify(rows: list[dict]) -> tuple[list[str], list[str]]:
    """Return (blocking failures, informational notes)."""
    fails, notes = [], []
    bad = [r for r in rows if r["url"] and not URL_FORMAT_RE.match(r["url"])]
    if bad:
        fails.append(f"{len(bad)} urls fail ^https?://[^\\s]+$ (e.g. {bad[0]['url']!r})")
    bad = [r for r in rows if r["type"] in ("doi", "doi_url")
           and r["url"] and not doi_suffix_intact(r["identifier"], r["url"])]
    if bad:
        fails.append(f"{len(bad)} DOI suffixes altered (e.g. {bad[0]['identifier']!r} -> {bad[0]['url']!r})")
    bad = [r for r in rows if r["type"] in ("uuid", "oai_recolecta", "isbn", "issn", "other", "empty")
           and r["url"] is not None]
    if bad:
        fails.append(f"{len(bad)} no-resolver rows got a URL (must stay NULL)")

    bad = [r for r in rows if r["type"] in ("tier_d", "tier_e") and not r["url"]]
    if bad:
        fails.append(f"{len(bad)} tier_d/tier_e rows without a url")
    other = sum(1 for r in rows if r["type"] == "other")
    notes.append(f"'other' rows: {other} (target ~0)")
    by_url = defaultdict(list)
    for r in rows:
        if r["url"]:
            by_url[r["url"]].append(r["identifier"])
    dups = {u: ids for u, ids in by_url.items() if len(ids) > 1}
    notes.append(f"urls shared by >1 record: {len(dups)} (informational; may be duplicate records)")
    for u, ids in list(dups.items())[:5]:
        notes.append(f"    {u}  <-  {ids[:3]}")
    return fails, notes


def head_check(rows: list[dict], n: int, seed: int) -> None:
    """Opt-in, rate-limited HEAD spot-check of n random URLs."""
    pool = [r for r in rows if r["url"]]
    sample = random.Random(seed).sample(pool, min(n, len(pool)))
    print(f"\nHEAD spot-check of {len(sample)} urls (1 req/sec):")
    ok = 0
    for r in sample:
        req = urllib.request.Request(r["url"], method="HEAD",
                                     headers={"User-Agent": "soilwise-url-check/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
        except Exception as e:                      # noqa: BLE001 - report, don't fail
            code = getattr(e, "code", type(e).__name__)
        ok += isinstance(code, int) and code < 400
        print(f"  {code}  {r['url']}")
        time.sleep(1)
    print(f"  reachable: {ok}/{len(sample)} (informational only)")


def merge_candidates(rows: list[dict], path: Path) -> None:
    """Fill still-NULL rows from find_urls_external.py output (verified=yes only)."""
    csv.field_size_limit(10 ** 9)
    with open(path, newline="", encoding="utf-8") as fh:
        cand = {r["record_id"].strip(): r for r in csv.DictReader(fh)
                if r["verified"].strip() == "yes" and r["tier"].strip() in ("D", "E")
                and URL_FORMAT_RE.match(r["candidate_url"].strip())}
    merged, kept = Counter(), 0
    for r in rows:
        c = cand.get(str(r["id"]))
        if not c:
            continue
        if r["url"] or r["type"] not in ("uuid", "other", "oai_recolecta"):
            kept += 1                                  # rules already decided: never overwrite
            continue
        r["type"], r["url"] = "tier_" + c["tier"].strip().lower(), c["candidate_url"].strip()
        merged[r["type"]] += 1
    print(f"candidates merged from {path.name}: "
          + (", ".join(f"{t}={n}" for t, n in sorted(merged.items())) or "none")
          + f"  (verified rows ignored because rules already had a url: {kept})")


# --------------------------------------------------------------------------- #
# Apply                                                                        #
# --------------------------------------------------------------------------- #
ROW_FINGERPRINT = ("SELECT md5(coalesce(string_agg(md5((to_jsonb(r) - 'url')::text), '' "
                   "ORDER BY id), '')) FROM records r")


def apply(conn, rows: list[dict], has_url: bool, refresh: bool, backup: Path) -> None:
    # 1. before-image (id, url) -- column may not exist yet, then every url is empty
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        sys.exit(f"backup exists, refusing to overwrite: {backup}")
    with conn.cursor() as cur:
        cur.execute("SELECT id, url FROM records ORDER BY id" if has_url
                    else "SELECT id, NULL::text FROM records ORDER BY id")
        before = cur.fetchall()
    with open(backup, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "url"])
        w.writerows(before)
        fh.flush()
        os.fsync(fh.fileno())
    print(f"before-image: {backup} ({len(before)} rows)")
    current = {i: u for i, u in before}

    todo = [r for r in rows if r["url"] and
            (current.get(r["id"]) != r["url"] if refresh else current.get(r["id"]) is None)]
    expected = len(todo)

    # 2. one transaction
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM records")
        n_before = cur.fetchone()[0]
        cur.execute(ROW_FINGERPRINT)
        fp_before = cur.fetchone()[0]

        cur.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS url text")
        cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (CONSTRAINT,))
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE records ADD CONSTRAINT {CONSTRAINT} "
                        r"CHECK (url IS NULL OR url ~ '^https?://[^\s]+$') NOT VALID")

        cur.execute("CREATE TEMP TABLE _url_stage ON COMMIT DROP AS "
                    "SELECT id, identifier, NULL::text AS url FROM records LIMIT 0")
        execute_values(cur, "INSERT INTO _url_stage (id, identifier, url) VALUES %s",
                       [(r["id"], r["identifier"], r["url"]) for r in todo], page_size=1000)
        # identifier re-checked: if it changed mid-run the stale URL is not written
        cur.execute(f"""UPDATE records r SET url = s.url
                        FROM _url_stage s
                        WHERE r.id = s.id AND r.identifier = s.identifier
                          AND {'r.url IS DISTINCT FROM s.url' if refresh else 'r.url IS NULL'}""")
        updated = cur.rowcount

        cur.execute("SELECT count(*) FROM records")
        n_after = cur.fetchone()[0]
        cur.execute(ROW_FINGERPRINT)
        fp_after = cur.fetchone()[0]

    problems = []
    if updated != expected:
        problems.append(f"updated {updated} rows, expected {expected}")
    if n_after != n_before:
        problems.append(f"row count changed {n_before} -> {n_after}")
    if fp_after != fp_before:
        problems.append("a column other than url changed (trigger? concurrent writer?)")
    if problems:
        conn.rollback()
        sys.exit("ROLLED BACK: " + "; ".join(problems))
    conn.commit()
    print(f"committed: {updated} urls written; count(*) {n_after} unchanged; "
          "all other columns byte-identical")

    # 3. post-checks + validate
    with conn.cursor() as cur:
        cur.execute("SELECT count(url), count(*) FILTER (WHERE url IS NULL), count(*) FROM records")
        filled, nulls, total = cur.fetchone()
        assert filled + nulls == total
        print(f"post-check: url filled {filled}, NULL {nulls}, total {total}")
        types = Counter(r["type"] for r in rows if not r["url"])
        print("NULL url by type: " + ", ".join(f"{t}={c}" for t, c in types.most_common()))
        cur.execute(f"ALTER TABLE records VALIDATE CONSTRAINT {CONSTRAINT}")
    conn.commit()
    print(f"constraint {CONSTRAINT} validated")
    print(f"undo: ALTER TABLE records DROP COLUMN url;   (before-image: {backup})")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="add the column and write")
    ap.add_argument("--refresh", action="store_true",
                    help="also overwrite url values that differ from the derived one")
    ap.add_argument("--samples", type=int, default=5, help="random samples per type")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--head-sample", type=int, default=0, metavar="N",
                    help="HEAD-check N random derived urls (off by default)")
    ap.add_argument("--candidates", metavar="CSV", default=None,
                    help="merge verified=yes rows of find_urls_external.py output "
                         "(fills only records the rules left NULL)")
    ap.add_argument("--preview", default=str(OUT_DIR / "url_preview.csv"))
    ap.add_argument("--backup", default=str(OUT_DIR / "url_before_image.csv"))
    args = ap.parse_args()

    conn = connect()
    try:
        with conn.cursor() as cur:
            has_url = recon(cur)
            cur.execute("SELECT id, identifier, raw_metadata FROM records ORDER BY id")
            raw = cur.fetchall()
        conn.rollback()                                   # end the read-only snapshot

        rows = []
        for _id, ident, rawmd in raw:
            t, u = classify(ident, rawmd)
            rows.append({"id": _id, "identifier": ident, "type": t, "url": u})

        if args.candidates:
            merge_candidates(rows, Path(args.candidates))

        counts = Counter(r["type"] for r in rows)
        print(f"\n{len(rows)} records classified")
        for t, c in counts.most_common():
            filled = sum(1 for r in rows if r["type"] == t and r["url"])
            print(f"  {t:14s} {c:8d}   -> url {filled:8d}  NULL {c - filled:8d}")

        rng = random.Random(args.seed)
        for t in counts:
            pool = [r for r in rows if r["type"] == t]
            print(f"\n[{t}] samples")
            for r in rng.sample(pool, min(args.samples, len(pool))):
                print(f"  {str(r['identifier'])[:70]!r:74s} -> {r['url']}")
        if counts.get("other"):
            shapes = Counter(shape(r["identifier"]) for r in rows if r["type"] == "other")
            print(f"\ndistinct shapes in 'other' ({len(shapes)}):")
            for s, c in shapes.most_common(25):
                print(f"  {c:6d}  {s}")

        fails, notes = verify(rows)
        print("\nverify gate:")
        for n in notes:
            print(f"  info  {n}")
        for f in fails:
            print(f"  FAIL  {f}")
        if not fails:
            print("  PASS")

        Path(args.preview).parent.mkdir(parents=True, exist_ok=True)
        with open(args.preview, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["record_id", "identifier", "identifier_type", "url"])
            w.writerows([r["id"], r["identifier"], r["type"], r["url"] or ""] for r in rows)
        print(f"\npreview: {args.preview}")

        if args.head_sample:
            head_check(rows, args.head_sample, args.seed)

        if not args.apply:
            print("\nDry run. Nothing written. Re-run with --apply to add the column and fill it.")
            return 0
        if fails:
            sys.exit("verify gate failed; refusing to apply")
        apply(conn, rows, has_url, args.refresh, Path(args.backup))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
