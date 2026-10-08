#!/usr/bin/env python3
"""[helper] Print the Python types of every cleaning-relevant raw_metadata key. READ-ONLY.

Confirms which keys are str, list[str] or list-of-JSON-string before choosing
a cleaner (clean_list_plain vs clean_json_list) for them.
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
cur.execute("select id, raw_metadata from records where raw_metadata ? 'persons' limit 1")
id_, raw = cur.fetchone()
for f in ['persons', 'organizations', 'contact_persons', 'contact_organizations',
          'matched_subjects', 'license', 'soilmission', 'project_acronyms',
          'projects', 'sources', 'view_authors', 'view_contacts']:
    print(f, '->', type(raw.get(f)), repr(raw.get(f))[:200])
cur.close()
conn.close()
