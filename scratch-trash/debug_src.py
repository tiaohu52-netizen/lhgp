"""Print save_contract source."""
import sys
sys.path.insert(0, ".")
import longtask.persistence.store as s
import inspect
src = inspect.getsource(s.save_contract)
lines = src.splitlines()
print("total lines:", len(lines))
for i, l in enumerate(lines):
    if 100 < i < 184:
        print(f"{i+1}: {l}")
