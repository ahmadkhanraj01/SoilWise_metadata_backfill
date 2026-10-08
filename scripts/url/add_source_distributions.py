#!/usr/bin/env python3
"""Add the source-catalogue URLs (output/url/source_urls_before_image.csv) as rows in validate.distributions.

    python3 scripts/url/add_source_distributions.py [--apply]
One row per record, format like the existing rows (access_url percent-encoded, name = source label,
format = WWW:LINK-1.0-http--link). Skips a record that already has that access_url. INSERT only;
inserted ids saved to output/url/source_distributions_inserted_ids.csv (rollback = DELETE those ids).
"""
import argparse, csv, sys, uuid
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from psycopg2.extras import execute_values
sys.path.insert(0, str(Path(__file__).parent))
from build_url_column import connect, OUT_DIR
LABEL = {"p1_isric": "ISRIC data catalogue record", "p1_eea": "EEA SDI catalogue record",
         "p1_islandr": "islandr GeoNetwork catalogue record", "p1_inspire": "Source catalogue record",
         "p1_ejpsoil": "EJP SOIL datahub source record", "europa_id": "data.europa.eu dataset page",
         "openaire_doi": "OpenAIRE record (DOI)", "openaire_instance": "OpenAIRE record (repository)",
         "openaire_explore": "OpenAIRE explore page"}
FMT = "WWW:LINK-1.0-http--link"
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
meth = {r["id"]: r["method"] for r in csv.DictReader(open(OUT_DIR / "source_urls_before_image.csv", newline=""))}
conn = connect(); cur = conn.cursor()
cur.execute("SELECT id::text, url FROM validate.records WHERE id::text = ANY(%s)", (list(meth),))
cur_url = dict(cur.fetchall())
cur.execute("SELECT record_id::text, access_url FROM validate.distributions WHERE record_id::text = ANY(%s)", (list(meth),))
have = {(r, u) for r, u in cur.fetchall()}
rows = []
for i, m in meth.items():
    u = cur_url.get(i)
    if not u: continue
    enc = quote(u, safe="")
    if (i, enc) in have or (i, u) in have: continue
    rows.append((str(uuid.uuid4()), LABEL[m], FMT, enc, i))
print("records:", len(meth), "| to insert:", len(rows), "| already present:", len(meth) - len(rows))
print(Counter(r[1] for r in rows)); print(rows[0] if rows else "")
if not a.apply: sys.exit("dry run - nothing written")
out = OUT_DIR / "source_distributions_inserted_ids.csv"
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
