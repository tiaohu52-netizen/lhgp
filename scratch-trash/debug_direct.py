"""Run the actual save_contract flow but break on the SQL."""
import sys
sys.path.insert(0, ".")
import tempfile
from pathlib import Path
from longtask.persistence.types import StoreConfig
from lhgp.persistence.schema import connect, ensure_schema
import lhgp.contracts.schema as ds
from datetime import datetime, UTC
import json

# Monkey-patch the INSERT statement and count its params at runtime
import longtask.persistence.store as stmod
orig_save = stmod.save_contract


def patched_save(*args, **kwargs):
    # Use the original logic but call the cursor directly to inspect
    # This is a no-op patch
    return orig_save(*args, **kwargs)


# Actually, let me use ctypes to read the conn's sqlite3_stmt and count the actual bound params
import ctypes
import sqlite3


# Simplest approach: dump the actual SQL by patching the connection's
# executemany or using sqlite3 trace
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "state.db"
    conn = connect(StoreConfig(db_path=p))
    ensure_schema(conn)

    def trace(sql):
        print("SQL:", sql[:300])

    conn.set_trace_callback(trace)
    try:
        stmod.save_contract(
            conn,
            ds.ContractDraft(
                title="test",
                objective="test",
                deadline_at=datetime(2099, 1, 1, tzinfo=UTC),
                hard_constraints={"file_effects": {"mode": "workspace-write"}},
                acceptance=ds.Acceptance(standard="s", checks=("c1",)),
                workload_initial_hours=1.0,
                budget=ds.Budget(
                    max_dispatches=3,
                    max_escalations=1,
                    max_concurrent_attempts=1,
                    max_attempt_minutes=60,
                    max_output_bytes=1048576,
                ),
            ),
            contract_id="lt-test",
            now=datetime.now(UTC),
        )
        print("OK")
    except Exception as e:
        print("ERR:", e)
    conn.close()
