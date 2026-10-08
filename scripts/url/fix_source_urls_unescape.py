#!/usr/bin/env python3
"""Repair URLs written by apply_source_urls.py that still contain HTML entities (&amp; ...).

    python3 scripts/url/fix_source_urls_unescape.py [--apply]
Only rows listed in output/url/source_urls_before_image.csv whose CURRENT url still equals
the value written then are touched; url column only; before-image; rowcount asserted.
"""
import argparse, csv, html, sys
from pathlib import Path
from psycopg2.extras import execute_values
sys.path.insert(0, str(Path(__file__).parent))
from build_url_column import connect, OUT_DIR, URL_FORMAT_RE
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
written = {r["id"]: r["url_new"] for r in csv.DictReader(open(OUT_DIR / "source_urls_before_image.csv", newline=""))}
conn = connect(); cur = conn.cursor()
cur.execute("SELECT id::text, url FROM validate.records WHERE id::text = ANY(%s)", (list(written),))
fix = []
for i, cur_url in cur.fetchall():
    new = html.unescape(cur_url or "")
    if cur_url == written[i] and new != cur_url and URL_FORMAT_RE.match(new): fix.append((i, cur_url, new))
print("to fix:", len(fix)); print(fix[0] if fix else "")
if not a.apply: sys.exit("dry run")
bk = OUT_DIR / "source_urls_unescape_before_image.csv"
if bk.exists(): sys.exit(f"refusing to overwrite {bk}")
with open(bk, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["id", "url_before", "url_new"]); w.writerows(fix)
try:
    cur.execute("CREATE TEMP TABLE _fx (id uuid, old text, new text) ON COMMIT DROP")
    execute_values(cur, "INSERT INTO _fx VALUES %s", fix)
    cur.execute("UPDATE validate.records r SET url = f.new FROM _fx f WHERE r.id = f.id AND r.url = f.old")
    if cur.rowcount != len(fix): raise RuntimeError(f"updated {cur.rowcount} != {len(fix)}")
    conn.commit(); print("committed", len(fix))
except Exception as e:
    conn.rollback(); sys.exit(f"ROLLED BACK: {e}")
