"""Count items in save_contract values - robust."""
import sys
sys.path.insert(0, ".")
import ast
import inspect
import longtask.persistence.store as s
src = inspect.getsource(s.save_contract)
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Tuple) and len(node.elts) >= 20:
        print(f"Tuple at line {node.lineno}, {len(node.elts)} elements")
        for i, e in enumerate(node.elts, 1):
            print(f"  {i}: {ast.unparse(e)[:80]}")
