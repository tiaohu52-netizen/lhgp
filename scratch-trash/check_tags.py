import sqlite3

con = sqlite3.connect(":memory:")
con.execute(
    "CREATE TABLE memories(id INTEGER PRIMARY KEY, scope TEXT, kind TEXT, title TEXT, body_md TEXT, tags_json TEXT, source_contract_id TEXT, source_event_id INTEGER, source_actor TEXT, score REAL, created_at TEXT, expires_at TEXT, schema_version INTEGER)"
)
con.execute(
    "INSERT INTO memories VALUES (1, 'project', 'pattern', 't1', 'b1', '[\"a\",\"b\"]', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (2, 'project', 'pattern', 't2', 'b2', 'null', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (3, 'project', 'pattern', 't3', 'b3', 'not-json', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (4, 'project', 'pattern', 't4', 'b4', '[\"topic/foo\"]', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (5, 'project', 'pattern', 't5', 'b5', '[\"topic/foobar\"]', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (6, 'project', 'pattern', 't6', 'b6', NULL, NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (7, 'domain', 'pattern', 't7', 'b7', '[\"topic/foo\"]', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (8, 'domain', 'pattern', 't8', 'b8', '[\"topic/baz\"]', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)
con.execute(
    "INSERT INTO memories VALUES (9, 'global', 'pattern', 't9', 'b9', '[\"x\"]', NULL, NULL, NULL, 0.5, '2026-01-01T00:00:00+00:00', NULL, 4)"
)

print("=== Test 1: refactored query with domain=foo ===")
for row in con.execute(
    "SELECT * FROM memories WHERE (scope = 'global' OR scope = 'project' OR (scope = 'domain' AND EXISTS (SELECT 1 FROM json_each(tags_json) WHERE json_each.value = ?))) AND (expires_at IS NULL OR expires_at > ?) ORDER BY score DESC, created_at DESC LIMIT ?",
    ("topic/foo", "2030-01-01T00:00:00+00:00", 20),
):
    print("row:", row[0], row[1], row[5])

print()
print("=== Test 2: domain=None ===")
for row in con.execute(
    "SELECT * FROM memories WHERE (scope = 'global' OR scope = 'project') AND (expires_at IS NULL OR expires_at > ?) ORDER BY score DESC, created_at DESC LIMIT ?",
    ("2030-01-01T00:00:00+00:00", 20),
):
    print("row:", row[0], row[1], row[5])

print()
print("=== Test 3: domain=foo with NULL tags_json ===")
try:
    for row in con.execute(
        "SELECT * FROM memories WHERE (scope = 'global' OR scope = 'project' OR (scope = 'domain' AND EXISTS (SELECT 1 FROM json_each(tags_json) WHERE json_each.value = ?))) AND (expires_at IS NULL OR expires_at > ?) ORDER BY score DESC, created_at DESC LIMIT ?",
        ("topic/foo", "2030-01-01T00:00:00+00:00", 20),
    ):
        print("row:", row[0], row[1], row[5])
    print("OK - did not raise")
except Exception as e:
    print("RAISED:", type(e).__name__, e)

con.close()
