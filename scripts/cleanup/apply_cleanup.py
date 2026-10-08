#!/usr/bin/env python3
"""Apply the signed-off messy-text cleanup (``*_before_after.csv``) to the DB.

Dry run by default: SELECTs only. ``--apply`` writes.

    python3 scripts/cleanup/apply_cleanup.py messy_cleanup_v4_before_after.csv            # dry run
    python3 scripts/cleanup/apply_cleanup.py messy_cleanup_v4_before_after.csv --apply    # write

No-data-loss safeguards
-----------------------
1. Preflight: ``verify_diff.py`` must pass on the CSV (skip: ``--skip-verify``).
2. Optimistic guard: every UPDATE only fires if the live value still equals the
   CSV ``before`` value, so anything edited since the dry run is never clobbered.
   Any mismatch aborts the whole run (``--skip-stale`` skips those rows instead).
3. Before-image: the full ``title, abstract, raw_metadata`` of every affected
   record is written to ``--backup`` (JSONL) *before* the first UPDATE.
4. One transaction. Each UPDATE must hit exactly 1 row.
5. In-transaction audit before COMMIT: every affected record is re-read and must
   equal its before-image with *only* the intended fields replaced -- every other
   raw_metadata key and column byte-for-byte identical. Any difference -> ROLLBACK.
6. jsonb keys are never created (``create_missing = false``).

``updated_at`` is left alone unless ``--touch-updated-at`` is passed.
Restore from the backup: see ``--restore``.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COLUMN_FIELDS = ("title", "abstract")
TEXT_FIELDS = {"title", "abstract"}          # raw_metadata values stored as plain strings

csv.field_size_limit(10**9)


def connect(env: str):
    load_dotenv(env)
    return psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )


def parse_value(field: str, cell: str):
    """CSV cell -> the python value stored in jsonb (str for text, parsed JSON for lists)."""
    return cell if field in TEXT_FIELDS else json.loads(cell)


def load_changes(path: str) -> dict:
    """-> {id: [ {target, field, before, after}, ... ]}"""
    by_id: dict = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["target"] not in ("both", "column", "raw_metadata"):
                sys.exit(f"invalid target {r['target']!r} for {r['id']}")
            if r["target"] != "raw_metadata" and r["field"] not in COLUMN_FIELDS:
                sys.exit(f"target {r['target']} not allowed for field {r['field']}")
            by_id[r["id"]].append({
                "target": r["target"], "field": r["field"],
                "before": parse_value(r["field"], r["before"]),
                "after": parse_value(r["field"], r["after"]),
            })
    return by_id


def fetch_current(conn, ids: list) -> dict:
    out = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for i in range(0, len(ids), 500):
            cur.execute("select id::text as id, title, abstract, raw_metadata "
                        "from records where id = any(%s::uuid[])", (ids[i:i + 500],))
            for row in cur:
                out[row["id"]] = row
    return out


def expected_state(row: dict, changes: list):
    """before-image + intended changes -> (title, abstract, raw_metadata) or error list."""
    title, abstract = row["title"], row["abstract"]
    raw = copy.deepcopy(row["raw_metadata"]) if isinstance(row["raw_metadata"], dict) else None
    problems = []
    for ch in changes:
        f, tgt = ch["field"], ch["target"]
        if tgt in ("column", "both"):
            cur_val = title if f == "title" else abstract
            if cur_val != ch["before"]:
                problems.append(f"column {f} differs from CSV before")
            elif f == "title":
                title = ch["after"]
            else:
                abstract = ch["after"]
        if tgt in ("raw_metadata", "both"):
            if raw is None or f not in raw:
                problems.append(f"raw_metadata has no key {f!r}")
            elif raw[f] != ch["before"]:
                problems.append(f"raw_metadata.{f} differs from CSV before")
            else:
                raw[f] = ch["after"]
    return title, abstract, raw, problems


def build_update(changes: list, touch: bool):
    """One UPDATE per record; WHERE re-checks every before value."""
    sets, set_params, where, where_params = [], [], [], []
    raw_expr, raw_params = "raw_metadata", []
    for ch in changes:
        f, tgt = ch["field"], ch["target"]
        if tgt in ("column", "both"):
            sets.append(f"{f} = %s")
            set_params.append(ch["after"])
            where.append(f"{f} = %s")
            where_params.append(ch["before"])
        if tgt in ("raw_metadata", "both"):
            raw_expr = f"jsonb_set({raw_expr}, %s::text[], %s::jsonb, false)"
            raw_params += ["{" + f + "}", json.dumps(ch["after"], ensure_ascii=False)]
            where.append("raw_metadata -> %s = %s::jsonb")
            where_params += [f, json.dumps(ch["before"], ensure_ascii=False)]
    if raw_params:
        sets.append(f"raw_metadata = {raw_expr}")
    if touch:
        sets.append("updated_at = now()")
    sql = f"update records set {', '.join(sets)} where id = %s::uuid and {' and '.join(where)}"
    return sql, set_params + raw_params, where_params


def restore(conn, backup: str, apply: bool) -> None:
    n = 0
    with conn.cursor() as cur, open(backup, encoding="utf-8") as fh:
        for line in fh:
            b = json.loads(line)
            n += 1
            if apply:
                cur.execute("update records set title=%s, abstract=%s, raw_metadata=%s::jsonb "
                            "where id=%s::uuid",
                            (b["title"], b["abstract"], json.dumps(b["raw_metadata"]), b["id"]))
                assert cur.rowcount == 1, f"restore: {b['id']} not found"
    if apply:
        conn.commit()
        print(f"restored {n} records from {backup}")
    else:
        print(f"dry run: would restore {n} records from {backup}; add --apply")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="*_before_after.csv sign-off artifact")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--env", default=str(PROJECT_ROOT / "crosscheck" / ".env"))
    ap.add_argument("--backup", default=None, help="before-image JSONL (default: derived from csv name)")
    ap.add_argument("--expect-records", type=int, default=6538)
    ap.add_argument("--expect-rows", type=int, default=9090)
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--skip-stale", action="store_true",
                    help="skip records changed since the dry run instead of aborting")
    ap.add_argument("--touch-updated-at", action="store_true")
    ap.add_argument("--restore", metavar="BACKUP.jsonl", help="write before-images back")
    args = ap.parse_args()

    conn = connect(args.env)
    print(f"db: {os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
          f"  mode: {'APPLY' if args.apply else 'dry run'}")
    try:
        if args.restore:
            restore(conn, args.restore, args.apply)
            return
        if not args.csv:
            sys.exit("csv argument required")

        changes = load_changes(args.csv)
        n_rows = sum(len(v) for v in changes.values())
        print(f"csv: {len(changes)} records, {n_rows} field changes "
              f"{dict(Counter(c['target'] for v in changes.values() for c in v))}")
        if (len(changes), n_rows) != (args.expect_records, args.expect_rows):
            sys.exit(f"count mismatch: expected {args.expect_records}/{args.expect_rows} "
                     "(override with --expect-records/--expect-rows if the CSV changed)")

        if not args.skip_verify:
            rc = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "verify_diff.py"),
                                 args.csv], capture_output=True, text=True)
            print("verify_diff:", "PASS" if rc.returncode == 0 else "FAIL")
            if rc.returncode != 0:
                print(rc.stdout[-2000:])
                sys.exit("verify_diff failed; refusing to continue")

        ids = sorted(changes)
        current = fetch_current(conn, ids)
        missing = [i for i in ids if i not in current]
        stale = {}
        for i in ids:
            if i in current:
                *_, problems = expected_state(current[i], changes[i])
                if problems:
                    stale[i] = problems
        print(f"live check: {len(ids) - len(missing) - len(stale)} match CSV, "
              f"{len(stale)} stale, {len(missing)} missing")
        for i, p in list(stale.items())[:10]:
            print("  stale", i, p)
        if (stale or missing) and not args.skip_stale:
            sys.exit("live DB differs from the CSV; aborting (re-run dry_run.py, "
                     "or use --skip-stale to leave those records alone)")
        todo = [i for i in ids if i in current and i not in stale]
        print(f"{len(todo)} records / {sum(len(changes[i]) for i in todo)} field changes to write")

        if not args.apply:
            print("\nDry run only, nothing written. Re-run with --apply.")
            return

        backup = args.backup or str(
            PROJECT_ROOT / "output" / "apply" /
            (Path(args.csv).stem.replace("_before_after", "") + "_backup.jsonl"))
        Path(backup).parent.mkdir(parents=True, exist_ok=True)
        if Path(backup).exists():
            sys.exit(f"backup exists, refusing to overwrite: {backup}")
        with open(backup, "w", encoding="utf-8") as fh:
            for i in todo:
                r = current[i]
                fh.write(json.dumps({"id": i, "title": r["title"], "abstract": r["abstract"],
                                     "raw_metadata": r["raw_metadata"]}, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        print(f"before-image: {backup} ({len(todo)} records)")

        t0 = time.time()
        with conn.cursor() as cur:
            for n, i in enumerate(todo, 1):
                sql, sp, wp = build_update(changes[i], args.touch_updated_at)
                cur.execute(sql, sp + [i] + wp)
                if cur.rowcount != 1:
                    raise RuntimeError(f"{i}: UPDATE hit {cur.rowcount} rows, expected 1")
                if n % 500 == 0:
                    print(f"  {n}/{len(todo)} ({time.time() - t0:.0f}s)")

        # audit: everything not intentionally changed must be untouched
        after = fetch_current(conn, todo)
        bad = []
        for i in todo:
            t, a, raw, _ = expected_state(current[i], changes[i])
            got = after[i]
            if (got["title"], got["abstract"], got["raw_metadata"]) != (t, a, raw):
                bad.append(i)
        if bad:
            raise RuntimeError(f"audit failed for {len(bad)} records, e.g. {bad[:5]}")
        print(f"audit: {len(todo)} records identical to before-image + intended changes")

        conn.commit()
        print("COMMITTED.")
    except BaseException:
        conn.rollback()
        print("ROLLED BACK; nothing was written.", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
