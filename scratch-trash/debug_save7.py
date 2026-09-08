"""Trace save_contract values."""
import sys
sys.path.insert(0, ".")
import tempfile
from pathlib import Path
from longtask.persistence.types import StoreConfig
from lhgp.persistence.schema import connect, ensure_schema
from longtask.persistence import store as st
import lhgp.contracts.schema as ds
from datetime import datetime, UTC

import sqlite3


class _TraceCursor:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, params=None):
        if isinstance(sql, str) and "INSERT INTO contracts" in sql and "VALUES" in sql and "SELECT" not in sql:
            ph = sql.count("?")
            plen = len(params) if hasattr(params, "__len__") else -1
            print(f"PH={ph} param_count={plen}")
            if plen != ph and plen > 0:
                print("SQL:", sql[:200])
                print("PARAMS:", params)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TraceConn:
    def __init__(self, real):
        self._real = real

    def cursor(self):
        return _TraceCursor(self._real.cursor())

    def __getattr__(self, name):
        return getattr(self._real, name)


with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "state.db"
    conn = connect(StoreConfig(db_path=p))
    ensure_schema(conn)
    trace = _TraceConn(conn)
    try:
        st.save_contract(
            trace,
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
