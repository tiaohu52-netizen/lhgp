import sys

sys.path.insert(0, "src")
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Test: contract with title containing " (double quote)
# The standard is a non-issue since its rendered separately. But what about contract title?
# Actually contract_id is the only thing used in the file name. title is in frontmatter.
# But frontmatter title is unquoted. If title contains a colon, YAML might break.
# Let me check: contract draft validation says title must be 1..200 chars
# It does NOT say "no colons". So a title like "fix: edge case" would break the frontmatter.
# Let me trace:
from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import ContractState
from lhgp.persistence.store import StoreConfig, connect, ensure_schema, save_contract
from lhgp.wiki.sync import publish_active_contracts

tmpdir = Path(tempfile.mkdtemp())
db = tmpdir / "state.db"
conn = connect(StoreConfig(db_path=db))
ensure_schema(conn)
NOW = datetime.now(UTC)
draft = ContractDraft(
    title="fix: edge case with colon in title",
    objective="o",
    deadline_at=NOW + timedelta(hours=1),
    hard_constraints={},
    acceptance=Acceptance(standard="s", checks=("c",)),
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
save_contract(conn, draft=draft, contract_id="lt-colon", now=NOW, state=ContractState.ACTIVE)
wiki_root = tmpdir / "wiki"
written = publish_active_contracts(conn, wiki_root)
for p in written:
    text = p.read_text(encoding="utf-8")
    print("=== Page content ===")
    print(text)
    print("=== End ===")
conn.close()
