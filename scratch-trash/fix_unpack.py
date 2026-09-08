"""Update remaining test files for 3-tuple return."""

import re
from pathlib import Path

for f in [
    "tests/unit/test_agent_messaging.py",
    "tests/integration/test_contract_visibility.py",
    "tests/integration/test_p5_repair_loop.py",
]:
    p = Path(f)
    content = p.read_text(encoding="utf-8")
    # _, scratch = compile_context_snapshot(...)  →  _, scratch, _consumed = ...
    content = re.sub(
        r"^(\s*)_,\s*scratch\s*=\s*compile_context_snapshot\(",
        r"\1_, scratch, _consumed = compile_context_snapshot(",
        content,
        flags=re.MULTILINE,
    )
    # _, _ = compile_context_snapshot(...) → _, _, _consumed = ...
    content = re.sub(
        r"^(\s*)_,\s*_\s*=\s*compile_context_snapshot\(",
        r"\1_, _, _consumed = compile_context_snapshot(",
        content,
        flags=re.MULTILINE,
    )
    # active, _ = compile_context_snapshot(...) → active, _, _consumed = ...
    content = re.sub(
        r"^(\s*)active,\s*_\s*=\s*compile_context_snapshot\(",
        r"\1active, _, _consumed = compile_context_snapshot(",
        content,
        flags=re.MULTILINE,
    )
    p.write_text(content, encoding="utf-8")
    print(f"updated {f}")
