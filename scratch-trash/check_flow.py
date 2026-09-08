import sys

sys.path.insert(0, "src")
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import ContractState
from lhgp.flow.contract_flow import (
    _resolve_by_substring,
    walk_contract,
)
from lhgp.persistence.store import StoreConfig, connect, ensure_schema, save_contract

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
import tempfile

tmpdir = Path(tempfile.mkdtemp())
src_root = tmpdir / "src"
src_root.mkdir(parents=True)
db = tmpdir / "state.db"
conn = connect(StoreConfig(db_path=db))
ensure_schema(conn)

# Test 1: seed is a Python import path
draft = ContractDraft(
    title="import path",
    objective="o",
    deadline_at=NOW + timedelta(hours=1),
    hard_constraints={},
    acceptance=Acceptance(standard="import lhgp.flow.contract_flow", checks=("must use this",)),
    workload_initial_hours=1.0,
    budget=Budget(
        max_dispatches=3,
        max_escalations=1,
        max_concurrent_attempts=1,
        max_attempt_minutes=30,
        max_output_bytes=1024,
    ),
    context={},
    execution={},
)
save_contract(conn, draft=draft, contract_id="lt-import", now=NOW, state=ContractState.ACTIVE)
flow = walk_contract(conn, "lt-import", src_root=Path("D:/工作台/远期任务协议/src"))
print("Test 1: import path seed")
print("  nodes count:", len(flow.nodes))
ids = sorted([n.id for n in flow.nodes])
print("  node ids:", ids[:5], "..." if len(ids) > 5 else "")
print("  source:", flow.source)

# Test 2: substring false-positive

# Create src with common seed "is"
(test_src := tmpdir / "test_src").mkdir(exist_ok=True)
(test_src / "iso_handler.py").write_text("def iso_handler(): return 1\n", encoding="utf-8")
(test_src / "distance_calc.py").write_text("def distance_calc(): return 1\n", encoding="utf-8")
(test_src / "fizzle.py").write_text("def fizzle(): return 1\n", encoding="utf-8")
(test_src / "ignored.py").write_text("# nothing here\n", encoding="utf-8")
matches = _resolve_by_substring(test_src, "is")
print()
print("Test 2: substring ''is'' false positives")
print("  matches:", [m.name for m in matches])

# Test 3: empty contract (no standard, no checks, no execution, no context)
draft3 = ContractDraft(
    title="empty",
    objective="o",
    deadline_at=NOW + timedelta(hours=1),
    hard_constraints={},
    acceptance=Acceptance(standard="", checks=()),
    workload_initial_hours=1.0,
    budget=Budget(
        max_dispatches=3,
        max_escalations=1,
        max_concurrent_attempts=1,
        max_attempt_minutes=30,
        max_output_bytes=1024,
    ),
    context={},
    execution={},
)
save_contract(conn, draft=draft3, contract_id="lt-empty2", now=NOW, state=ContractState.ACTIVE)
flow3 = walk_contract(conn, "lt-empty2", src_root=test_src)
print()
print("Test 3: empty contract (no seeds)")
print("  nodes count:", len(flow3.nodes))
print("  source:", flow3.source)

conn.close()
