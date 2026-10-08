#!/usr/bin/env python3
"""[helper] Dump the DB schema of `records` + row count. READ-ONLY.

Use this first when connecting to a new environment: confirms column names
and that DB_TABLE in crosscheck/.env points where you think it does.
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
cur.execute(
    "select column_name, data_type from information_schema.columns "
    "where table_name=%s order by ordinal_position",
    (os.getenv('DB_TABLE', 'records'),))
for r in cur.fetchall():
    print(r)
cur.execute("select count(*) from records")
print(cur.fetchone())
cur.close()
conn.close()
