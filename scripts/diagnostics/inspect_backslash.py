#!/usr/bin/env python3
"""[helper] Classify every backslash in abstracts: \\xNN vs LaTeX-like vs other. READ-ONLY.

Produced the evidence that almost all backslashes are legitimate (LaTeX math,
R code, grant numbers) and only a handful are real corruption -- which is why
dry_run.py does NOT touch backslashes in body text.
"""
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
cur = conn.cursor(name='c1')
cur.itersize = 2000
cur.execute("select identifier, abstract from records where abstract like '%\\\\%'")

literal_escape = re.compile(r'\\x[0-9A-Fa-f]{2}')
latex_like = re.compile(r'\\[A-Za-z]+')
n_literal = n_latex = n_other = 0
examples_other = []

for ident, ab in cur:
    if literal_escape.search(ab):
        n_literal += 1
    elif latex_like.search(ab):
        n_latex += 1
    else:
        n_other += 1
        if len(examples_other) < 5:
            idx = ab.find('\\')
            examples_other.append((ident, ab[max(0, idx - 80):idx + 80]))

print('literal_escape (\\xNN):', n_literal)
print('latex-like (\\Word):', n_latex)
print('other:', n_other)
for e in examples_other:
    print(e)

cur.close()
conn.close()
