#!/usr/bin/env python3
"""Verify a dry-run diff CSV before signing off / applying. READ-ONLY (file only).

Checks performed on ``<prefix>_before_after.csv``:

1. **Residual issues** -- no HTML tag or double space may survive in any
   ``after`` value. Mojibake markers are split two ways:
   * markers *newly introduced* by the cleanup -> FAIL;
   * markers that were already in ``before`` and survived -> informational,
     because ftfy already refused to touch them (usually they are legitimate
     text that merely looks like mojibake, e.g. the name "Maria Ângela").
2. **Content preservation** -- visible text of ``before`` vs ``after`` (entities
   resolved on *both* sides, markup dropped) must keep the same words; more than
   --max-token-loss missing word-tokens is reported. Tokens that are themselves
   mojibake (``â€™``, ``Ã¸``) are ignored: their disappearance *is* the fix.
3. **Targets** -- every row targets ``both``, ``column`` or ``raw_metadata``,
   and the field is one that can legally live there.

Exit code 0 = clean, 1 = needs review.

Usage:
    python3 verify_diff.py messy_cleanup_v2_before_after.csv
    python3 verify_diff.py <csv> --samples 10
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize)  # the record-level `changes` JSON can be huge

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_rules import (  # noqa: E402
    JSON_LIST_FIELDS,
    MOJIBAKE_RE,
    MULTISPACE_RE,
    PLAIN_LIST_FIELDS,
    HTML_TAG_RE,
    HTML_STRIP_LIST_FIELDS,
)

VALID_TARGETS = {"both", "column", "raw_metadata"}
COLUMN_FIELDS = {"title", "abstract"}
RAW_FIELDS = COLUMN_FIELDS | set(HTML_STRIP_LIST_FIELDS) | set(PLAIN_LIST_FIELDS) \
    | set(JSON_LIST_FIELDS)
TEXT_FIELDS = set(COLUMN_FIELDS)  # plain strings (not JSON lists)
MOJIBAKE_CHARS = ("Ã", "Â", "â")  # tokens containing these are mojibake artefacts


def visible_text(s: str) -> str:
    """Resolve entities (repeatedly: data is often double-escaped), then drop markup.

    Uses the *same* tag regex as the cleaner, on both sides of the comparison:
    an HTML parser would treat a bare ``p < 0.05`` as an unclosed tag and silently
    swallow the rest of the paragraph, which looks exactly like deleted text.
    """
    for _ in range(3):
        unescaped = html.unescape(s)
        if unescaped == s:
            break
        s = unescaped
    return " ".join(HTML_TAG_RE.sub(" ", s).split())


def tokens(s: str) -> Counter:
    return Counter(re.findall(r"[\w']+", unicodedata.normalize("NFC", s), re.UNICODE))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--samples", type=int, default=5,
                    help="how many example rows to print per problem")
    ap.add_argument("--max-token-loss", type=int, default=3,
                    help="word tokens that may vanish from visible text before it is "
                         "reported (entity artefacts are expected to cancel out)")
    args = ap.parse_args()

    residual_tag = residual_space = bad_target = new_moj = 0
    kept_moj = 0
    token_loss = []
    n = 0

    with open(args.csv_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            n += 1
            target, field, before, after = r["target"], r["field"], r["before"], r["after"]
            ident = r["identifier"]

            # --- target legality -------------------------------------------------
            ok = target in VALID_TARGETS
            if ok:
                if target == "column":
                    ok = field in COLUMN_FIELDS
                elif target == "raw_metadata":
                    ok = field in RAW_FIELDS
                else:
                    ok = field in COLUMN_FIELDS
            if not ok:
                bad_target += 1
                if bad_target <= args.samples:
                    print(f"  BAD TARGET     [{ident}] {field}/{target}")

            # --- residual issues --------------------------------------------------
            if HTML_TAG_RE.search(after):
                residual_tag += 1
                if residual_tag <= args.samples:
                    print(f"  RESIDUAL TAG   [{ident}] {field}/{target}: {after[:110]!r}")
            if MULTISPACE_RE.search(after):
                residual_space += 1
                if residual_space <= args.samples:
                    print(f"  RESIDUAL SPACE [{ident}] {field}/{target}")

            marks_before = len(MOJIBAKE_RE.findall(before))
            marks_after = len(MOJIBAKE_RE.findall(after))
            if marks_after > marks_before:
                new_moj += 1
                if new_moj <= args.samples:
                    print(f"  NEW MOJIBAKE   [{ident}] {field}/{target} "
                          f"({marks_before} -> {marks_after})")
            elif marks_after:
                kept_moj += 1  # pre-existing and unfixed: legitimate text or beyond ftfy

            # --- content preservation --------------------------------------------
            if field in TEXT_FIELDS:
                lost = tokens(visible_text(before)) - tokens(visible_text(after))
                lost = Counter({k: v for k, v in lost.items()
                                if len(k) > 1 and not any(c in k for c in MOJIBAKE_CHARS)})
                total = sum(lost.values())
                if total > args.max_token_loss:
                    token_loss.append((total, ident, field, lost.most_common(args.samples)))

    print(f"\nrows checked: {n}")
    print(f"  residual HTML tags in 'after'   : {residual_tag}")
    print(f"  residual multi-space in 'after' : {residual_space}")
    print(f"  mojibake markers introduced     : {new_moj}")
    print(f"  pre-existing markers left alone : {kept_moj} (info: e.g. name 'Ângela')")
    print(f"  invalid target/field combos     : {bad_target}")
    print(f"  rows with possible text loss    : {len(token_loss)}")
    token_loss.sort(reverse=True)
    for total, ident, field, toks in token_loss[:args.samples]:
        print(f"    lost {total} tokens [{ident}] {field}: {toks}")

    ok = not (residual_tag or residual_space or new_moj or bad_target or token_loss)
    print("\nRESULT:", "PASS" if ok else "NEEDS REVIEW")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
