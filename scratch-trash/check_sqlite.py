import sqlite3

print("sqlite version:", sqlite3.sqlite_version)
con = sqlite3.connect(":memory:")
try:
    rows = con.execute("SELECT value FROM json_each(?)", ('["a","b"]',)).fetchall()
    print("json_each ok:", rows)
except Exception as e:
    print("json_each error:", e)
finally:
    con.close()
