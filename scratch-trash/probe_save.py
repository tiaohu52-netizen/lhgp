import os, sys, sqlite3, datetime, json
p1 = r'src\longtask\persistence\__pycache__\store.cpython-313.pyc'
if os.path.exists(p1):
    os.remove(p1)
p2 = r'src\longtask\persistence\__pycache__\schema.cpython-313.pyc'
if os.path.exists(p2):
    os.remove(p2)
sys.path.insert(0, 'src')
from lhgp.contracts import ContractDraft, Acceptance, Budget
from longtask.persistence.schema import ensure_schema
from longtask.persistence import store as s
conn = sqlite3.connect(':memory:')
ensure_schema(conn)
cur = conn.execute("PRAGMA table_info(contracts)")
cols = cur.fetchall()
print('schema columns:', len(cols))
for c in cols:
    print(' ', c[1])
now = datetime.datetime.now(datetime.timezone.utc)
draft = ContractDraft(
    title='x', objective='o',
    deadline_at=now + datetime.timedelta(hours=1),
    hard_constraints={}, acceptance=Acceptance(standard='s', checks=('c1',)),
    workload_initial_hours=1.0,
    budget=Budget(5, 1, 1, 30, 1048576, 2),
)
# Now monkey-patch the save_contract function to inspect values
import inspect
src = inspect.getsource(s.save_contract)
print('source length:', len(src))
# Replace save_contract with a tracer
orig_func = s.save_contract
def traced_save_contract(*args, **kwargs):
    print('save_contract called with', len(args), 'args,', len(kwargs), 'kwargs')
    print('  args types:', [type(a).__name__ for a in args])
    print('  kwargs keys:', list(kwargs.keys()))
    # We can't easily intercept the inner conn.execute, so let's just call and see
    return orig_func(*args, **kwargs)
s.save_contract = traced_save_contract
try:
    s.save_contract(conn, draft=draft, contract_id='lt-x', now=now, actor='user')
except Exception as e:
    import traceback
    traceback.print_exc()
