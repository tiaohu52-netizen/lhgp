import sqlite3

con = sqlite3.connect(":memory:")
con.row_factory = sqlite3.Row
con.execute("""
CREATE TABLE events (
    event_id INTEGER PRIMARY KEY,
    contract_id TEXT,
    actor TEXT
)
""")
# Truncation test
actor = "auto-mine:eve-with-a-very-long-name-that-exceeds-sixty-four-chars-by-some"
print("len:", len(actor))
trunc = actor[:64]
print("trunc len:", len(trunc))
print("trunc:", trunc)
con.close()
