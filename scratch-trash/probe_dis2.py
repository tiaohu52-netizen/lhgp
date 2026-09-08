import os, sys, dis
p1 = r'src\longtask\persistence\__pycache__\store.cpython-313.pyc'
if os.path.exists(p1):
    os.remove(p1)
sys.path.insert(0, 'src')
from longtask.persistence import store as s

# Disassemble save_contract fully, looking at all inner code objects
def deep_dis(co, depth=0):
    indent = '  ' * depth
    print(f'{indent}== {co.co_name} (firstline {co.co_firstlineno}) ==')
    instructions = list(dis.get_instructions(co))
    # Find all BUILD_TUPLE and BUILD_LIST instructions
    for instr in instructions:
        if instr.opname in ('BUILD_TUPLE', 'BUILD_LIST', 'BUILD_SET'):
            print(f'{indent}  {instr.opname} {instr.arg} at offset {instr.offset} (line {instr.starts_line})')
        if instr.opname == 'CALL':
            print(f'{indent}  CALL at offset {instr.offset} (line {instr.starts_line})')
    for c in co.co_consts:
        if hasattr(c, 'co_code'):
            deep_dis(c, depth+1)

deep_dis(s.save_contract.__code__)
