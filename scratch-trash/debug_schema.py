"""Check schema columns."""
import sqlite3
import tempfile
from pathlib import Path
from longtask.persistence.types import StoreConfig
from longtask.persistence.schema import connect, ensure_schema

with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "state.db"
    conn = connect(StoreConfig(db_path=p))
    ensure_schema(conn)
    cur = conn.execute("PRAGMA table_info(contracts)")
    cols = cur.fetchall()
    print(len(cols), "columns:")
    for c in cols:
        print(" ", c)
    conn.close()
