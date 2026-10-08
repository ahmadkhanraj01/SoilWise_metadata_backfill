#!/usr/bin/env python3
"""Find URLs for records the identifier/raw_metadata rules cannot resolve (tiers D + E).

READ-ONLY: reads ``records`` from the DB, calls external services, writes a CSV.
Nothing is written to the database and the main preview/rules are not changed.

    python3 scripts/url/find_urls_external.py [--limit N] [--only D|E]

Both tiers are evidence-based: a URL is accepted only when it is verified.

D  records from OpenAIRE (``sources`` contains ``openaire``, no URL yet):
   search the OpenAIRE Graph API by title. Accept a hit only when the normalised
   title is identical AND the publication year matches (or the record has no
   date). URL = DOI of the hit if it has one, else its first https instance URL.
   Several matches with different DOIs => rejected as ambiguous.

E  records with a ``links`` list but no declared landing page: candidate links are
   limited to page-type formats (no downloads / OGC services / images / archives),
   then each candidate is fetched and accepted only if it answers 2xx **and the
   page text contains the record's title**. First verified candidate wins.

Output: output/url/url_candidates_DE.csv
  record_id, identifier, tier, candidate_url, verified, evidence, http_status
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from build_url_column import connect
from url_rules import URL_FORMAT_RE, classify, doi_to_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJECT_ROOT / "output" / "url" / "url_candidates_DE.csv"
UA = "Mozilla/5.0 (compatible; soilwise-url-finder/1.0; +metadata QA)"
GRAPH = "https://api.openaire.eu/graph/v1/researchProducts"

# link formats / extensions that are never a human landing page
BAD_FORMAT = re.compile(r"(?i)download|ogc|wms|wfs|wcs|wmts|esri|ftp|atom|folderpath|image/|"
                        r"geotiff|shp|zip|pdf|canonical|map:preview|csw|sos|rest")
BAD_EXT = re.compile(r"(?i)\.(zip|gz|tgz|tif|tiff|shp|jpg|jpeg|png|gif|pdf|csv|xls|xlsx|xml|json|"
                     r"kml|gml|7z|rar|nc|asc)(\?.*)?$")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).casefold()
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", s)).strip()


def year_of(x) -> str:
    m = re.match(r"(\d{4})", str(x or ""))
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# Tier D: OpenAIRE title search                                                #
# --------------------------------------------------------------------------- #
def tier_d(rec: dict) -> dict:
    out = {"tier": "D", "candidate_url": "", "verified": "no", "evidence": "", "http_status": ""}
    title = rec["title"] or ""
    if len(norm(title)) < 15:
        out["evidence"] = "title too short to match safely"
        return out
    try:
        r = requests.get(GRAPH, params={"mainTitle": title[:250], "pageSize": 10},
                         headers={"User-Agent": UA}, timeout=40)
        results = r.json().get("results") or []
    except Exception as e:                                    # noqa: BLE001
        out["evidence"] = f"api error {type(e).__name__}"
        return out
    want_t, want_y = norm(title), year_of(rec["date"])
    hits = [x for x in results if norm(x.get("mainTitle")) == want_t
            and (not want_y or year_of(x.get("publicationDate")) == want_y)]
    if not hits:
        out["evidence"] = f"no exact title+year match among {len(results)} results"
        return out
    dois = {(p["value"].lower()) for x in hits for p in (x.get("pids") or [])
            if p.get("scheme") == "doi"}
    if len(dois) == 1:
        out.update(candidate_url=doi_to_url(dois.pop()), evidence="openaire title+year match, DOI")
    elif len(dois) > 1:
        out["evidence"] = f"ambiguous: {len(dois)} different DOIs for the same title+year"
        return out
    else:
        urls = {u for x in hits for i in (x.get("instances") or []) for u in (i.get("urls") or [])
                if u.startswith("https://") and not BAD_EXT.search(u)
                and "downloadPublic" not in u}
        if len(urls) == 1:
            out.update(candidate_url=urls.pop(), evidence="openaire title+year match, instance url")
        else:
            out["evidence"] = f"match but {len(urls)} candidate instance urls, no DOI"
            return out
    # the title match is the evidence; HTTP only confirms the URL is reachable
    try:
        r = requests.get(out["candidate_url"], headers={"User-Agent": UA}, timeout=20, stream=True)
        r.close()
        out["http_status"] = r.status_code
        out["verified"] = ("yes" if r.ok else "blocked" if r.status_code in (401, 403, 429) else "no")
    except Exception as e:                                    # noqa: BLE001
        out["evidence"] += f"; http error {type(e).__name__}"
    return out


# --------------------------------------------------------------------------- #
# Tier E: verified page links                                                  #
# --------------------------------------------------------------------------- #
def page_links(raw: dict) -> list[str]:
    out = []
    for l in raw.get("links") or []:
        try:
            d = json.loads(l) if isinstance(l, str) else l
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        u, f = unquote((d.get("url") or "").strip()), d.get("format") or ""
        if URL_FORMAT_RE.match(u) and not BAD_FORMAT.search(f) and not BAD_EXT.search(u) \
                and not BAD_EXT.search(urlsplit(u).path):
            if u not in out:
                out.append(u)
    return out


def tier_e(rec: dict) -> dict:
    out = {"tier": "E", "candidate_url": "", "verified": "no", "evidence": "", "http_status": ""}
    cands = page_links(rec["raw"])
    if not cands:
        out["evidence"] = "no page-type link among links"
        return out
    want = norm(rec["title"])
    if len(want) < 12:
        out["evidence"] = "title too short to verify against page"
        return out
    notes = []
    for u in cands[:5]:
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=20, stream=True)
            ctype = r.headers.get("Content-Type", "")
            body = r.raw.read(1_500_000, decode_content=True).decode(
                r.encoding or "utf-8", errors="ignore") if r.ok else ""
            r.close()
        except Exception as e:                                # noqa: BLE001
            notes.append(f"{type(e).__name__}")
            continue
        if not r.ok:
            notes.append(str(r.status_code))
            continue
        if "html" not in ctype.lower():
            notes.append(f"non-html {ctype[:20]}")
            continue
        if want in norm(body):
            out.update(candidate_url=r.url, verified="yes", http_status=r.status_code,
                       evidence="page 2xx and contains the record title")
            return out
        notes.append("title not on page")
    out["evidence"] = f"{len(cands)} candidates, none verified ({', '.join(notes)[:80]})"
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="random subset per tier (trial)")
    ap.add_argument("--only", choices=["D", "E"])
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, identifier, title, publication_date, raw_metadata FROM records")
    d_recs, e_recs = [], []
    for _id, ident, title, pub, raw in cur:
        raw = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
        if classify(ident, raw).url:
            continue
        rec = {"id": _id, "identifier": ident, "title": title, "date": pub or raw.get("date"),
               "raw": raw}
        srcs = raw.get("sources") or []
        if "openaire" in srcs:
            d_recs.append(rec)
        elif raw.get("links"):
            e_recs.append(rec)
    conn.close()

    rng = random.Random(42)
    work = []
    if args.only != "E":
        sel = rng.sample(d_recs, min(args.limit, len(d_recs))) if args.limit else d_recs
        work += [(tier_d, r) for r in sel]
    if args.only != "D":
        sel = rng.sample(e_recs, min(args.limit, len(e_recs))) if args.limit else e_recs
        work += [(tier_e, r) for r in sel]
    print(f"D candidates: {len(d_recs)}  E candidates: {len(e_recs)}  to run: {len(work)}", flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = ["record_id", "identifier", "tier", "candidate_url", "verified", "evidence", "http_status"]
    done = 0
    with open(OUT, "w", newline="", encoding="utf-8") as fh, \
            ThreadPoolExecutor(args.workers) as ex:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        futs = {ex.submit(fn, rec): rec for fn, rec in work}
        for f in as_completed(futs):
            rec, res = futs[f], f.result()
            w.writerow({"record_id": rec["id"], "identifier": rec["identifier"], **res})
            fh.flush()
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(work)}", flush=True)
    print(f"written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
