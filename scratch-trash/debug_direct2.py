"""Replicate save_contract call and count tuple."""
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
    # Manually construct the values exactly as the source would
    from longtask.contracts.authority import to_dict as authority_to_dict
    from longtask.contracts.attention import to_dict as attention_to_dict
    from longtask.contracts.continuity import to_dict as continuity_to_dict
    import json

    now = datetime.now(UTC)
    # Build exactly the same way as the source
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
            (
                draft.auto_approve.to_dict()
                if isinstance(draft.auto_approve, st.AutoApprove)
                else draft.auto_approve
            ),
            ensure_ascii=False,
        ),
        now.isoformat(),
        now.isoformat(),
        None,
        None,
        2,
    )
    print(f"Manual values count: {len(values)}")
    # Now try to actually call save_contract
    try:
        st.save_contract(conn, draft, "lt-test2", now)
        print("save_contract OK")
    except Exception as e:
        print(f"save_contract ERR: {e}")
    conn.close()
