#!/usr/bin/env python3
"""Fill empty ``records.url`` with source-catalogue URLs found by the source check.

    python3 scripts/url/apply_source_urls.py            # dry run (read-only)
    python3 scripts/url/apply_source_urls.py --apply    # write, one transaction

Inputs: output/url/source_url_check.csv (pass 1) + source_url_check_pass2.csv (pass 2).
Included (high confidence): pass 1 ``200+title`` (catalogue API returned the record with
its title) and ejpsoil ``200`` (GitHub source page loads); pass 2 ``europa_id``,
``openaire_*`` (record found by id). Excluded: ``europa_title`` (title-only match).
Fill-only (``url IS NULL``), before-image CSV, rowcount asserted, rollback on mismatch.
"""
import argparse, csv, re, sys
from pathlib import Path
from psycopg2.extras import execute_values
sys.path.insert(0, str(Path(__file__).parent))
from build_url_column import connect, OUT_DIR, URL_FORMAT_RE

def collect():
    rows = {}
    for r in csv.DictReader(open(OUT_DIR / "source_url_check.csv", newline="")):
        ok = r["check"] == "200+title" or (r["source"] == "ejpsoil" and r["check"] == "200")
        if ok and r["candidate_url"]:
            rows[r["id"]] = (r["candidate_url"], "p1_" + r["source"])
    for r in csv.DictReader(open(OUT_DIR / "source_url_check_pass2.csv", newline="")):
        if r["url"] and (r["method"] == "europa_id" or r["method"].startswith("openaire_")):
            rows.setdefault(r["id"], (r["url"], r["method"]))
    return rows

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    rows = {k: v for k, v in collect().items() if URL_FORMAT_RE.match(v[0])}
    print("candidates:", len(rows))
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT id::text, url FROM validate.records WHERE id::text = ANY(%s)", (list(rows),))
    cur_url = dict(cur.fetchall())
    todo = [(k, v[0], v[1]) for k, v in rows.items() if k in cur_url and not (cur_url[k] or "").strip()]
    print("in DB:", len(cur_url), "| still empty (will write):", len(todo))
    from collections import Counter
    print(Counter(t[2] for t in todo))
    if not a.apply:
        print("dry run - nothing written"); return
    bk = OUT_DIR / "source_urls_before_image.csv"
    if bk.exists(): sys.exit(f"refusing to overwrite {bk}")
    with open(bk, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["id", "url_before", "url_new", "method"])
        for k, u, m in todo: w.writerow([k, cur_url[k], u, m])
    try:
        cur.execute("CREATE TEMP TABLE _su (id uuid, url text, m text) ON COMMIT DROP")
        execute_values(cur, "INSERT INTO _su VALUES %s", todo)
        cur.execute("SELECT count(*) FROM validate.records"); n0 = cur.fetchone()[0]
        cur.execute("""UPDATE validate.records r SET url = s.url FROM _su s
                       WHERE r.id = s.id AND (r.url IS NULL OR btrim(r.url) = '')""")
        if cur.rowcount != len(todo): raise RuntimeError(f"updated {cur.rowcount} != {len(todo)}")
        cur.execute("SELECT count(*) FROM validate.records"); assert cur.fetchone()[0] == n0
        conn.commit(); print("committed", len(todo))
    except Exception as e:
        conn.rollback(); sys.exit(f"ROLLED BACK: {e}")

if __name__ == "__main__": main()
