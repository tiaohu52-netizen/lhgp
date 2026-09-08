import os, sys, dis
p1 = r'src\longtask\persistence\__pycache__\store.cpython-313.pyc'
if os.path.exists(p1):
    os.remove(p1)
sys.path.insert(0, 'src')
from longtask.persistence import store as s

# Find the bytecode of save_contract and search for BUILD_TUPLE
fn = s.save_contract
print('=== Code object name ===')
code = fn.__code__
print('co_filename:', code.co_filename)
print('co_name:', code.co_name)
print('co_consts count:', len(code.co_consts))
# Walk all code objects
def walk(co, depth=0):
    indent = '  ' * depth
    print(f'{indent}NAME {co.co_name} filename={co.co_filename} firstline={co.co_firstlineno} consts_len={len(co.co_consts)}')
    for const in co.co_consts:
        if hasattr(const, 'co_code'):
            walk(const, depth+1)
walk(code)

# Look for the BUILD_TUPLE / BUILD_LIST / CALL instructions in save_contract's bytecode
# Find inner code object containing the conn.execute INSERT INTO contracts
def find_insert_block(co, parent_name='save_contract'):
    # Check this co's constants for any tuple of length 27
    found = []
    for i, c in enumerate(co.co_consts):
        if isinstance(c, tuple) and len(c) == 27:
            found.append((parent_name + '.' + co.co_name, i, c))
    for c in co.co_consts:
        if hasattr(c, 'co_consts'):
            found.extend(find_insert_block(c, co.co_name))
    return found

tups = find_insert_block(code)
print('27-tuples found:', len(tups))
for name, i, t in tups[:3]:
    print(f'  in {name} const[{i}]: {len(t)} elements')
    for j, e in enumerate(t, 1):
        print(f'    [{j}] {e!r}'[:120])
