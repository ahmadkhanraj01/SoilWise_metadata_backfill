#!/usr/bin/env python3
"""Compare public.records.url with public.distributions.access_url. READ-ONLY (DB).

    python3 scripts/url/compare_url_accessurl.py                  # structural compare + report (no network)
    python3 scripts/url/compare_url_accessurl.py --http           # + HTTP-check dist URLs missing from cache
    python3 scripts/url/compare_url_accessurl.py --http --workers 48 --per-host 12

`distributions.access_url` is stored percent-encoded (https%3A%2F%2F…); it is
URL-decoded first, then both sides are normalised identically
(unquote -> trim -> lowercase scheme+host -> strip trailing / . , ;) so the
comparison is fair. Two match levels are reported: exact (decoded only) and
normalised.

Per record (U = normalised records.url, D = the set of normalised access_urls
of that record) exactly one category:
    neither          U NULL, no distribution URL        (no link at all)
    url_only         U set, no distribution URL         (in records, not in dist)
    dist_only        U NULL, >=1 distribution URL       (in dist, not in records)
    sets_equal       D == {U}
    url_in_dist_extra  U in D but D has more URLs
    url_not_in_dist  both present, U not in D

Outputs:
    output/url/url_vs_dist_per_record.csv   record_id, identifier, records_url,
                                            n_dist_urls, dist_urls, category,
                                            url_http, best_dist_http
    output/url/url_vs_dist_urls.csv         url, in_records, in_dist_rows,
                                            in_dist_records, http_status,
                                            status_class, final_url, error
    docs/url/url_vs_accessurl_report.md         the report
    output/url/url_check_cache.csv          APPENDED (same schema) when --http

HTTP classes are the same as check_urls.py: ok (2xx) | blocked (401/403/429) |
client_error | server_error | error | not_checked. `blocked` is NOT "broken".
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote

import psycopg2
import requests
from dotenv import load_dotenv

import check_urls  # reuse check(), classify_status(), CACHE_FIELDS, UA

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJECT_ROOT / "output" / "url"
REPORT = PROJECT_ROOT / "docs" / "url" / "url_vs_accessurl_report.md"
CACHE = OUT / "url_check_cache.csv"
CSV_RECORDS = OUT / "url_vs_dist_per_record.csv"
CSV_URLS = OUT / "url_vs_dist_urls.csv"

_RANK = {"ok": 5, "blocked": 4, "client_error": 2, "server_error": 2,
         "error": 1, "not_checked": 0}
_CATS = ("neither", "url_only", "dist_only", "sets_equal",
         "url_in_dist_extra", "url_not_in_dist")
_CAT_MEANING = {
    "neither": "no link at all (no records.url, no distribution)",
    "url_only": "in records.url, NOT in distributions",
    "dist_only": "in distributions, NOT in records.url",
    "sets_equal": "both sides: distributions carry exactly the same single URL",
    "url_in_dist_extra": "records.url is one of several distribution URLs",
    "url_not_in_dist": "both sides have links, but they differ (disjoint)",
}
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$", re.S)


# --------------------------------------------------------------------------- util
def decode_only(u: str | None) -> str | None:
    """Percent-decode + trim, nothing else (the 'exact decoded' level)."""
    if not u:
        return None
    s = unquote(u.strip())
    return s or None


def normalize(u: str | None) -> str | None:
    """decode + trim + lowercase scheme/host + strip trailing / . , ;"""
    s = decode_only(u)
    if not s:
        return None
    m = _SCHEME.match(s)
    if not m:
        return s
    scheme, rest = m.group(1).lower(), m.group(2)
    cut = len(rest)
    for ch in ("/", "?", "#"):
        i = rest.find(ch)
        if i != -1:
            cut = min(cut, i)
    host, tail = rest[:cut], rest[cut:]
    out = f"{scheme}://{host.lower()}{tail}"
    while out and out[-1] in "/.,;":
        out = out[:-1]
    return out or None


def classify(status: str, error: str) -> str:
    code = int(status) if status not in ("", None) else None
    return check_urls.classify_status(code, error)


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "-"


# ------------------------------------------------------------------------ load DB
def detect_schema(cur) -> str:
    """The schema holding records+distributions.

    2026-10-07: the dev DB was migrated out of `public` (now `validate`),
    so never hardcode the schema. Override with DB_SCHEMA in crosscheck/.env.
    """
    forced = os.getenv("DB_SCHEMA")
    if forced:
        cur.execute("""select count(*) from information_schema.tables
                       where table_type='BASE TABLE' and table_schema=%s
                       and table_name in ('records','distributions')""", (forced,))
        if cur.fetchone()[0] == 2:
            return forced
        print(f"warning: DB_SCHEMA={forced} has no records/distributions "
              f"(DB migrated?), auto-detecting", flush=True)
    cur.execute("""
        select table_schema from information_schema.tables
        where table_type='BASE TABLE' and table_name in ('records','distributions')
        group by table_schema having count(*) = 2
        order by array_position(array['public','validate','harvest'], table_schema NULLS LAST)
        limit 1""")
    row = cur.fetchone()
    if not row:
        raise SystemExit("no schema with both `records` and `distributions` found")
    return row[0]


def load_db():
    load_dotenv(PROJECT_ROOT / "crosscheck" / ".env")
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")))
    conn.set_session(readonly=True, autocommit=True)   # hard read-only
    cur = conn.cursor()
    schema = detect_schema(cur)
    print(f"schema: {schema} (set DB_SCHEMA in crosscheck/.env to force)", flush=True)
    cur.execute(f"select id, identifier, url from {schema}.records")
    records = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.execute(f"select record_id, access_url from {schema}.distributions")
    dists = cur.fetchall()
    cur.close()
    conn.close()
    return records, dists


# ------------------------------------------------------------------------- compare
def compare(records, dists):
    dist_by_rec: dict = defaultdict(list)
    for rid, au in dists:
        n = normalize(au)
        if n:
            dist_by_rec[rid].append(n)

    rows, cats = [], Counter()
    rec_url_n = Counter()                            # distinct-value side
    dist_url_n, dist_rows_n, dist_recs_n = Counter(), Counter(), Counter()

    for rid, (ident, url) in records.items():
        u = normalize(url)
        dset = set(dist_by_rec.get(rid, ()))
        if u:
            rec_url_n[u] += 1
        for du in dset:
            dist_url_n[du] += 1
        for raw in dist_by_rec.get(rid, ()):
            dist_rows_n[raw] += 1
        for du in dset:
            dist_recs_n[du] += 1

        if u is None and not dset:
            cat = "neither"
        elif u and not dset:
            cat = "url_only"
        elif not u and dset:
            cat = "dist_only"
        elif dset == {u}:
            cat = "sets_equal"
        elif u in dset:
            cat = "url_in_dist_extra"
        else:
            cat = "url_not_in_dist"
        cats[cat] += 1
        rows.append({
            "record_id": str(rid), "identifier": ident or "",
            "records_url": u or "", "n_dist_urls": len(dset),
            "dist_urls": "|".join(sorted(dset)), "category": cat,
            "url_http": "", "best_dist_http": "",
        })

    return rows, cats, rec_url_n, dist_url_n, dist_rows_n, dist_recs_n


def exact_sets(records, dists):
    """Distinct sets at the 'exact decoded' level (decode only, no normalisation)."""
    r = {e for (_ident, u) in records.values() if (e := decode_only(u))}
    d = {e for (_rid, au) in dists if (e := decode_only(au))}
    return r, d


# -------------------------------------------------------------------------- cache
def load_cache():
    if not CACHE.exists():
        return {}
    with open(CACHE, newline="", encoding="utf-8") as fh:
        return {r["url"]: r for r in csv.DictReader(fh)}


def fill_http(rows, cache_norm):
    """Fill url_http / best_dist_http in the per-record rows from the cache."""
    for r in rows:
        u = r["records_url"]
        c = cache_norm.get(u) if u else None
        r["url_http"] = (classify(c["http_status"], c["error"]) if c
                         else "not_checked" if u else "")
        if r["n_dist_urls"] == 0:
            r["best_dist_http"] = ""
        else:
            best = "not_checked"
            for du in r["dist_urls"].split("|"):
                c = cache_norm.get(du)
                cls = classify(c["http_status"], c["error"]) if c else "not_checked"
                if _RANK[cls] > _RANK[best]:
                    best = cls
            r["best_dist_http"] = best


def http_check(urls, cache, cache_norm, args):
    todo = [u for u in urls if u not in cache_norm]
    print(f"{len(urls)} distinct distribution URLs, {len(cache)} cached, "
          f"{len(todo)} to check", flush=True)
    if not todo:
        return cache_norm
    new_file = not CACHE.exists()
    with open(CACHE, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=check_urls.CACHE_FIELDS)
        if new_file:
            w.writeheader()
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        t0, done = time.time(), 0
        with ThreadPoolExecutor(args.workers) as ex:
            futs = [ex.submit(check_urls.check, session, u, args.timeout, args.per_host)
                    for u in todo]
            for f in as_completed(futs):
                res = f.result()
                cache[res["url"]] = res
                cache_norm[normalize(res["url"])] = res
                w.writerow(res)
                fh.flush()
                done += 1
                if done % 250 == 0 or done == len(todo):
                    rate = done / (time.time() - t0)
                    eta = (len(todo) - done) / rate / 60 if rate else 0
                    print(f"  {done}/{len(todo)}  {rate:.1f}/s  eta {eta:.0f} min", flush=True)
    return cache_norm


# ------------------------------------------------------------------------- outputs
def write_csvs(rows, dist_url_counts, dist_rec_counts, rec_counts, cache_norm):
    with open(CSV_RECORDS, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    all_urls = set(rec_counts) | set(dist_url_counts)
    with open(CSV_URLS, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["url", "in_records", "in_dist_rows", "in_dist_records",
                    "http_status", "status_class", "final_url", "error"])
        for u in sorted(all_urls):
            c = cache_norm.get(u)
            w.writerow([u, rec_counts.get(u, 0), dist_url_counts.get(u, 0),
                        dist_rec_counts.get(u, 0),
                        c["http_status"] if c else "",
                        classify(c["http_status"], c["error"]) if c else "not_checked",
                        c["final_url"] if c else "", c["error"] if c else ""])


def write_report(rows, cats, rec_counts, dist_counts, dist_rows_n,
                 exact_r, exact_d, cache_norm, http_ran: bool):
    total = len(rows)
    inter = set(rec_counts) & set(dist_counts)
    only_r = set(rec_counts) - set(dist_counts)
    only_d = set(dist_counts) - set(rec_counts)
    ex_inter = exact_r & exact_d
    ex_only_r = exact_r - exact_d
    ex_only_d = exact_d - exact_r

    L = ["# `records.url` vs `distributions.access_url` — comparison report", "",
         "Generated by `scripts/url/compare_url_accessurl.py`. **Read-only: nothing was "
         "written to the database.** `distributions.access_url` was percent-decoded "
         "before comparing.", "",
         f"## 1. Distinct URL values ({len(rec_counts)} vs {len(dist_counts)} distinct)", "",
         "Both sides decoded + normalised (scheme/host lowercased, trailing "
         "`/ . , ;` stripped):", "",
         "| set | distinct URLs | share |", "| :-- | --: | --: |",
         f"| in **both** columns | {len(inter)} | {pct(len(inter), len(inter | only_r | only_d))} |",
         f"| **only in `records.url`** | {len(only_r)} | {pct(len(only_r), len(inter | only_r | only_d))} |",
         f"| **only in `distributions.access_url`** | {len(only_d)} | {pct(len(only_d), len(inter | only_r | only_d))} |",
         f"| union | {len(inter | only_r | only_d)} | 100% |", "",
         "Exact (percent-decode only, no case/trailing-slash normalisation): "
         f"both **{len(ex_inter)}**, only records **{len(ex_only_r)}**, "
         f"only distributions **{len(ex_only_d)}**.", "",
         f"Distribution rows: {sum(dist_rows_n.values())} non-empty `access_url` "
         f"across {total} records (avg {sum(dist_rows_n.values())/max(1,total):.2f} per record).", ""]

    L += ["## 2. Per-record table (all 25,129 records)", "",
          "| category | meaning | records | share |", "| :-- | :-- | --: | --: |"]
    for c in _CATS:
        L.append(f"| `{c}` | {_CAT_MEANING[c]} | {cats[c]} | {pct(cats[c], total)} |")
    L.append(f"| **total** | | **{total}** | 100% |")

    L += ["", "Grouped the way the question was asked:", "",
          "| question | answer |", "| :-- | --: |",
          f"| records with **no link at all** | {cats['neither']} |",
          f"| records whose URL is in records **but not** in distributions (`url_only` + `url_not_in_dist`) | {cats['url_only'] + cats['url_not_in_dist']} |",
          f"| records whose URL is in distributions **but not** in records (`dist_only`) | {cats['dist_only']} |",
          f"| records present on **both** sides (any relationship) | {total - cats['neither'] - cats['url_only'] - cats['dist_only']} |", ""]

    # ---- HTTP
    checked_r = Counter(r["url_http"] for r in rows if r["records_url"])
    checked_d = Counter(r["best_dist_http"] for r in rows if r["n_dist_urls"])
    L += ["## 3. Which one works? (HTTP, from `output/url/url_check_cache.csv`)", ""]
    unchecked = len(set(dist_counts) - set(cache_norm))
    if not http_ran:
        L += [f"> Structural pass only — statuses below come from the existing cache "
              f"(**{unchecked}** distribution URLs not checked yet; re-run with `--http` "
              f"or `scripts/url/url_check_ui.py`).", ""]
    L += ["`blocked` (401/403/429) = server answered but refuses scripts — **not** "
          "counted as broken. `not_checked` = no HTTP result in the cache.", "",
          "### 3.1 `records.url` (per record)", "",
          "| class | records | share |", "| :-- | --: | --: |"]
    n_r = sum(checked_r.values())
    for k in ("ok", "blocked", "client_error", "server_error", "error", "not_checked"):
        L.append(f"| {k} | {checked_r[k]} | {pct(checked_r[k], n_r)} |")
    L += ["", "### 3.2 best `distributions.access_url` per record", "",
          "| class (best URL of the record) | records | share |", "| :-- | --: | --: |"]
    n_d = sum(checked_d.values())
    for k in ("ok", "blocked", "client_error", "server_error", "error", "not_checked"):
        L.append(f"| {k} | {checked_d[k]} | {pct(checked_d[k], n_d)} |")

    L += ["", "### 3.3 head-to-head (records that have both sides)", "",
          "| records.url \\ best distribution URL | ok | blocked | 4xx/5xx | error | not_checked |",
          "| :-- | --: | --: | --: | --: | --: |"]

    def bucket(c):
        return {"ok": 0, "blocked": 1, "client_error": 2, "server_error": 2,
                "error": 3, "not_checked": 4, "": 4}.get(c, 4)
    heads = Counter()
    for r in rows:
        if r["records_url"] and r["n_dist_urls"]:
            heads[(bucket(r["url_http"]), bucket(r["best_dist_http"]))] += 1
    labels = ["ok", "blocked", "4xx/5xx", "error", "not_checked"]
    for i, lab in enumerate(labels):
        row = [heads[(i, j)] for j in range(5)]
        L.append(f"| **{lab}** | " + " | ".join(str(x) for x in row) + " |")

    L += ["", "## 4. Files", "",
          "* `output/url/url_vs_dist_per_record.csv`: one row per record "
          "(`record_id, identifier, records_url, n_dist_urls, dist_urls, category, "
          "url_http, best_dist_http`)",
          "* `output/url/url_vs_dist_urls.csv`: one row per distinct normalised URL "
          "(`url, in_records, in_dist_rows, in_dist_records, http_status, status_class, "
          "final_url, error`)",
          "* `output/url/url_check_cache.csv`: HTTP cache (append-only, shared with "
          "`check_urls.py`)", "",
          "## 5. Caveats", "",
          "* 100% of `distributions.access_url` values are percent-encoded as stored "
          "(raw comparison would show 0 matches).",
          "* `distributions.access_url` includes downloads (.zip/.pdf), OGC services "
          "(WMS/WFS) and API endpoints — not only landing pages.",
          "* `maps.bonares.de` URLs do not answer from this network (untested, not "
          "proven broken); `blocked` means bot/login-gated, likely fine in a browser.", ""]
    text = "\n".join(L)
    REPORT.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", action="store_true",
                    help="HTTP-check distribution URLs missing from the cache")
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--per-host", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--limit", type=int, default=0, help="check only N URLs (smoke test)")
    args = ap.parse_args()

    t0 = time.time()
    records, dists = load_db()
    print(f"loaded {len(records)} records, {len(dists)} distribution rows "
          f"({time.time()-t0:.1f}s)", flush=True)

    rows, cats, rec_counts, dist_counts, dist_rows_n, dist_recs_n = compare(records, dists)
    exact_r, exact_d = exact_sets(records, dists)

    cache = load_cache()
    cache_norm = {}
    for k, v in cache.items():
        # index BOTH the raw row key and its normalised form: normalising an
        # already-normalised row is not always idempotent (double-encoded
        # %25XX values), and the target set only has the once-normalised key.
        if k and k not in cache_norm:
            cache_norm[k] = v
        nk = normalize(k)
        if nk and nk not in cache_norm:
            cache_norm[nk] = v

    http_ran = False
    if args.http:
        todo_urls = sorted(set(dist_counts))
        if args.limit:
            todo_urls = todo_urls[:args.limit]
        http_check(todo_urls, cache, cache_norm, args)
        http_ran = True

    fill_http(rows, cache_norm)
    write_csvs(rows, dist_counts, dist_recs_n, rec_counts, cache_norm)
    text = write_report(rows, cats, rec_counts, dist_counts, dist_rows_n,
                        exact_r, exact_d, cache_norm, http_ran)
    print(text)
    print(f"\ncsv:     {CSV_RECORDS}\n         {CSV_URLS}\nreport:  {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
