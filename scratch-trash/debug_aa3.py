"""Debug: trace through the route."""

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from longtask.adapters.registry import ExecutorRegistry
from longtask.mcp_server import tool_prepare_contract
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
)
from longtask.persistence.store import (
    get_contract as get_contract_lhgp,
)

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    conn = connect(StoreConfig(db_path=tmp / "state.db"))
    ensure_schema(conn)
    try:
        prepared = tool_prepare_contract(
            {
                "title": "auto-approve test",
                "objective": "verify auto-approve survives prepare",
                "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                "acceptance_standard": "ok",
                "acceptance_checks": ["ok"],
                "auto_approve": {"enabled": True, "actions": ["verify acceptance"]},
            },
            {"conn": conn, "registry": ExecutorRegistry(), "root": tmp},
        )
        cid = prepared["result"]["contract_id"]
        # Check what's actually in the DB
        row = conn.execute(
            "SELECT auto_approve_json FROM contracts WHERE contract_id = ?", (cid,)
        ).fetchone()
        print("DB auto_approve_json:", row[0])
        v = get_contract_lhgp(conn, cid)
        print("Loaded auto_approve:", v.draft.auto_approve)
    finally:
        conn.close()
