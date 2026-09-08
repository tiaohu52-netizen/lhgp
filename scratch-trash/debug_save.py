"""Debug: actually count tuple values in save_contract."""
import sys
sys.path.insert(0, ".")
import ast
import inspect
from longtask.persistence import store
src = inspect.getsource(store.save_contract)
# Find the second tuple (the values tuple, after the SQL string)
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Tuple) and len(node.elts) >= 25:
        print(f"Tuple at line {node.lineno}, {len(node.elts)} elements")
