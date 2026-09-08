"""Count actual values via ast - explicitly target save_contract's INSERT tuple."""
import ast
import sys
sys.path.insert(0, ".")
import longtask.persistence.store as s
import inspect
src = inspect.getsource(s.save_contract)
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Tuple):
        if node.lineno == 85:
            print(f"Tuple at line {node.lineno}, {len(node.elts)} elements")
            for i, e in enumerate(node.elts, 1):
                u = ast.unparse(e)
                print(f"  {i}: {u[:80]}")
