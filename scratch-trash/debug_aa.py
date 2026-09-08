"""Debug: verify auto_approve survives prepare."""

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from lhgp.persistence.store import get_contract as get_contract_lhgp
from longtask.adapters.registry import ExecutorRegistry
from longtask.mcp_server import tool_prepare_contract, tool_submit_plan
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
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
        v = get_contract_lhgp(conn, cid)
        print("auto_approve:", v.draft.auto_approve)
        print("to_dict:", v.draft.auto_approve.to_dict())
        result = tool_submit_plan(
            {
                "contract_id": cid,
                "steps": [
                    {
                        "step_id": 1,
                        "action": "verify acceptance",
                        "target": "ok",
                        "rationale": "verify acceptance of objective 'verify auto-approve survives prepare'",
                        "expected_outcome": "ok",
                    }
                ],
            },
            {"conn": conn, "registry": ExecutorRegistry(), "root": tmp},
        )
        print("plan result:", result)
    finally:
        conn.close()
