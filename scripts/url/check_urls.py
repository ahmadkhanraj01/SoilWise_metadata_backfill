#!/usr/bin/env python3
"""HTTP-check every derived/existing URL from the preview CSV. READ-ONLY, no database.

    python3 scripts/url/check_urls.py                 # check all, resumable
    python3 scripts/url/check_urls.py --limit 200     # quick trial on a random subset
    python3 scripts/url/check_urls.py --report-only   # rebuild CSV + report from the cache

Input : output/url/url_preview.csv   (written by build_url_column.py's dry run)
Output: output/url/url_status.csv     id, identifier, identifier_type, url_source, url,
                                      http_status, status_class, final_url, error
        output/url/url_check_cache.csv  one row per distinct URL (resume point)
        docs/url/url_check_report.md

Method: HEAD following redirects; when HEAD is refused (403/405/501/429/5xx or an
error) retry once with a streamed GET (body never downloaded). Each distinct URL is
checked once. Workers are capped globally and per start-host, and the order is
shuffled so no single host takes a burst.

status_class:  ok (2xx) | blocked (403/429/401: server answered, bot-blocked or
auth-gated) | client_error (other 4xx, e.g. 404) | server_error (5xx) |
error (DNS/timeout/SSL, no HTTP answer) | not_checked (no URL)
"""
from __future__ import annotations

import argparse
import csv
import random
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

import requests

csv.field_size_limit(10**9)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJECT_ROOT / "output" / "url"
UA = "Mozilla/5.0 (compatible; soilwise-url-check/1.0; +metadata QA)"
SOURCE_ALREADY = {"doi_url", "url"}          # identifier is itself a URL
SOURCE_RAW = {"landing_page", "bonares_landing"}   # taken from raw_metadata
SOURCE_EXT = {"tier_d", "tier_e"}                  # found by find_urls_external.py
CACHE_FIELDS = ["url", "http_status", "final_url", "error", "method"]
_host_locks: dict[str, threading.Semaphore] = {}
_mk = threading.Lock()


def host_sem(host: str, per_host: int) -> threading.Semaphore:
    with _mk:
        return _host_locks.setdefault(host, threading.Semaphore(per_host))


def classify_status(code, error) -> str:
    if code is None:
        return "error" if error else "not_checked"
    if 200 <= code < 300:
        return "ok"
    if code in (401, 403, 429):
        return "blocked"
    if 400 <= code < 500:
        return "client_error"
    if code >= 500:
        return "server_error"
    return "client_error"                       # unfollowed 3xx etc.


def check(session: requests.Session, url: str, timeout: int, per_host: int) -> dict:
    host = urlsplit(url).netloc.lower()
    out = {"url": url, "http_status": "", "final_url": "", "error": "", "method": ""}
    with host_sem(host, per_host):
        time.sleep(0.05)
        for method in ("HEAD", "GET"):
            try:
                r = session.request(method, url, allow_redirects=True, timeout=timeout,
                                    stream=True, headers={"User-Agent": UA})
                r.close()
                out.update(http_status=r.status_code, final_url=r.url, method=method, error="")
                if method == "HEAD" and (r.status_code in (403, 405, 501, 429)
                                         or r.status_code >= 500):
                    continue
                return out
            except requests.RequestException as e:
                out.update(method=method, error=type(e).__name__)
                if method == "GET":
                    return out
    return out


def load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["url"]: r for r in csv.DictReader(fh)}


def source_of(identifier_type: str, url: str) -> str:
    if not url:
        return "no_url_possible"
    if identifier_type in SOURCE_ALREADY:
        return "identifier_already_url"
    if identifier_type in SOURCE_RAW:
        return "from_raw_metadata"
    if identifier_type in SOURCE_EXT:
        return "from_external_lookup"
    return "derived_from_identifier"


def write_outputs(preview: list[dict], cache: dict[str, dict]) -> list[dict]:
    rows = []
    for p in preview:
        url = p["url"]
        c = cache.get(url) if url else None
        code = int(c["http_status"]) if c and c["http_status"] != "" else None
        err = c["error"] if c else ""
        rows.append({
            "id": p["record_id"], "identifier": p["identifier"],
            "identifier_type": p["identifier_type"],
            "url_source": source_of(p["identifier_type"], url), "url": url,
            "http_status": "" if code is None else code,
            "status_class": classify_status(code, err) if url and c else "not_checked",
            "final_url": c["final_url"] if c else "", "error": err,
        })
    with open(OUT / "url_status.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return rows


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "-"


def write_report(rows: list[dict], path: Path) -> str:
    total = len(rows)
    by_src = Counter(r["url_source"] for r in rows)
    with_url = [r for r in rows if r["url"]]
    L = ["# URL column: pre-implementation report", "",
         f"Generated by `scripts/url/check_urls.py` from `output/url/url_preview.csv` "
         f"({total} records). **Nothing has been written to the database.**", "",
         "## 1. Who gets a URL", "",
         "| group | meaning | records | share |", "| :-- | :-- | --: | --: |",
         f"| identifier already a URL | `doi_url`, `url`: identifier is `http(s)://…`, "
         f"URL = normalised identifier | {by_src['identifier_already_url']} | "
         f"{pct(by_src['identifier_already_url'], total)} |",
         f"| URL derived | `doi`, `arxiv`, `handle`, `urn`: built from the identifier | "
         f"{by_src['derived_from_identifier']} | {pct(by_src['derived_from_identifier'], total)} |",
         f"| no URL possible | `uuid`, `oai_recolecta`, `other`, …: stays NULL | "
         f"{by_src['no_url_possible']} | {pct(by_src['no_url_possible'], total)} |",
         f"| **total with URL** | | **{len(with_url)}** | {pct(len(with_url), total)} |", "",
         "By identifier type:", "", "| type | records | with URL |", "| :-- | --: | --: |"]
    t_all, t_url = Counter(r["identifier_type"] for r in rows), Counter(
        r["identifier_type"] for r in with_url)
    for t, c in t_all.most_common():
        L.append(f"| {t} | {c} | {t_url[t]} |")

    checked = [r for r in with_url if r["status_class"] != "not_checked"]
    L += ["", "## 2. Do the URLs work? (HTTP status)", "",
          f"{len(checked)} of {len(with_url)} URLs checked "
          f"({len({r['url'] for r in checked})} distinct).", "",
          "| class | meaning | records | share of checked |", "| :-- | :-- | --: | --: |"]
    cls = Counter(r["status_class"] for r in checked)
    meaning = {"ok": "2xx after redirects: works", "blocked": "401/403/429: server answered, "
               "bot-blocked or login-gated (likely fine in a browser)",
               "client_error": "other 4xx (404 not found, 410 gone…)", "server_error": "5xx",
               "error": "no HTTP answer (DNS, timeout, SSL, connection)"}
    for k in ("ok", "blocked", "client_error", "server_error", "error"):
        L.append(f"| {k} | {meaning[k]} | {cls[k]} | {pct(cls[k], len(checked))} |")

    L += ["", "Status-code histogram:", "", "| code | records |", "| :-- | --: |"]
    for code, c in Counter(r["http_status"] for r in checked).most_common(15):
        L.append(f"| {code or 'none'} | {c} |")

    L += ["", "Works vs not, by group:", "", "| group | ok | blocked | 4xx | 5xx | error |",
          "| :-- | --: | --: | --: | --: | --: |"]
    for g in ("identifier_already_url", "derived_from_identifier"):
        c = Counter(r["status_class"] for r in checked if r["url_source"] == g)
        L.append(f"| {g} | {c['ok']} | {c['blocked']} | {c['client_error']} | "
                 f"{c['server_error']} | {c['error']} |")

    bad = [r for r in checked if r["status_class"] in ("client_error", "server_error", "error")]
    hosts: dict[str, Counter] = defaultdict(Counter)
    for r in checked:
        h = urlsplit(r["final_url"] or r["url"]).netloc.lower()
        hosts[h][r["status_class"]] += 1
    L += ["", "Hosts with the most failing URLs (final host after redirects):", "",
          "| host | failing | total |", "| :-- | --: | --: |"]
    fail_by_host = sorted(((sum(c[k] for k in ("client_error", "server_error", "error")),
                            sum(c.values()), h) for h, c in hosts.items()), reverse=True)[:10]
    for f, t, h in fail_by_host:
        if f:
            L.append(f"| {h} | {f} | {t} |")

    L += ["", "Sample of failing URLs:", "", "| identifier | status | url |", "| :-- | :-- | :-- |"]
    for r in random.Random(7).sample(bad, min(15, len(bad))):
        L.append(f"| `{r['identifier'][:50]}` | {r['http_status'] or r['error']} | {r['url'][:80]} |")

    nourl = Counter(r["identifier_type"] for r in rows if not r["url"])
    L += ["", "## 3. Cannot get a URL (stays NULL)", "", "| type | records |", "| :-- | --: |"]
    for t, c in nourl.most_common():
        L.append(f"| {t} | {c} |")
    L += ["", "## 4. Files", "",
          "* `output/url/url_status.csv`: one row per record "
          "(`id, identifier, identifier_type, url_source, url, http_status, status_class, "
          "final_url, error`)",
          "* `output/url/url_check_cache.csv`: one row per distinct URL (resume point)", ""]
    text = "\n".join(L)
    path.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", default=str(OUT / "url_preview.csv"))
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--per-host", type=int, default=4, help="max concurrent requests per start host")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--limit", type=int, default=0, help="check only N random distinct URLs")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    with open(args.preview, newline="", encoding="utf-8") as fh:
        # tolerate CSVs re-aligned by an editor plugin (padded fields, huge pad rows)
        preview = [{k.strip(): (v or "").strip() for k, v in r.items() if k}
                   for r in csv.DictReader(fh)]
    cache_path = OUT / "url_check_cache.csv"
    cache = load_cache(cache_path)

    if not args.report_only:
        urls = sorted({p["url"] for p in preview if p["url"]})
        random.Random(args.seed).shuffle(urls)
        if args.limit:
            urls = urls[:args.limit]
        todo = [u for u in urls if u not in cache]
        print(f"{len(urls)} distinct urls, {len(cache)} cached, {len(todo)} to check", flush=True)

        new_file = not cache_path.exists()
        with open(cache_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CACHE_FIELDS)
            if new_file:
                w.writeheader()
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            t0, done = time.time(), 0
            with ThreadPoolExecutor(args.workers) as ex:
                futs = [ex.submit(check, session, u, args.timeout, args.per_host) for u in todo]
                for f in as_completed(futs):
                    res = f.result()
                    cache[res["url"]] = res
                    w.writerow(res)
                    fh.flush()
                    done += 1
                    if done % 250 == 0 or done == len(todo):
                        rate = done / (time.time() - t0)
                        eta = (len(todo) - done) / rate / 60 if rate else 0
                        print(f"  {done}/{len(todo)}  {rate:.1f}/s  eta {eta:.0f} min", flush=True)

    rows = write_outputs(preview, cache)
    text = write_report(rows, PROJECT_ROOT / "docs" / "url" / "url_check_report.md")
    print("\n" + text)
    print(f"csv:    {OUT / 'url_status.csv'}\nreport: {PROJECT_ROOT / 'docs' / 'url_check_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
