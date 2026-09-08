"""Quick script to update test_context.py callers."""

from pathlib import Path

p = Path("tests/unit/test_context.py")
content = p.read_text(encoding="utf-8")

replacements = [
    (
        'active, scratch = compile_context_snapshot(root, conn, contract, "att-1", NOW)',
        'active, scratch, _consumed = compile_context_snapshot(root, conn, contract, "att-1", NOW)',
    ),
    (
        'active, _ = compile_context_snapshot(root, conn, contract, "att-2", NOW)',
        'active, _, _consumed = compile_context_snapshot(root, conn, contract, "att-2", NOW)',
    ),
    (
        'active, _ = compile_context_snapshot(root, conn, get_contract(conn, cid), "att-1", NOW)',
        'active, _, _consumed = compile_context_snapshot(root, conn, get_contract(conn, cid), "att-1", NOW)',
    ),
    (
        'compile_context_snapshot(root, conn, contract, "att-1", NOW)',
        'compile_context_snapshot(root, conn, contract, "att-1", NOW)[0:2]',
    ),
    (
        'input_ = build_attempt_input(root, conn, contract, "att-3", NOW)',
        'input_, _consumed = build_attempt_input(root, conn, contract, "att-3", NOW)',
    ),
]
for old, new in replacements:
    content = content.replace(old, new)
p.write_text(content, encoding="utf-8")
print("updated")
