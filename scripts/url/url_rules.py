"""Identifier -> URL rules for the ``records.url`` column (docs/url/url_table_plan.md §1).

Single source of truth, shared by recon, the dry run and the apply pass
(*detector == generator*, same discipline as ``cleanup_rules.py``): the type and
the URL come out of one function, so a preview CSV and the written column cannot
disagree.

Pure string logic -- no database access, no file I/O, no network.

Rules are evaluated top to bottom, first match wins. Input is ``identifier``
after ``strip()`` only. Types that have no public resolver (``uuid``,
``oai_recolecta``, ``isbn``, ``issn``, ``other``, ``empty``) yield ``url=None``:
NULL is never replaced by a guessed or search URL.
"""
from __future__ import annotations

import json
import re
from typing import NamedTuple, Optional
from urllib.parse import unquote, urlsplit, urlunsplit


class Result(NamedTuple):
    type: str
    url: Optional[str]


# --------------------------------------------------------------------------- #
# Patterns                                                                     #
# --------------------------------------------------------------------------- #
DOI_URL_RE = re.compile(r"(?i)^https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/\S+)$")
DOI_RE = re.compile(r"(?i)^(?:doi:\s*)?(10\.\d{4,9}/\S+)$")
URL_RE = re.compile(r"(?i)^https?://\S+$")
ARXIV_RE = re.compile(
    r"(?i)^arxiv:(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$")
HANDLE_RE = re.compile(r"(?i)^hdl:(\d+(?:\.\d+)*/\S+)$")
URN_RE = re.compile(r"(?i)^urn:nbn:[a-z]{2}:\S+$")
UUID_RE = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
OAI_RE = re.compile(r"^\d+\|\S+")
ISBN_RE = re.compile(r"(?i)^isbn(?:-1[03])?:?\s*([0-9][0-9\- ]{8,16}[0-9Xx])$")
ISSN_RE = re.compile(r"(?i)^issn:?\s*(\d{4}-\d{3}[\dXx])$")
# DSpace-style OAI id: oai:<host>:<numeric handle prefix>/<suffix>  (tier C)
OAI_HANDLE_RE = re.compile(r"(?i)^oai:[^:\s]+:(\d+/\S+)$")

# URL-final check used by the DB CHECK constraint and the verify gate.
URL_FORMAT_RE = re.compile(r"^https?://[^\s]+$")

# Characters that are illegal in a URL path (plan §1). ``#`` and ``?`` are added
# on top of the plan's list: left raw they would turn the DOI tail into a
# fragment / query string and doi.org would resolve the wrong thing.
_ILLEGAL = set(' <>"{}|\\^`#?')


def _encode(s: str) -> str:
    return "".join(f"%{ord(c):02X}" if c in _ILLEGAL else c for c in s)


# --------------------------------------------------------------------------- #
# Normalisers                                                                  #
# --------------------------------------------------------------------------- #
def _strip_trailing(s: str) -> str:
    """Drop trailing . , ; always, and ) only when it has no matching ( ."""
    while s:
        if s[-1] in ".,;":
            s = s[:-1]
        elif s[-1] == ")" and s.count(")") > s.count("("):
            s = s[:-1]
        else:
            break
    return s


def doi_to_url(doi: str) -> str:
    """``10.xxxx/suffix`` -> ``https://doi.org/10.xxxx/suffix``.

    Only the ``10.`` prefix is case-folded; the suffix is case-sensitive and is
    never altered beyond percent-encoding of illegal characters.
    """
    doi = _strip_trailing(doi)
    return "https://doi.org/" + "10." + _encode(doi[3:])


def normalise_url(u: str) -> str:
    """Lowercase scheme + host only; path/query/fragment untouched."""
    u = u.rstrip(".,")
    p = urlsplit(u)
    userinfo, sep, host = p.netloc.rpartition("@")
    netloc = userinfo + sep + host.lower()
    # urlunsplit would drop an empty '?' / '#'; harmless, but keep the rest as is
    return urlunsplit((p.scheme.lower(), netloc, p.path, p.query, p.fragment))


# --------------------------------------------------------------------------- #
# ISBN / ISSN (type reporting only -- these never produce a URL)               #
# --------------------------------------------------------------------------- #
def _isbn_ok(raw: str) -> bool:
    d = re.sub(r"[\s-]", "", raw).upper()
    if len(d) == 10 and re.fullmatch(r"\d{9}[\dX]", d):
        s = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
        return s % 11 == 0
    if len(d) == 13 and d.isdigit():
        s = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d))
        return s % 10 == 0
    return False


def _issn_ok(raw: str) -> bool:
    d = raw.replace("-", "").upper()
    s = sum((8 - i) * int(c) for i, c in enumerate(d[:7]))
    check = (11 - s % 11) % 11
    return d[7] == ("X" if check == 10 else str(check))


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def _as_dict(x):
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        try:
            d = json.loads(x)
            return d if isinstance(d, dict) else None
        except ValueError:
            return None
    return None


def url_from_raw(raw) -> Optional[Result]:
    """Tiers A + B: a URL the source record itself declares as its landing page.

    A  ``raw_metadata.distribution.landingPage.url``  (MCF records)
    B  BONARES: first link (``format == "link-0"``) is the record's own
       ``maps.bonares.de`` page
    Anything else in ``links`` is deliberately NOT used (downloads, WMS, GitHub
    sources: choosing one would be a guess).
    """
    raw = _as_dict(raw)
    if not raw:
        return None
    lp = _as_dict(((raw.get("distribution") or {}).get("landingPage"))) or {}
    u = (lp.get("url") or "").strip()
    if URL_RE.match(u):
        return Result("landing_page", normalise_url(u))
    links = raw.get("links")
    if "bonares" in (raw.get("sources") or []) and isinstance(links, list) and links:
        first = _as_dict(links[0]) or {}
        u = unquote((first.get("url") or "").strip())
        if first.get("format") == "link-0" and URL_RE.match(u) \
                and urlsplit(u).netloc.lower() == "maps.bonares.de":
            return Result("bonares_landing", normalise_url(u))
    return None


def classify(identifier, raw_metadata=None) -> Result:
    """Return ``(type, url)`` for one ``records`` row.

    ``raw_metadata`` is optional; when given, records whose identifier has no
    resolver may still get a landing-page URL (tiers A/B, see ``url_from_raw``).
    """
    if identifier is None or not str(identifier).strip():
        return Result("empty", None)
    s = str(identifier).strip()

    if m := DOI_URL_RE.match(s):
        return Result("doi_url", doi_to_url(m.group(1)))
    if m := DOI_RE.match(s):
        return Result("doi", doi_to_url(m.group(1)))
    if URL_RE.match(s):
        return Result("url", normalise_url(s))
    if m := ARXIV_RE.match(s):
        return Result("arxiv", "https://arxiv.org/abs/" + m.group(1))
    if m := HANDLE_RE.match(s):
        return Result("handle", "https://hdl.handle.net/" + _encode(m.group(1)))
    if URN_RE.match(s):
        return Result("urn", "https://nbn-resolving.org/" + _encode(s))
    if m := OAI_HANDLE_RE.match(s):
        return Result("oai_handle", "https://hdl.handle.net/" + _encode(m.group(1)))

    if UUID_RE.match(s):
        base = Result("uuid", None)
    elif OAI_RE.match(s):
        base = Result("oai_recolecta", None)
    elif (m := ISBN_RE.match(s)) and _isbn_ok(m.group(1)):
        base = Result("isbn", None)
    elif (m := ISSN_RE.match(s)) and _issn_ok(m.group(1)):
        base = Result("issn", None)
    else:
        base = Result("other", None)
    if raw_metadata is not None and base.type in ("uuid", "other"):
        return url_from_raw(raw_metadata) or base
    return base


def doi_suffix_intact(identifier: str, url: str) -> bool:
    """Verify-gate helper: URL suffix == input suffix modulo percent-encoding
    and the trailing-punctuation strip."""
    s = str(identifier).strip()
    m = DOI_URL_RE.match(s) or DOI_RE.match(s)
    if not m or not url.startswith("https://doi.org/"):
        return False
    want = "10." + _strip_trailing(m.group(1))[3:]
    return unquote(url[len("https://doi.org/"):]) == want
