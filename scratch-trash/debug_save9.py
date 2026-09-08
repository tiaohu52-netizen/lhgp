"""Run the save_contract body manually to see what values it builds."""
import sys
sys.path.insert(0, ".")
import tempfile
from pathlib import Path
from longtask.persistence.types import StoreConfig
from lhgp.persistence.schema import connect, ensure_schema
from longtask.persistence import store as st
import lhgp.contracts.schema as ds
from longtask.contracts.authority import to_dict as authority_to_dict
from longtask.contracts.attention import to_dict as attention_to_dict
from longtask.contracts.continuity import to_dict as continuity_to_dict
from datetime import datetime, UTC
import json

with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "state.db"
    conn = connect(StoreConfig(db_path=p))
    ensure_schema(conn)
    draft = ds.ContractDraft(
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
    )
    # Manually build what save_contract builds
    values = (
        "lt-test",
        "lt-test",
        1,
        "drafted",
        "not_due",
        "pending",
        None,
        draft.title,
        draft.objective,
        draft.deadline_at.isoformat(),
        json.dumps(draft.hard_constraints, ensure_ascii=False),
        json.dumps(
            {
                "standard": draft.acceptance.standard,
                "checks": ["c1"],
                "verifier": "cross_check",
            },
            ensure_ascii=False,
        ),
        draft.workload_initial_hours,
        json.dumps(
            {
                "max_dispatches": 3,
                "max_escalations": 1,
                "max_concurrent_attempts": 1,
                "max_attempt_minutes": 60,
                "max_output_bytes": 1048576,
                "verification_attempts_reserved": 3,
            },
            ensure_ascii=False,
        ),
        json.dumps(draft.soft_guidance, ensure_ascii=False),
        json.dumps(draft.context, ensure_ascii=False),
        json.dumps(draft.execution, ensure_ascii=False),
        json.dumps(draft.client_meta, ensure_ascii=False),
        json.dumps(authority_to_dict(draft.authority), ensure_ascii=False),
        json.dumps(attention_to_dict(draft.attention), ensure_ascii=False),
        json.dumps(continuity_to_dict(draft.continuity), ensure_ascii=False),
        json.dumps(
            draft.auto_approve.to_dict() if hasattr(draft.auto_approve, 'to_dict') else draft.auto_approve,
            ensure_ascii=False,
        ),
        "2026-09-08T00:00:00+00:00",
        "2026-09-08T00:00:00+00:00",
        None,
        None,
        2,
    )
    print("values count:", len(values))
    conn.close()
