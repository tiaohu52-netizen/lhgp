import os, sys, sqlite3, datetime
p1 = r'src\longtask\persistence\__pycache__\store.cpython-313.pyc'
if os.path.exists(p1):
    os.remove(p1)
sys.path.insert(0, 'src')
from lhgp.contracts import ContractDraft, Acceptance, Budget
from longtask.persistence.schema import ensure_schema
from longtask.persistence import store as s
conn = sqlite3.connect(':memory:')
ensure_schema(conn)
now = datetime.datetime.now(datetime.timezone.utc)
draft = ContractDraft(
    title='x', objective='o',
    deadline_at=now + datetime.timedelta(hours=1),
    hard_constraints={}, acceptance=Acceptance(standard='s', checks=('c1',)),
    workload_initial_hours=1.0,
    budget=Budget(5, 1, 1, 30, 1048576, 2),
)

# Patch the function so we can intercept the SQL and params
import inspect
src_text = inspect.getsource(s.save_contract)
# Find the SQL inside the multi-line string
# Strategy: split at INSERT INTO contracts, grab until VALUES
idx = src_text.find('INSERT INTO contracts')
sql_block = src_text[idx:idx+1200]
# Find the triple-quoted SQL
qstart = sql_block.find('"""') + 3
qend = sql_block.find('"""', qstart)
sql = sql_block[qstart:qend]
print('=== SQL ===')
print(sql)
print('=== ? count:', sql.count('?'))
print('=== VALUES clause ===')
vstart = src_text.find('VALUES (', idx)
vend = src_text.find(')', vstart + 8)
# That's just the marker. Find the actual values list right after.
vstart2 = src_text.find('            (', idx)
print(vstart2)
