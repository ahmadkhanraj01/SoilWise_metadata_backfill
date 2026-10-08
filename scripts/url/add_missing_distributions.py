#!/usr/bin/env python3
"""Add a validate.distributions row for every records.url that has none yet.

    python3 scripts/url/add_missing_distributions.py [--apply]
name/format: 'landing page' / 'text/html' (the harmonizer's convention); URLs containing
'downloadPublic' (EC participant-portal documents) -> 'download' / 'WWW:DOWNLOAD-1.0-http--download'.
access_url percent-encoded like existing rows. INSERT only; skips a record that already has the URL
(exact, encoded, or case/trailing-slash variant). Inserted ids -> output/url/missing_distributions_inserted_ids.csv.
"""
import argparse, collections, csv, sys, uuid
from pathlib import Path
from urllib.parse import quote, unquote
from psycopg2.extras import execute_values
sys.path.insert(0, str(Path(__file__).parent))
from build_url_column import connect, OUT_DIR
norm = lambda u: unquote(u).rstrip("/").lower()
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
conn = connect(); cur = conn.cursor()
cur.execute("SELECT id::text, url FROM validate.records WHERE url IS NOT NULL AND btrim(url) <> ''")
recs = cur.fetchall()
cur.execute("SELECT record_id::text, access_url FROM validate.distributions")
have = collections.defaultdict(set)
for r, u in cur.fetchall(): have[r].update({norm(u or ""), u or ""})
rows = []
for i, u in recs:
    if norm(u) in have[i] or u in have[i] or quote(u, safe="") in have[i]: continue
    dl = "downloadPublic" in u
    rows.append((str(uuid.uuid4()), "download" if dl else "landing page",
                 "WWW:DOWNLOAD-1.0-http--download" if dl else "text/html", quote(u, safe=""), i))
print("records with url:", len(recs), "| to insert:", len(rows))
print(collections.Counter((r[1], r[2]) for r in rows)); print(unquote(rows[0][3]) if rows else "")
if not a.apply: sys.exit("dry run - nothing written")
out = OUT_DIR / "missing_distributions_inserted_ids.csv"
if out.exists(): sys.exit(f"refusing to overwrite {out}")
try:
    cur.execute("SELECT count(*) FROM validate.distributions"); n0 = cur.fetchone()[0]
    execute_values(cur, "INSERT INTO validate.distributions (id,name,format,access_url,record_id,created_at,updated_at) VALUES %s",
                   rows, template="(%s,%s,%s,%s,%s,now(),now())")
    cur.execute("SELECT count(*) FROM validate.distributions"); n1 = cur.fetchone()[0]
    if n1 - n0 != len(rows): raise RuntimeError(f"inserted {n1-n0} != {len(rows)}")
    with open(out, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["distribution_id", "record_id"]); w.writerows((r[0], r[4]) for r in rows)
    conn.commit(); print("committed", len(rows))
except Exception as e:
    conn.rollback(); sys.exit(f"ROLLED BACK: {e}")
