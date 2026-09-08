import sys

sys.path.insert(0, "src")
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.feedback.lessons import mine_lesson_if_due
from lhgp.feedback.store import record_evaluation
from lhgp.feedback.types import EvaluationRating, EvaluationVerdict, UserEvaluation
from lhgp.memory.store import list_memories
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event
from lhgp.persistence.store import StoreConfig, connect, ensure_schema

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
db = Path("scratch-trash/state_test.db")
with contextlib.suppress(FileNotFoundError):
    db.unlink()
conn = connect(StoreConfig(db_path=db))
ensure_schema(conn)

CID = "lt-mixed-1"

# 2 fails
for i in range(2):
    append_event(
        conn,
        contract_id=CID,
        event_type=EventType.ATTEMPT_FAILED,
        payload={"i": i},
        now=NOW + timedelta(seconds=i),
    )
    conn.commit()

# 1 ACCEPT (verdict=ACCEPT)
record_evaluation(
    conn,
    UserEvaluation(
        contract_id=CID,
        contract_revision=1,
        evaluator="u",
        rating=EvaluationRating.GOOD,
        verdict=EvaluationVerdict.ACCEPT,
        comments="now ok",
        created_at=NOW + timedelta(seconds=2),
    ),
)

# 2 more fails
for i in range(2):
    append_event(
        conn,
        contract_id=CID,
        event_type=EventType.ATTEMPT_FAILED,
        payload={"i": i},
        now=NOW + timedelta(seconds=3 + i),
    )
    conn.commit()

print("Test: 2 fails + 1 ACCEPT + 2 fails (4 fails total):")
mid = mine_lesson_if_due(conn, CID, min_failures=3)
print("  lesson mined:", mid)
mid2 = mine_lesson_if_due(conn, CID, min_failures=3)
print("  second call (same cluster):", mid2)
lessons = [m for m in list_memories(conn, include_expired=True) if "source/lesson" in m.tags]
print("  total lessons:", len(lessons))

conn.close()
db.unlink()
