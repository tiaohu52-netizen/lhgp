"""Count items in save_contract values."""
import sys
sys.path.insert(0, ".")
import longtask.persistence.store as s
import inspect
src = inspect.getsource(s.save_contract)
values_section = src.split('""",\n            (', 1)[1]
print("json.dumps calls in values:", values_section.count("json.dumps("))
# Count top-level commas
depth = 0
count = 1
for c in values_section:
    if c == "(":
        depth += 1
    elif c == ")":
        depth -= 1
    elif c == "," and depth == 0:
        count += 1
    if depth == 0 and c == ")":
        break
print("Top-level items in values tuple:", count)
