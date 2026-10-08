#!/usr/bin/env python3
"""[helper] Pull a few real samples per (field, issue) straight from the DB. READ-ONLY.

The scan counts patterns; this is what turns counts into decisions ("is this
pipe an intentional separator, or corruption?"). Run it whenever a new issue
type appears before adding a cleaning rule for it.

Note: detection patterns now live in cleanup_rules.py; this helper keeps its
own copies on purpose so you can tune a candidate pattern here without
changing what the real scan flags.
"""
import json
import os
import re
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / "crosscheck" / ".env")
conn = psycopg2.connect(
    host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'),
    dbname=os.getenv('DB_NAME'), user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'), sslmode=os.getenv('DB_SSLMODE', 'prefer'),
    connect_timeout=int(os.getenv('DB_CONNECT_TIMEOUT', '10'))
)
cur = conn.cursor(name='sample_scan')
cur.itersize = 2000
cur.execute("select id, identifier, title, abstract, raw_metadata from records")

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
PIPE_RE = re.compile(r"\|")
BACKSLASH_RE = re.compile(r"\\")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
URLENC_RE = re.compile(r"%[0-9A-Fa-f]{2}")
MOJIBAKE_RE = re.compile(r"Ã.|Â.|â€.")


def flatten(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        out = []
        for i in v:
            out.extend(flatten(i))
        return out
    if isinstance(v, dict):
        out = []
        for i in v.values():
            out.extend(flatten(i))
        return out
    return [str(v)]


samples = {
    ('persons', 'pipe'): [], ('view_authors', 'pipe'): [], ('contact_persons', 'pipe'): [],
    ('view_contacts', 'pipe'): [], ('subjects', 'pipe'): [], ('title', 'pipe'): [],
    ('abstract', 'pipe'): [], ('abstract', 'backslash'): [], ('view_contacts', 'backslash'): [],
    ('contact_persons', 'backslash'): [], ('abstract', 'mojibake'): [],
    ('contact_persons', 'mojibake'): [], ('view_contacts', 'mojibake'): [],
    ('abstract', 'url_encoded'): [], ('abstract', 'control_char'): [],
}
LIMIT = 4

for id_, identifier, title, abstract, raw in cur:
    raw = raw or {}
    for field in ['title', 'abstract', 'persons', 'organizations', 'contact_persons',
                  'contact_organizations', 'subjects', 'view_authors', 'view_contacts']:
        val = raw.get(field)
        if val is None:
            if field == 'title':
                val = title
            elif field == 'abstract':
                val = abstract
        for t in flatten(val):
            if not isinstance(t, str) or not t:
                continue
            for issue, rx in [('pipe', PIPE_RE), ('backslash', BACKSLASH_RE),
                              ('mojibake', MOJIBAKE_RE), ('url_encoded', URLENC_RE),
                              ('control_char', CONTROL_RE)]:
                key = (field, issue)
                if key in samples and len(samples[key]) < LIMIT and rx.search(t):
                    samples[key].append((str(id_), identifier, t[:250]))

for key, items in samples.items():
    if not items:
        continue
    print("=====", key, "=====")
    for id_, ident, t in items:
        print(f"  [{ident}] {t!r}")
    print()

cur.close()
conn.close()
