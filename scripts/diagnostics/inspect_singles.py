#!/usr/bin/env python3
"""[helper] Find literal \\xNN escapes and control characters in abstracts. READ-ONLY.

Supports the two one-off manual-fix cases called out in the cleanup report:
`S\\xF6derstr\\xF6m` (double-mangled name) and stray \\x7f DEL characters.
"""
import os
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
cur = conn.cursor()
cur.execute("select identifier, abstract from records where abstract like '%\\\\x%'")
for ident, ab in cur.fetchall():
    idx = ab.find('\\x')
    print(ident, repr(ab[max(0, idx - 60):idx + 60]))

print('---control char---')
cur.execute(r"select identifier, abstract from records where abstract ~ '[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]'")
for ident, ab in cur.fetchall():
    for i, c in enumerate(ab):
        if ord(c) < 32 and c not in '\n\t\r' or ord(c) == 127:
            print(ident, 'char code', ord(c), repr(ab[max(0, i - 40):i + 40]))
cur.close()
conn.close()
