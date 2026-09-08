import sys

sys.path.insert(0, "src")
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.feedback.lessons import mine_lesson_if_due
from lhgp.memory.store import list_memories
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event
from lhgp.persistence.store import StoreConfig, connect, ensure_schema

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
db = Path("scratch-trash/state_test2.db")
with contextlib.suppress(FileNotFoundError):
    db.unlink()
conn = connect(StoreConfig(db_path=db))
ensure_schema(conn)

CID = "lt-arrival"

# First batch: 3 fails -> mine lesson
for i in range(3):
    append_event(
        conn,
        contract_id=CID,
        event_type=EventType.ATTEMPT_FAILED,
        payload={"i": i},
        now=NOW + timedelta(seconds=i),
    )
    conn.commit()
mid1 = mine_lesson_if_due(conn, CID, min_failures=3)
print("First batch lesson:", mid1)

# Second call (no new fails) -> None
mid2 = mine_lesson_if_due(conn, CID, min_failures=3)
print("Same cluster (no new fails):", mid2)

# Add 2 more fails -> still under threshold (2 < 3)
for i in range(2):
    append_event(
        conn,
        contract_id=CID,
        event_type=EventType.ATTEMPT_FAILED,
        payload={"i": i},
        now=NOW + timedelta(seconds=10 + i),
    )
    conn.commit()
mid3 = mine_lesson_if_due(conn, CID, min_failures=3)
print("After 2 more fails (5 total, but only 2 since last lesson):", mid3)
# At this point, since the boundary is the last LESSON event, fails count is 2, below threshold

# 1 more fail -> 3 since last lesson
append_event(
    conn,
    contract_id=CID,
    event_type=EventType.ATTEMPT_FAILED,
    payload={"i": 99},
    now=NOW + timedelta(seconds=20),
)
conn.commit()
mid4 = mine_lesson_if_due(conn, CID, min_failures=3)
print("After 3rd new fail (6 total, 3 since last lesson):", mid4)

lessons = [m for m in list_memories(conn, include_expired=True) if "source/lesson" in m.tags]
print("Total lessons:", len(lessons))

conn.close()
db.unlink()
