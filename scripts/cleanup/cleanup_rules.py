"""Shared cleaning + detection rules for the SoilWise messy-metadata pass.

Single source of truth for the regexes and the text-cleaning functions used by
* both ``find_messy.py`` (scan / flag records) and
* ``dry_run.py``   (produce the before/after diff).

Why a shared module: the original two scripts each carried their own copy of
the patterns, and they had drifted apart (the scanner used ``<[a-zA-Z/][^>]{0,60}>``
while the cleaner used a stricter tag regex), so a record could be flagged as
"has HTML tags" and still not be fixable. Anything that is detected here must
be fixable here.

This module only touches strings -- no database access, no file I/O -- so it can
be imported by the scan, the dry run, a verification script, or the future apply
pass without side effects.
"""
from __future__ import annotations

import json
import re

import ftfy

# --------------------------------------------------------------------------- #
# Detection patterns (find_messy.py flags a record when one of these matches)  #
# --------------------------------------------------------------------------- #

# Must stay in sync with strip_html() below: if it flags it, strip_html removes it.
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:_-]*(?:\s[^>]*)?/?>")
MULTISPACE_RE = re.compile(r"[ \t]{2,}")
STRAY_BACKSLASH_QUOTE_RE = re.compile(r'\\+(["\'])')
PIPE_RE = re.compile(r"\|")
# pipes (and the whitespace around them) hanging off either end of a name value
EDGE_PIPE_RE = re.compile(r"^[\s|]+|[\s|]+$")
BACKSLASH_RE = re.compile(r"\\")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
URLENC_RE = re.compile(r"%[0-9A-Fa-f]{2}")
# common mojibake byte sequences from UTF-8-as-Latin1/CP1252 misdecodes
MOJIBAKE_RE = re.compile(r"Ã.|Â.|â€.")
# Emoji + pictographs + regional indicator pairs (flags).
# Deliberately excludes U+2190-21FF (arrows) and U+2B00-2BFF (geometric shapes),
# which the original pattern swept in and reported as false "emoji".
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # emoticons, transport, misc symbols (incl. 🦋)
    "\U00002600-\U000027BF"  # dingbats, misc symbols
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "]"
)

# What strip_emoji() removes: an emoji plus a trailing variation selector / ZWJ.
EMOJI_STRIP_RE = re.compile(EMOJI_RE.pattern + "[\ufe0f\u200d]*")

# Issue labels are emitted in this order so scanner output stays comparable.
ISSUE_ORDER = (
    "emoji", "pipe", "backslash", "control_char",
    "mojibake", "html_tag", "url_encoded", "multi_space",
)


def scan_text(s: str) -> list[str]:
    """Return the issue labels that apply to one string, in ISSUE_ORDER."""
    if not isinstance(s, str) or not s:
        return []
    hits = {
        "emoji": EMOJI_RE.search(s),
        "pipe": PIPE_RE.search(s),
        "backslash": BACKSLASH_RE.search(s),
        "control_char": CONTROL_RE.search(s),
        "mojibake": MOJIBAKE_RE.search(s),
        "html_tag": HTML_TAG_RE.search(s),
        "url_encoded": URLENC_RE.search(s),
        "multi_space": MULTISPACE_RE.search(s),
    }
    return [label for label in ISSUE_ORDER if hits[label]]


# --------------------------------------------------------------------------- #
# Cleaning functions (dry_run.py / future apply pass)                          #
# --------------------------------------------------------------------------- #

FTFY_CONFIG = ftfy.TextFixerConfig(
    unescape_html="auto",        # decode &amp; / &#8211; / &nbsp; (see clean_plain_text)
    uncurl_quotes=False,
    fix_character_width=False,
    fix_latin_ligatures=False,
    normalization="NFC",         # documented side effect: decomposed accents -> composed
    # everything else (fix_encoding, restore_byte_a0) stays on:
    # reversing encoding corruption is the point.
)


def strip_html(s: str) -> str:
    """Replace markup with a space (never delete) so words don't merge."""
    return HTML_TAG_RE.sub(" ", s)


def collapse_space(s: str) -> str:
    return MULTISPACE_RE.sub(" ", s).strip()


def fix_mojibake(s: str) -> str:
    fixed = ftfy.fix_text(s, config=FTFY_CONFIG)
    if MOJIBAKE_RE.search(fixed):
        # ftfy's heuristics can miss short/rare mojibake runs (e.g. names).
        # Fall back to a manual cp1252-as-utf8 roundtrip, but only keep it if
        # it actually reduces the mojibake marker count (never apply blind).
        try:
            candidate = fixed.encode("sloppy-windows-1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            candidate = None
        if candidate is not None:
            before_hits = len(MOJIBAKE_RE.findall(fixed))
            after_hits = len(MOJIBAKE_RE.findall(candidate))
            if after_hits < before_hits:
                fixed = candidate
    return fixed


def strip_emoji(s: str) -> str:
    """Delete emoji/pictographs (and their variation selectors). Spacing is fixed by collapse_space."""
    return EMOJI_STRIP_RE.sub("", s)


def strip_edge_pipes(s: str) -> str:
    """Remove leading/trailing pipes (``Name||``, ``|Name||``); inner pipes are kept."""
    return EDGE_PIPE_RE.sub("", s)


def unescape_stray(s: str) -> str:
    """Turn literal ``\\"`` / ``\\'`` sequences in plain text into a plain quote."""
    return STRAY_BACKSLASH_QUOTE_RE.sub(r"\1", s)


def clean_plain_text(s: str, do_html: bool = False) -> str:
    """Clean one string. ``do_html`` enables markup stripping (title/abstract/subjects)."""
    if do_html:
        s = strip_html(s)
    s = strip_emoji(s)
    s = unescape_stray(s)
    s = fix_mojibake(s)
    if do_html:
        # Second strip: ftfy's `unescape_html="auto"` only preserves entities when
        # a literal `<` is present in *its* input. Pass 1 has already removed the
        # tags, so `&amp;lt;p&amp;gt;` / `CO&lt;sub&gt;2` decode all the way back to
        # `<p>` / `<sub>` and reintroduce markup. (review bug #1: 9 titles + 29 abstracts)
        s = strip_html(s)
    return collapse_space(s)


def clean_list_plain(lst: list) -> tuple[list, bool]:
    """persons / organizations / contact_persons / contact_organizations / matched_subjects.

    Drops placeholder entries (``'|'`` and empty strings) and cleans the rest.
    """
    out, changed = [], False
    for item in lst:
        if not isinstance(item, str):
            out.append(item)
            continue
        if item.strip() in ("|", ""):
            changed = True  # dropping an element is always a change
            continue
        new_item = strip_edge_pipes(clean_plain_text(item, do_html=False))
        if not new_item:
            changed = True  # only pipes/whitespace: drop like a placeholder
            continue
        if new_item != item:
            changed = True
        out.append(new_item)
    return out, changed


def clean_list_html(lst: list) -> tuple[list, bool]:
    """subjects: markup + mojibake + spacing. Pipes are intentional separators -- kept."""
    out, changed = [], False
    for item in lst:
        if not isinstance(item, str):
            out.append(item)
            continue
        new_item = clean_plain_text(item, do_html=True)
        if new_item != item:
            changed = True
        out.append(new_item)
    return out, changed


def clean_json_list(lst: list) -> tuple[list, bool]:
    """view_authors / view_contacts: items are JSON-encoded strings.

    Clears a ``'|'`` placeholder name/organisation, otherwise cleans the values.
    Anything that is not a JSON object is passed through untouched.
    """
    out, changed = [], False
    for item in lst:
        if not isinstance(item, str):
            out.append(item)
            continue
        try:
            obj = json.loads(item)
        except (ValueError, TypeError):
            out.append(item)
            continue
        if not isinstance(obj, dict):
            out.append(item)
            continue
        obj_changed = False
        for k in ("person", "organization"):
            v = obj.get(k)
            if not isinstance(v, str):
                continue
            newv = strip_edge_pipes(clean_plain_text(v, do_html=False))
            if newv != v:
                obj[k] = newv
                obj_changed = True
        if obj_changed:
            changed = True
            out.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        else:
            out.append(item)
    return out, changed


# --------------------------------------------------------------------------- #
# Field groups                                                                 #
# --------------------------------------------------------------------------- #

# Top-level columns records.title / records.abstract *and* the same key inside
# raw_metadata -- these two copies are cleaned independently (never synced).
TEXT_TOP_FIELDS = ("title", "abstract")
# jsonb-only fields
HTML_STRIP_LIST_FIELDS = ("subjects",)                                  # list[str]
PLAIN_LIST_FIELDS = (
    "persons", "organizations", "contact_persons",
    "contact_organizations", "matched_subjects",
)                                                                       # list[str]
JSON_LIST_FIELDS = ("view_authors", "view_contacts")                    # list[str] of JSON

# Fields the scanner looks at but the cleaner deliberately does NOT touch:
# pipes in subjects/title are intentional separators, LaTeX/R/markdown
# backslashes and %xx inside URLs are content (see report section 2).
CLEANED_FIELDS = set(TEXT_TOP_FIELDS) | set(HTML_STRIP_LIST_FIELDS) \
    | set(PLAIN_LIST_FIELDS) | set(JSON_LIST_FIELDS)
