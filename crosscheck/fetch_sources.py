#!/usr/bin/env python3
"""
Look up each record's upstream source by identifier and propose an abstract
from it, with the same title-agreement guard as check_missing.py.

    python3 fetch_sources.py                                  # dry run
    python3 fetch_sources.py --emit-sql source_abstracts.sql
    python3 fetch_sources.py --apply                          # writes to DB
    python3 fetch_sources.py --input ../records_x_empty_abstract.csv
    python3 fetch_sources.py --from-db --limit 200            # live DB rows
    python3 fetch_sources.py --refresh                        # ignore cache

Which resolvers run depends on what kind of identifier the row has:

    DOI   -> openalex, crossref, datacite   (+ landing with --doi-landing)
    URL   -> landing (meta / JSON-LD / YouTube shortDescription)
           -> pdf      (pdftotext, only a section headed Abstract/Summary/...)
    no DOI or URL (OAI ids, internal 50| keys, slugs)
          -> title_search, Crossref query.title

Two guards, because they are not equally trustworthy:

  * Identifier lookups (DOI/URL) are authoritative, so a candidate only has to
    clear --min-agreement (0.34, token overlap over the *shorter* title), the
    same bar check_missing.py uses.
  * title_search has no identifier behind it, so it is held to
    --title-jaccard (0.7, overlap over the *union*). The union denominator
    matters: a short generic DB title like "Spectral soil library" otherwise
    scores 1.00 against a long unrelated paper that happens to contain those
    three words. On a 60-row pilot this split cleanly -- 3 correct at >= 0.7,
    all 8 wrong below it.

Results are cached per normalized identifier in --cache, so an interrupted run
resumes instead of re-fetching. Nothing is written without --apply, and the
UPDATE re-checks that the column is still missing at write time.
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import html as html_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote

import urllib.error
import urllib.request
from urllib.parse import urlparse

from check_missing import (
    DEFAULT_PLACEHOLDERS,
    clean_text,
    id_variants,
    is_missing,
    preview,
    sql_quote,
    title_agreement,
    title_tokens,
)

CONTACT = os.getenv("BOT_CONTACT", "metadata@soilwise.eu")
UA = f"SoilWiseMetadataBot/1.0 (mailto:{CONTACT})"
HTML_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SoilWiseMetadataBot/1.0")

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
PLACEHOLDER_TEXT = set(DEFAULT_PLACEHOLDERS["abstract"])


# ------------------------------------------------------------------ identify

def doi_of(ident):
    """Canonical DOI for an identifier, or None. Handles percent-encoding,
    doi.org URLs, and dash-slugified forms."""
    if not ident:
        return None
    raw = unquote(str(ident).strip())
    for variant in id_variants(raw):
        if DOI_RE.match(variant):
            return variant
    # slugified: https---doi.org-10.20387-bonares-40kc-2661
    low = raw.lower()
    if "doi.org" in low:
        m = re.search(r"doi\.org[/-]+(10\.\d{4,9}[-/]\S+)", low)
        if m:
            cand = m.group(1).replace("-", "/", 1)
            if DOI_RE.match(cand):
                return cand
    return None


def url_of(ident):
    s = clean_text(ident)
    # identifiers scraped from HTML carry &amp; escapes
    s = html_mod.unescape(s).strip()
    return s if s.startswith(("http://", "https://")) else None


def lookup_key(ident, title):
    """Cache key: one per logical source record."""
    d = doi_of(ident)
    if d:
        return "doi:" + d
    u = url_of(ident)
    if u:
        return "url:" + u
    if clean_text(title):
        return "title:" + clean_text(title)
    return None


# ------------------------------------------------------------------ plumbing

class Polite:
    """Minimum interval between requests to the same host."""

    def __init__(self, interval):
        self.interval = interval
        self.locks = {}
        self.last = {}
        self.guard = threading.Lock()

    def wait(self, host):
        if self.interval <= 0:
            return
        with self.guard:
            lock = self.locks.setdefault(host, threading.Lock())
        with lock:
            due = self.last.get(host, 0.0) + self.interval
            pause = due - time.time()
            if pause > 0:
                time.sleep(pause)
            self.last[host] = time.time()


class FetchError(Exception):
    pass


_print_lock = threading.Lock()


def vprint(msg):
    """Thread-safe progress print, used only when --verbose is set."""
    with _print_lock:
        print(msg, flush=True)


def fetch(url, polite, timeout=20, headers=None, tries=3):
    host = urllib_host(url)
    for attempt in range(tries):
        polite.wait(host)
        req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(4_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < tries:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 60.0)
                except (TypeError, ValueError):
                    delay = 4.0 * (attempt + 1)  # 429 needs more than 1.5s
                time.sleep(max(delay, 1.5 * (attempt + 1)))
                continue
            raise FetchError(f"HTTP {exc.code}") from None
        except Exception as exc:  # timeout, DNS, TLS
            if attempt + 1 < tries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise FetchError(type(exc).__name__) from None
    raise FetchError("exhausted")


def fetch_bytes(url, polite, timeout=25, headers=None, tries=3):
    """fetch(), but returning raw bytes (PDFs cannot round-trip via str)."""
    host = urllib_host(url)
    for attempt in range(tries):
        polite.wait(host)
        req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(30_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < tries:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 60.0)
                except (TypeError, ValueError):
                    delay = 4.0 * (attempt + 1)
                time.sleep(max(delay, 1.5 * (attempt + 1)))
                continue
            raise FetchError(f"HTTP {exc.code}") from None
        except Exception as exc:
            if attempt + 1 < tries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise FetchError(type(exc).__name__) from None
    raise FetchError("exhausted")


def urllib_host(url):
    return urlparse(url).netloc.lower()


def get_json(url, polite, headers=None):
    body = fetch(url, polite, headers=headers)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(f"bad JSON: {exc}") from None


# ------------------------------------------------------------------ resolvers

def title_jaccard(a, b):
    """Jaccard overlap of title token sets. Unlike title_agreement this divides
    by the union, so a short generic DB title cannot score well against a long
    unrelated one that merely contains its words."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return -1.0
    return len(ta & tb) / len(ta | tb)


# candidates found by title search (rather than by an authoritative identifier)
# are held to the stricter Jaccard bar; identifier matches keep --min-agreement
TITLE_SEARCH_SOURCES = {"crossref_title", "openalex_title"}


def cand(source, abstract, title=""):
    a = clean_text(abstract)
    if len(a) >= 20 and a.lower().rstrip(". ") not in PLACEHOLDER_TEXT:
        return {"source": source, "abstract": a, "title": clean_text(title)}
    return None


def resolve_openalex(doi, polite, _row):
    url = f"https://api.openalex.org/works/doi:{quote(doi, safe='')}" \
          f"?mailto={quote(CONTACT)}"
    data = get_json(url, polite, headers={"User-Agent": UA})
    abstract = ""
    inv = data.get("abstract_inverted_index")
    if isinstance(inv, dict):
        pos = {}
        for word, offsets in inv.items():
            for i in offsets:
                pos[i] = word
        abstract = " ".join(pos[i] for i in sorted(pos))
    return [c for c in [cand("openalex", abstract, data.get("title") or "")] if c]


def resolve_crossref(doi, polite, _row):
    url = f"https://api.crossref.org/works/{quote(doi, safe='')}" \
          f"?mailto={quote(CONTACT)}"
    msg = get_json(url, polite, headers={"User-Agent": UA}).get("message", {})
    title = (msg.get("title") or [""])[0]
    return [c for c in [cand("crossref", msg.get("abstract", ""), title)] if c]


def resolve_datacite(doi, polite, _row):
    url = f"https://api.datacite.org/dois/{quote(doi, safe='')}"
    attrs = (get_json(url, polite, headers={"User-Agent": UA})
             .get("data", {}).get("attributes", {}) or {})
    descriptions = attrs.get("descriptions") or []
    # prefer an Abstract-typed description, then any substantial one
    descriptions.sort(key=lambda d: 0 if (d.get("descriptionType") or "") == "Abstract" else 1)
    title = (attrs.get("titles") or [{}])[0].get("title", "")
    out = []
    for desc in descriptions:
        c = cand("datacite", desc.get("description", ""), title)
        if c:
            out.append(c)
            break
    return out


META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
META_ATTR = re.compile(r"""(\w[\w:.-]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def meta_tags(fragment):
    out = {}
    for tag in META_TAG.findall(fragment):
        attrs = {}
        for m in META_ATTR.finditer(tag):
            attrs[m.group(1).lower()] = m.group(3) or m.group(4) or m.group(5) or ""
        key = attrs.get("name") or attrs.get("property") or attrs.get("itemprop")
        if key and "content" in attrs:
            out[key.lower()] = attrs["content"]
    return out


DESC_KEYS = ("dc.description", "dcterms.abstract", "citation_abstract",
             "og:description", "description", "twitter:description",
             "name.description")


def jsonld_descriptions(fragment):
    found = []
    for m in re.finditer(
            r"""<script[^>]*type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
            fragment, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if isinstance(node.get("description"), str):
                    found.append(node["description"])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return found


ABSTRACT_HEADING = re.compile(
    r"^\s{0,10}(?:abstract|summary|streszczenie|souhrn|zusammenfassung|"
    r"r[ée]sum[ée]|sinopsi[si]|sintesi| الملخص|概要)\s*[:.]?\s*$",
    re.I | re.M)
STOP_HEADINGS = re.compile(
    r"^\s{0,10}(?:key\s*words?|keywords|cl[ée]s?|words?\s+used|"
    r"s[łl]owa\s+kluczowe|inhalts[üu]eberblick|1[\s.]|introduction|"
    r"einleitung|wst[ęe]p)\b", re.I | re.M)


def _pdf_tools():
    """(pdftotext, pdfinfo) paths, or None if poppler is not installed."""
    import shutil
    pdftotext, pdfinfo = shutil.which("pdftotext"), shutil.which("pdfinfo")
    return (pdftotext, pdfinfo) if pdftotext and pdfinfo else None


def resolve_pdf(url, polite, _row):
    """Pull the abstract section out of a PDF's first pages.

    Only a section that actually announces itself as an abstract is used; the
    cover-page text a report starts with is not an abstract, so those rows
    stay unfilled rather than being filled with front matter.
    """
    if not _pdf_tools():
        raise FetchError("no_pdftotext")
    import subprocess
    import tempfile

    body = fetch_bytes(url, polite, timeout=40,
                       headers={"User-Agent": HTML_UA,
                                "Accept": "application/pdf,*/*"})
    if not body[:5].startswith(b"%PDF"):
        raise FetchError("not_pdf")

    pdftotext, pdfinfo = _pdf_tools()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(body)
        path = fh.name
    try:
        info = subprocess.run([pdfinfo, path], capture_output=True, timeout=30)
        title = ""
        for line in info.stdout.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("title:"):
                title = line.partition(":")[2].strip()
                break
        out = subprocess.run([pdftotext, "-f", "1", "-l", "4", path, "-"],
                             capture_output=True, timeout=60)
        text = out.stdout.decode("utf-8", "replace")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    text = text.replace("\u00ad", "").replace("\r", "\n")
    text = re.sub(r"-\n(\w)", r"\1", text)          # de-hyphenate line breaks
    text = re.sub(r"[ \t]+", " ", text)
    m = ABSTRACT_HEADING.search(text)
    if not m:
        return []                                    # no abstract section: do not guess
    rest = text[m.end():]
    stop = STOP_HEADINGS.search(rest)
    section = rest[: stop.start()] if stop else rest[: 3000]
    section = re.sub(r"\s*\n\s*", " ", section).strip()
    if len(section) < 100:
        return []
    c = cand("pdf", section, title)
    return [c] if c else []


def resolve_semantic_scholar(doi, polite, _row):
    """Semantic Scholar's Graph API sometimes has an abstract (scraped from
    the publisher) that OpenAlex/Crossref/DataCite lack. Not in the default
    --chain: re-verified 2026-09-25 that the unauthenticated endpoint is
    blocked at the WAF level (HTTP 429, x-api-key: blocked-by-waf, on every
    request, no Retry-After) -- not a soft rate limit backoff can work
    around. Needs a real Semantic Scholar API key to be usable at all."""
    url = (f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='')}"
           f"?fields=abstract,title")
    data = get_json(url, polite, headers={"User-Agent": UA})
    return [c for c in [cand("semantic_scholar", data.get("abstract") or "",
                             data.get("title") or "")] if c]


def resolve_landing(url, polite, _row):
    body = fetch(url, polite, timeout=25,
                 headers={"User-Agent": HTML_UA, "Accept": "text/html,*/*"})
    head = body[:400_000]
    metas = meta_tags(head)
    title = metas.get("og:title") or metas.get("twitter:title") or ""

    yt = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', body)
    if yt:
        try:
            text = json.loads('"' + yt.group(1) + '"')
        except json.JSONDecodeError:
            text = yt.group(1)
        c = cand("youtube", text, title)
        if c:
            return [c]

    best = None
    for key in DESC_KEYS:
        if key in metas:
            c = cand(f"landing:{key}", metas[key], title)
            if c and (best is None or len(c["abstract"]) > len(best["abstract"])):
                best = c
    if best:
        return [best]
    for text in jsonld_descriptions(head):
        c = cand("landing:jsonld", text, title)
        if c:
            return [c]
    return []


def resolve_title_search(title, polite, _row):
    """Fallback for rows whose identifier is an OAI id, an internal key or a
    slug with no DOI/URL: find the work by title in Crossref.

    Crossref's match is fuzzy, so every hit comes back as a candidate and the
    title_agreement guard in evaluate() decides which, if any, is trustworthy.
    """
    t = clean_text(title)
    if len(t) < 12:
        return []
    url = ("https://api.crossref.org/works?query.title="
           + quote(t[:300], safe="") + "&rows=10&select=DOI,title,abstract"
           + "&mailto=" + quote(CONTACT))
    items = get_json(url, polite, headers={"User-Agent": UA}).get("message", {})
    out = []
    for item in items.get("items", []):
        c = cand("crossref_title", item.get("abstract") or "",
                 (item.get("title") or [""])[0])
        if c:
            out.append(c)
    return out


def resolve_openalex_search(title, polite, _row):
    """Same idea against OpenAlex. Anonymous search is currently returning 503,
    so this is available via --chain but off by default."""
    t = clean_text(title)
    if len(t) < 12:
        return []
    url = ("https://api.openalex.org/works?filter=title.search:"
           + quote(t[:200], safe="") + "&per-page=5&mailto=" + quote(CONTACT))
    data = get_json(url, polite, headers={"User-Agent": UA})
    out = []
    for work in data.get("results", [])[:5]:
        inv = work.get("abstract_inverted_index")
        abstract = ""
        if isinstance(inv, dict):
            pos = {}
            for word, offsets in inv.items():
                for i in offsets:
                    pos[i] = word
            abstract = " ".join(pos[i] for i in sorted(pos))
        c = cand("openalex_title", abstract, work.get("title") or "")
        if c:
            out.append(c)
    return out


RESOLVERS = {
    "openalex": (resolve_openalex, "doi"),
    "crossref": (resolve_crossref, "doi"),
    "datacite": (resolve_datacite, "doi"),
    "landing": (resolve_landing, "url"),
    "pdf": (resolve_pdf, "url"),
    "title_search": (resolve_title_search, "title"),
    "openalex_search": (resolve_openalex_search, "title"),
    "semantic_scholar": (resolve_semantic_scholar, "doi"),
}


def resolve_key(key, chain, polite, titles, min_agreement,
                use_landing=True, doi_landing=False, verbose=False, tag=""):
    """Run the resolver chain for one cache key, stopping as soon as a
    candidate's title agrees with any of this group's DB titles.

    Returns (candidates, errors, responded) where `responded` counts resolvers
    that answered HTTP 200, even if they had no usable abstract.
    """
    kind, _, value = key.partition(":")
    candidates, errors, responded = [], [], 0
    label = f"{tag} " if tag else ""

    def announce(name, target):
        if not verbose:
            return
        if name == "landing":
            vprint(f"{label}loading page for {key}: {target[:90]}")
        elif name == "pdf":
            vprint(f"{label}downloading PDF for {key}: {target[:90]}")
        else:
            vprint(f"{label}querying {name} for {key}")

    def report(name, cands):
        if not verbose:
            return
        if cands:
            c = cands[0]
            vprint(f"{label}  {name} -> extracting abstract: "
                   f"\"{c['title'][:60]}\" ({len(c['abstract'])} chars)")
        else:
            vprint(f"{label}  {name} -> responded, no usable abstract")

    # PDFs need pdftotext; plain landing pages need HTML
    if kind == "url":
        is_pdf = value.lower().split("?")[0].endswith(".pdf")
        name = "pdf" if is_pdf else "landing"
        if name in chain:
            announce(name, value)
            try:
                cands = RESOLVERS[name][0](value, polite, None)
                candidates.extend(cands)
                responded += 1
                report(name, cands)
            except FetchError as exc:
                if verbose:
                    vprint(f"{label}  {name} -> FAILED: {exc}")
                errors.append(f"{name}:{exc}")
        return candidates, errors, responded

    # which resolvers apply to this kind of key, and what to pass each one.
    # 'landing' runs for DOI rows only with --doi-landing, because doi.org
    # redirects usually land on paywalls.
    doi_plan = [("openalex", value), ("crossref", value), ("datacite", value),
                ("semantic_scholar", value)]
    if doi_landing:
        doi_plan.append(("landing", f"https://doi.org/{value}"))
    plan = {
        "doi": doi_plan,
        "title": [("title_search", value)],
    }[kind]

    for name, target in plan:
        if name not in chain:
            continue
        announce(name, target)
        resolver = RESOLVERS[name][0]
        try:
            cands = resolver(target, polite, None)
            candidates.extend(cands)
            responded += 1
            report(name, cands)
        except FetchError as exc:
            if verbose:
                vprint(f"{label}  {name} -> FAILED: {exc}")
            errors.append(f"{name}:{exc}")
            continue
        # early stop once something is trustworthy for this row
        if kind != "title" and any(
                title_agreement(t, c["title"]) >= min_agreement
                for t in titles for c in candidates):
            if verbose:
                vprint(f"{label}  title agreement cleared, stopping chain")
            break
    return candidates, errors, responded

# ------------------------------------------------------------------ evaluation

def evaluate(row, candidates, min_len, min_agreement, title_jaccard_min=0.7):
    """Pick the best candidate for one DB row and bucket it.

    Title-search candidates have no identifier behind them, so they are held to
    --title-jaccard and the ones that fail are kept in `rejected` for the
    review CSV rather than silently dropped.
    """
    scored, rejected = [], []
    for c in candidates:
        agree = title_agreement(row["title"], c["title"])
        if c["source"] in TITLE_SEARCH_SOURCES:
            jac = title_jaccard(row["title"], c["title"])
            if jac < title_jaccard_min:
                rejected.append((jac, c))
                continue
            agree = min(agree, jac)
        scored.append((agree, len(c["abstract"]), c))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    rejected.sort(key=lambda t: t[0], reverse=True)

    if scored:
        agree, _, chosen = scored[0]
        if agree >= min_agreement:
            status = "recovered"
        elif agree < 0:
            status = "recovered_unverified"
        else:
            status = "title_mismatch"
        if min_len and len(chosen["abstract"]) < min_len:
            status, chosen = "source_also_placeholder", None
        if status == "title_mismatch":
            chosen = None  # never propose a value we cannot trust
        best = scored[0][2]
    elif rejected:
        # something was found, but only by title search and not well enough
        agree, status, chosen, best = rejected[0][0], "title_mismatch", None, rejected[0][1]
    else:
        agree, status, chosen, best = -1.0, "unreachable", None, None

    return {
        "id": row["id"],
        "identifier": row["identifier"],
        "title": clean_text(row["title"]),
        "db_abstract": clean_text(row["value"]),
        "resource_type": row["resource_type"],
        "source": best["source"] if best else "",
        "source_abstract": chosen["abstract"] if chosen else "",
        "abstract_len": len(chosen["abstract"]) if chosen else 0,
        "source_title": best["title"] if best else "",
        "title_agreement": round(agree, 2) if agree >= 0 else "",
        "rejected": len(rejected),
        "status": status,
    }


# ------------------------------------------------------------------ input rows

def read_csv(path, min_len, placeholders):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        out.append({"id": r.get("id", ""), "identifier": r.get("identifier", ""),
                    "title": r.get("title", ""), "value": r.get("abstract", ""),
                    "resource_type": r.get("resource_type", "")})
    return [r for r in out if is_missing(r["value"], placeholders, min_len)]


def read_db(schema, table, field, placeholders, min_len, limit):
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )
    try:
        ph = ",".join("%s" for _ in placeholders) or "NULL"
        sql = (f"SELECT id, identifier, title, {field} AS value, resource_type "
               f"FROM {schema}.{table} WHERE {field} IS NULL "
               f"OR length(btrim({field})) < %s "
               f"OR lower(btrim({field})) IN ({ph}) ORDER BY id")
        params = [min_len] + list(placeholders)
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "records_202609231630_empty_abstract.csv"))
    ap.add_argument("--from-db", action="store_true",
                    help="read missing rows from the database instead of --input")
    ap.add_argument("--field", default="abstract")
    ap.add_argument("--schema", default=os.getenv("DB_SCHEMA", "public"))
    ap.add_argument("--table", default=os.getenv("DB_TABLE", "records"))
    ap.add_argument("--min-len", type=int, default=20)
    ap.add_argument("--min-agreement", type=float, default=0.34)
    ap.add_argument("--title-jaccard", type=float, default=0.7,
                    help="min Jaccard title overlap for candidates found by "
                         "title search, which have no identifier to trust "
                         "(default 0.7)")
    ap.add_argument("--chain",
                    default="openalex,crossref,datacite,title_search,landing,pdf",
                    help="comma list of enabled resolvers; 'landing' and 'pdf' "
                         "serve URL-only rows, plus DOI rows with --doi-landing")
    ap.add_argument("--no-landing", action="store_true",
                    help="do not fetch landing pages for URL-only rows")
    ap.add_argument("--doi-landing", action="store_true",
                    help="also fetch the doi.org landing page as a last resort")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--interval", type=float, default=0.4,
                    help="min seconds between requests to one host")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cache", default="source_fetch_cache.json")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--csv", default="source_abstract_crosscheck.csv")
    ap.add_argument("--emit-sql", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-unverified", action="store_true")
    ap.add_argument("--show", type=int, default=8)
    ap.add_argument("--verbose", action="store_true",
                    help="print live per-record progress: source being "
                         "queried, page being loaded, abstract extracted")
    args = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.field):
        sys.exit("--field must be a plain column name")

    placeholders = set(DEFAULT_PLACEHOLDERS.get(args.field, []))
    rows = (read_db(args.schema, args.table, args.field, sorted(placeholders),
                    args.min_len, args.limit)
            if args.from_db else
            read_csv(args.input, args.min_len, placeholders))
    if args.limit and not args.from_db:
        rows = rows[: args.limit]

    chain = [c.strip() for c in args.chain.split(",") if c.strip()]
    for name in chain:
        if name not in RESOLVERS:
            sys.exit(f"unknown resolver: {name}")
    use_landing = "landing" in chain or "pdf" in chain
    if args.no_landing:
        chain = [n for n in chain if n not in ("landing", "pdf")]
        use_landing = False
        print("note: URL-only rows have no resolver and will be reported "
              "as unfetchable")
    elif "landing" not in chain:
        print("note: add 'landing' to --chain to cover URL-only rows")

    print(f"{len(rows)} rows with missing {args.field}")
    print(f"resolver chain: {' -> '.join(chain)}")

    # group rows by source record so each is fetched once
    groups, unresolvable = {}, []
    for row in rows:
        key = lookup_key(row["identifier"], row["title"])
        if key is None:
            unresolvable.append(row)
            continue
        groups.setdefault(key, []).append(row)
    print(f"{len(groups)} distinct source records, "
          f"{len(unresolvable)} with no resolvable identifier/url\n")

    cache = {}
    if not args.refresh and os.path.exists(args.cache):
        try:
            with open(args.cache, encoding="utf-8") as fh:
                cache = json.load(fh)
            print(f"cache: {len(cache)} entries loaded from {args.cache}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: cache unreadable ({exc}); starting fresh",
                  file=sys.stderr)
            cache = {}

    polite = Polite(args.interval)
    pending = [k for k in groups if k not in cache]
    print(f"{len(groups) - len(pending)} already cached, {len(pending)} to fetch")

    results, failures = {}, {}
    lock = threading.Lock()
    done = [0]
    submitted = [0]

    def work(key):
        titles = [r["title"] for r in groups[key]]
        if key.startswith("title:"):
            titles = [key.partition(":")[2]]
        with lock:
            submitted[0] += 1
            n = submitted[0]
        tag = f"[{n}/{len(pending)}]"
        return key, resolve_key(key, chain, polite, titles, args.min_agreement,
                                use_landing=use_landing,
                                doi_landing=args.doi_landing,
                                verbose=args.verbose, tag=tag)

    if pending:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(work, k): k for k in pending}
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    _, (cands_, errors, responded) = fut.result()
                except Exception as exc:  # keep going on unexpected errors
                    with lock:
                        failures[key] = f"crash:{type(exc).__name__}"
                        done[0] += 1
                    continue
                with lock:
                    if cands_:
                        cache[key] = cands_
                        if args.verbose:
                            vprint(f"[{done[0] + 1}/{len(pending)}] done: {key} "
                                   f"-> {len(cands_)} candidate(s) cached")
                    elif responded:
                        failures[key] = "no_abstract_in_source"
                        if args.verbose:
                            vprint(f"[{done[0] + 1}/{len(pending)}] done: {key} "
                                   f"-> no abstract in source")
                    else:
                        failures[key] = "unreachable:" + ",".join(errors)
                        if args.verbose:
                            vprint(f"[{done[0] + 1}/{len(pending)}] done: {key} "
                                   f"-> unreachable")
                    done[0] += 1
                    if not args.verbose and done[0] % 250 == 0:
                        print(f"  ...{done[0]}/{len(pending)} fetched "
                              f"({len(cache)} hits, {len(failures)} empty)")
                    if done[0] % 250 == 0:
                        with open(args.cache, "w", encoding="utf-8") as fh:
                            json.dump(cache, fh)

    with open(args.cache, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    print(f"cache written: {len(cache)} hits -> {args.cache}")

    # evaluate every row
    # a row whose only candidates were rejected by the title guards is a
    # mismatch, not an unreachable source
    buckets, all_rows = {}, []
    for row in rows:
        key = lookup_key(row["identifier"], row["title"])
        if key in cache:
            out = evaluate(row, cache[key], args.min_len, args.min_agreement,
                           args.title_jaccard)
        else:
            reason = failures.get(key) or (
                "no_resolvable_identifier" if key is None else "not_fetched")
            out = evaluate(row, [], args.min_len, args.min_agreement,
                           args.title_jaccard)
            out["source"] = reason
            if reason == "no_abstract_in_source":
                out["status"] = "source_no_abstract"
            elif reason.startswith(("unreachable", "crash")):
                out["status"] = "unreachable"
            else:
                out["status"] = "unfetchable"
        buckets.setdefault(out["status"], []).append(out)
        all_rows.append(out)

    order = ["recovered", "recovered_unverified", "title_mismatch",
             "source_also_placeholder", "source_no_abstract",
             "unreachable", "unfetchable"]
    for status in order:
        bucket = buckets.get(status, [])
        print("\n" + "=" * 100)
        print(f"{status.upper()} ({len(bucket)})")
        print("=" * 100)
        for r in bucket[: (len(bucket) if args.show == 0 else args.show)]:
            if not r["source_abstract"] and not r["source_title"]:
                print(f"\n  {r['identifier']}  [{r['source']}]")
                print(f"    db    : {preview(r['title'])}")
                continue
            print(f"\n  {r['identifier']}  [{r['source']}, {r['abstract_len']} chars]")
            print(f"    db    : {preview(r['title'])}")
            if r["source_title"]:
                print(f"    source: {preview(r['source_title'])}")
            if r["source_abstract"]:
                print(f"    abs   : {preview(r['source_abstract'])}")
        if len(bucket) > (len(bucket) if args.show == 0 else args.show):
            print(f"\n  ... and {len(bucket) - args.show} more (see {args.csv})")

    print("\nSummary: " + " / ".join(
        f"{len(buckets.get(s, []))} {s}" for s in order) + f" (of {len(rows)})")

    if all_rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nWrote {args.csv}")

    writable = list(buckets.get("recovered", []))
    if args.include_unverified:
        writable += buckets.get("recovered_unverified", [])

    if args.emit_sql:
        ph_sql = ",".join(sql_quote(p) for p in sorted(placeholders)) or "''"
        with open(args.emit_sql, "w", encoding="utf-8") as fh:
            fh.write("BEGIN;\n")
            for r in writable:
                fh.write(
                    f"UPDATE {args.schema}.{args.table} SET {args.field} = "
                    f"{sql_quote(r['source_abstract'])} "
                    f"WHERE id = {sql_quote(r['id'])} AND ({args.field} IS NULL "
                    f"OR length(btrim({args.field})) < {args.min_len} "
                    f"OR lower(btrim({args.field})) IN ({ph_sql}));\n")
            fh.write("-- COMMIT;\n")
        print(f"Wrote {args.emit_sql} ({len(writable)} statements, COMMIT commented out)")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return
    if not writable:
        print("\nNothing to do.")
        return

    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")))
    try:
        ph = ",".join("%s" for _ in placeholders) or "NULL"
        where = (f"({args.field} IS NULL OR length(btrim({args.field})) < %s "
                 f"OR lower(btrim({args.field})) IN ({ph}))")
        update = (f"UPDATE {args.schema}.{args.table} SET {args.field} = %s, "
                  f"updated_at = now() WHERE id = %s AND {where}")
        ph_sorted = sorted(placeholders)
        params = [(r["source_abstract"], r["id"], args.min_len, *ph_sorted)
                  for r in writable]
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, update, params, page_size=200)
        conn.commit()
        print(f"\nCommitted {len(params)} rows.")
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {args.schema}.{args.table} WHERE {where}",
                        [args.min_len] + ph_sorted)
            print(f"Rows still missing {args.field}: {cur.fetchone()[0]}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
