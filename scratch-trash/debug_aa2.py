"""Debug: trace auto_approve through parse_contract_draft."""

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
)
from longtask.rpc.handlers._common import parse_contract_draft

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    conn = connect(StoreConfig(db_path=tmp / "state.db"))
    ensure_schema(conn)
    try:
        params = {
            "title": "auto-approve test",
            "objective": "verify auto-approve survives prepare",
            "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "acceptance_standard": "ok",
            "acceptance_checks": ["ok"],
            "auto_approve": {"enabled": True, "actions": ["verify acceptance"]},
        }
        draft = parse_contract_draft(params)
        print("draft.auto_approve:", draft.auto_approve)
        print("draft.to_dict auto_approve:", draft.to_dict()["auto_approve"])
    finally:
        conn.close()
