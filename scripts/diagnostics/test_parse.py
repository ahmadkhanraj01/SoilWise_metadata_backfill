#!/usr/bin/env python3
"""[helper] Check whether view_* values really are JSON-encoded strings. READ-ONLY.

Feeds two known records through json.loads the same way the cleaner does, so
you can see parse-OK vs parse-FAILED before trusting clean_json_list().
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

cur.execute("select id, raw_metadata->'view_contacts' from records where identifier='10.1002/wat2.1616'")
row = cur.fetchone()
print(row[0])
for item in row[1]:
    print(repr(item))
    try:
        parsed = json.loads(item)
        print('  parsed OK:', parsed)
    except Exception as e:
        print('  parse FAILED:', e)

cur.execute("select id, raw_metadata->'contact_persons' from records where identifier='10.5281/zenodo.15019232'")
row = cur.fetchone()
print(row[0])
for item in row[1]:
    print(repr(item))

cur.close()
conn.close()
