"""Debug: count actual values at runtime by wrapping conn.execute via subclass."""
import sys
sys.path.insert(0, ".")
import tempfile
from pathlib import Path
from longtask.persistence.types import StoreConfig
from lhgp.persistence.schema import connect, ensure_schema
from longtask.persistence import store
import lhgp.contracts.schema as ds
from datetime import datetime, UTC

original_execute = None


def patched_execute(self, sql, params=None, *args, **kwargs):
    if isinstance(sql, str) and "INSERT INTO contracts" in sql and "VALUES" in sql and "(SELECT" not in sql:
        ph_count = sql.count("?")
        if params is not None:
            try:
                param_count = len(params)
            except TypeError:
                param_count = -1
            print(f"INSERT has {ph_count} placeholders, {param_count} params")
    return original_execute(self, sql, params, *args, **kwargs)


class TracedConnect:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        if name == "execute":
            return lambda sql, *a, **k: patched_execute(self._conn, sql, *a, **k)
        return getattr(self._conn, name)


with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "state.db"
    conn = connect(StoreConfig(db_path=p))
    ensure_schema(conn)
    # Wrap via Proxy
    import sqlite3

    original_execute = sqlite3.Connection.execute
    sqlite3.Connection.execute = patched_execute
    try:
        save_contract(
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
    finally:
        sqlite3.Connection.execute = original_execute
    conn.close()
