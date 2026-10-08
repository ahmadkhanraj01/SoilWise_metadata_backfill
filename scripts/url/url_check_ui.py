#!/usr/bin/env python3
"""Run the URL HTTP check with a small local web UI. READ-ONLY (DB) + cache append.

    python3 scripts/url/url_check_ui.py                 # start checking + UI at http://127.0.0.1:8111
    python3 scripts/url/url_check_ui.py --paused        # start the UI paused
    python3 scripts/url/url_check_ui.py --port 9000 --workers 48 --per-host 12

What it does
------------
* Target = every distinct normalised URL of BOTH columns
  (`records.url` + decoded `distributions.access_url`), loaded read-only from the DB.
* Anything already in `output/url/url_check_cache.csv` counts as done -> the run is
  always resumable: stop the laptop, re-run the same command, it continues.
* Every result is appended to the cache CSV immediately (auto-save per URL) and a
  status snapshot is written to `output/url/url_check_status.json`.
* N worker threads pull from a queue (parallel, capped per start-host).
* UI (stdlib http.server, no dependencies): progress bar, class table, live results
  table, start/pause buttons.
* When everything is checked, `compare_url_accessurl.py --http` is re-run once
  (network-free at that point) so `docs/url/url_vs_accessurl_report.md` has full HTTP data.

Pause = stop pulling new URLs (results already saved stay saved).
Kill/reboot at any time = safe; re-run to continue.
"""
from __future__ import annotations

import argparse
import csv
import json
import queue
import random
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import check_urls
from compare_url_accessurl import CACHE, OUT, load_db, normalize

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = OUT / "url_check_status.json"
REPORT = PROJECT_ROOT / "docs" / "url" / "url_vs_accessurl_report.md"
CLASSES = ("ok", "blocked", "client_error", "server_error", "error")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Run:
    """Shared state between the worker pool and the web UI."""

    def __init__(self, workers: int, per_host: int, timeout: int):
        self.lock = threading.Lock()
        self.workers, self.per_host, self.timeout = workers, per_host, timeout
        self.target: set[str] = set()          # all distinct normalised URLs
        self.cache: dict[str, dict] = {}       # normalised url -> cache row
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()          # kill workers
        self.pause = threading.Event()         # stop pulling (in-flight still saved)
        self.threads: list[threading.Thread] = []
        self.start_ts = 0.0                    # process start
        self.active_elapsed = 0.0              # seconds spent running (pauses excluded)
        self._last_ts = 0.0
        self.session_done = 0
        self.recent: list[dict] = []           # this session, newest first
        self.finished = False
        self.refreshing_report = False

    # ------------------------------------------------------------ lifecycle
    def start_workers(self) -> str:
        with self.lock:
            if any(t.is_alive() for t in self.threads):
                self.pause.clear()
                return "resumed"
            todo = sorted(self.target - set(self.cache))
            random.Random(42).shuffle(todo)             # spread hosts
            for u in todo:
                self.q.put(u)
            self.stop.clear()
            self.pause.clear()
            self._last_ts = time.time()
            self.threads = []
            for i in range(self.workers):
                t = threading.Thread(target=self._worker, name=f"w{i}", daemon=True)
                t.start()
                self.threads.append(t)
            return f"started ({len(todo)} URLs to check)"

    def pause_workers(self) -> str:
        with self.lock:
            self.pause.set()
            return "paused (in-flight requests will still be saved)"

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=self.timeout + 2)

    # -------------------------------------------------------------- workers
    def _worker(self) -> None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        while not self.stop.is_set():
            if self.pause.is_set():
                time.sleep(0.1)
                continue
            try:
                url = self.q.get(timeout=0.25)
            except queue.Empty:
                continue
            while self.pause.is_set() and not self.stop.is_set():
                time.sleep(0.1)
            if self.stop.is_set():
                break
            try:
                res = check_urls.check(session, url, self.timeout, self.per_host)
            except Exception as e:                       # never lose a worker
                res = {"url": url, "http_status": "", "final_url": "",
                       "error": type(e).__name__, "method": ""}
            self._save(res)

    def _save(self, res: dict) -> None:
        cls = check_urls.classify_status(
            int(res["http_status"]) if res["http_status"] not in ("", None) else None,
            res["error"])
        with self.lock:
            self.cache[res["url"]] = res
            new_file = not CACHE.exists()
            with open(CACHE, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=check_urls.CACHE_FIELDS)
                if new_file:
                    w.writeheader()
                w.writerow(res)
                fh.flush()
            self.session_done += 1
            self.recent.insert(0, {"url": res["url"],
                                   "status": res["http_status"] or res["error"],
                                   "class": cls, "at": now_iso()})
            del self.recent[40:]
            if len(self.cache) >= len(self.target):
                self.finished = True

    # -------------------------------------------------------------- status
    def status(self) -> dict:
        with self.lock:
            now = time.time()
            running = any(t.is_alive() for t in self.threads) and not self.pause.is_set()
            if running:
                self.active_elapsed += now - self._last_ts
            self._last_ts = now
            done = len(self.target & set(self.cache))
            remaining = len(self.target) - done
            rate = self.session_done / self.active_elapsed if self.active_elapsed > 5 else 0.0
            eta_min = remaining / rate / 60 if rate and remaining else 0.0
            classes = Counter()
            for u in self.target:
                c = self.cache.get(u)
                if c:
                    classes[check_urls.classify_status(
                        int(c["http_status"]) if c["http_status"] not in ("", None) else None,
                        c["error"])] += 1
            finished = remaining == 0
            return {
                "updated_at": now_iso(),
                "state": ("finished" if finished
                          else "running" if running
                          else "paused" if self.pause.is_set() else "stopped"),
                "target": len(self.target), "done": done, "remaining": remaining,
                "percent": round(100 * done / len(self.target), 1) if self.target else 100.0,
                "session_done": self.session_done,
                "rate_per_s": round(rate, 1), "eta_min": round(eta_min, 1),
                "workers": self.workers, "per_host": self.per_host,
                "classes": {k: classes[k] for k in CLASSES},
                "cached_total": len(self.cache),
                "recent": self.recent[:25],
                "report_stale": REPORT.exists() and REPORT.stat().st_mtime < CACHE.stat().st_mtime,
                "resume_cmd": "python3 scripts/url/url_check_ui.py",
            }

    def write_snapshot(self) -> None:
        tmp = SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.status(), indent=1), encoding="utf-8")
        tmp.replace(SNAPSHOT)


# --------------------------------------------------------------------- web UI
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>SoilWise URL check</title><style>
body{background:#141414;color:#ddd;font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:18px;color:#fff} h1 b{color:#e01b1b}
.bar{height:22px;background:#2a2a2a;border:1px solid #444;border-radius:4px;overflow:hidden;margin:10px 0 6px}
.bar i{display:block;height:100%;background:#e01b1b;transition:width .4s}
.grid{display:flex;gap:24px;flex-wrap:wrap;margin-top:14px}
table{border-collapse:collapse;min-width:340px}
th,td{border:1px solid #333;padding:4px 10px;text-align:left;font-size:13px}
th{background:#1e1e1e;color:#aaa} td.num{text-align:right}
button{background:#e01b1b;color:#fff;border:0;padding:7px 18px;font-size:14px;cursor:pointer;margin-right:8px}
button.alt{background:#3a3a3a} button:disabled{opacity:.4;cursor:default}
#state{color:#e01b1b;font-weight:600}
#url{word-break:break-all;max-width:760px}
.note{color:#888;font-size:12px;margin-top:14px}
.ok{color:#7ad37a} .bad{color:#ff7a7a} .mut{color:#999}
</style></head><body>
<h1><b>SoilWise</b> — URL HTTP check</h1>
<div>state: <span id="state">…</span> &nbsp;|&nbsp; <span id="counts">…</span></div>
<div class="bar"><i id="fill" style="width:0%"></i></div>
<div><span id="pct">0%</span> &nbsp; <span id="eta"></span> &nbsp; <span id="rate"></span></div>
<div style="margin-top:12px">
<button id="bStart">Start / Resume</button>
<button id="bPause" class="alt">Pause</button>
<span class="note">auto-saved per URL — you can close this page or shut the laptop; re-run
<code>python3 scripts/url/url_check_ui.py</code> to continue.</span></div>
<div class="grid">
<table><tr><th colspan="2">HTTP status breakdown (all target URLs)</th></tr>
<tr><td class="ok">ok (2xx)</td><td class="num" id="c_ok">–</td></tr>
<tr><td>blocked (401/403/429)</td><td class="num" id="c_blocked">–</td></tr>
<tr><td>client_error (4xx)</td><td class="num" id="c_client_error">–</td></tr>
<tr><td>server_error (5xx)</td><td class="num" id="c_server_error">–</td></tr>
<tr><td class="bad">error (no answer)</td><td class="num" id="c_error">–</td></tr></table>
<table><tr><th>recently checked</th><th>status</th><th>class</th><th>time (UTC)</th></tr>
<tbody id="rows"><tr><td colspan="4" class="mut">waiting…</td></tr></tbody></table>
</div>
<div class="note" id="foot"></div>
<script>
const $=id=>document.getElementById(id);
async function post(p){await fetch(p,{method:'POST'});tick();}
$('bStart').onclick=()=>post('/api/start');
$('bPause').onclick=()=>post('/api/pause');
function cls(c){return c=='ok'?'ok':(c=='error'||c=='client_error'||c=='server_error')?'bad':'mut';}
async function tick(){
  try{
    const s=await (await fetch('/api/status')).json();
    $('state').textContent=s.state;
    $('counts').textContent=s.done+' / '+s.target+' URLs checked';
    $('fill').style.width=s.percent+'%';
    $('pct').textContent=s.percent+'%';
    $('rate').textContent=s.rate_per_s?s.rate_per_s+' req/s':'';
    $('eta').textContent=s.state=='finished'?'done ✓':(s.eta_min?'eta ~'+s.eta_min+' min':'');
    for(const k in s.classes)$('c_'+k).textContent=s.classes[k].toLocaleString();
    $('rows').innerHTML=s.recent.length?s.recent.map(r=>
      `<tr><td id="url">${r.url.slice(0,90)}</td><td>${r.status}</td>`+
      `<td class="${cls(r.class)}">${r.class}</td><td class="mut">${r.at.slice(11)}</td></tr>`).join('')
      :'<tr><td colspan="4" class="mut">nothing checked in this session yet</td></tr>';
    $('foot').textContent=(s.report_stale?'report outdated — will refresh when finished. ':'')+
      'cache: '+s.cached_total.toLocaleString()+' rows in url_check_cache.csv · snapshot: output/url/url_check_status.json';
  }catch(e){$('state').textContent='ui disconnected — checker keeps running';}
}
setInterval(tick,1000);tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    run: Run = None  # set by main

    def log_message(self, *a):            # quiet
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(json.dumps(self.run.status()).encode(), "application/json")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        if self.path == "/api/start":
            msg = self.run.start_workers()
        elif self.path == "/api/pause":
            msg = self.run.pause_workers()
        else:
            self._send(b"not found", "text/plain", 404)
            return
        self._send(json.dumps({"ok": True, "msg": msg}).encode(), "application/json")


def refresh_report_when_stale(run: Run) -> None:
    """After the last URL, re-run the compare script (no network left to do)."""
    while True:
        time.sleep(5)
        st = run.status()
        if st["state"] == "finished" and st["report_stale"] and not run.refreshing_report:
            run.refreshing_report = True
            log = PROJECT_ROOT / "output" / "logs" / "url_compare_refresh.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"\n=== {now_iso()} refresh ===\n")
                subprocess.run([sys.executable,
                                str(PROJECT_ROOT / "scripts" / "compare_url_accessurl.py"),
                                "--http"], stdout=fh, stderr=subprocess.STDOUT)
            run.refreshing_report = False
        if st["state"] == "finished":
            run.write_snapshot()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8111)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--per-host", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--paused", action="store_true", help="open the UI without checking yet")
    args = ap.parse_args()

    print("loading URLs from the database (read-only) …", flush=True)
    records, dists = load_db()
    target = set()
    for (_ident, u) in records.values():
        if n := normalize(u):
            target.add(n)
    for (_rid, au) in dists:
        if n := normalize(au):
            target.add(n)
    print(f"{len(target)} distinct target URLs", flush=True)

    run = Run(args.workers, args.per_host, args.timeout)
    run.target = target
    if CACHE.exists():
        with open(CACHE, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                u = (row.get("url") or "").strip()
                if u:
                    # raw key first (already-normalised rows must stay findable),
                    # then the normalised alias
                    run.cache.setdefault(u, row)
                if n := normalize(u):
                    run.cache.setdefault(n, row)
    already = len(target & set(run.cache))
    print(f"cache: {len(run.cache)} rows, {already} already cover the target, "
          f"{len(target) - already} to check", flush=True)

    Handler.run = run
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"UI: http://{args.host}:{args.port}/", flush=True)

    threading.Thread(target=refresh_report_when_stale, args=(run,), daemon=True).start()

    if not args.paused:
        print(run.start_workers(), flush=True)

    stop_ev = threading.Event()

    def on_sig(_s, _f):
        stop_ev.set()

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    try:
        while not stop_ev.is_set():
            time.sleep(1)
            run.write_snapshot()               # status snapshot every second
    finally:
        run.shutdown()
        run.write_snapshot()
        server.shutdown()
        st = run.status()
        print(f"\nsaved: {st['done']}/{st['target']} checked · state '{st['state']}' · "
              f"re-run the same command to continue", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
