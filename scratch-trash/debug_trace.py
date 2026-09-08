"""Capture full SQL via trace."""
import sys
sys.path.insert(0, ".")
import tempfile
from pathlib import Path
from longtask.persistence.types import StoreConfig
from lhgp.persistence.schema import connect, ensure_schema
import longtask.persistence.store as st
import lhgp.contracts.schema as ds
from datetime import datetime, UTC

with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "state.db"
    conn = connect(StoreConfig(db_path=p))
    ensure_schema(conn)
    trace_lines = []

    def trace(sql):
        trace_lines.append(sql)

    conn.set_trace_callback(trace)
    try:
        st.save_contract(
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
    print("---trace---")
    for line in trace_lines:
        if "INSERT INTO contracts" in line:
            print("PLACEHOLDERS:", line.count("?"))
            print("SQL (truncated):", line[:500])
    conn.close()
