#!/usr/bin/env python3
"""[helper] Print one sample `raw_metadata` jsonb document, pretty-printed. READ-ONLY.

Used to learn the shape of the jsonb (which keys exist, whether a key holds a
string, a list of strings, or a list of JSON-encoded strings) before writing
cleaning rules.
"""
import json
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
cur.execute("select raw_metadata from records where raw_metadata is not null limit 1")
row = cur.fetchone()
print(json.dumps(row[0], indent=2)[:3000])
cur.close()
conn.close()
