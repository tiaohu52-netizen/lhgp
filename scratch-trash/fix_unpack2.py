"""Update remaining test files for tuple return."""

import re
from pathlib import Path

for f in [
    "tests/unit/test_agent_messaging.py",
    "tests/integration/test_contract_visibility.py",
    "tests/integration/test_p5_repair_loop.py",
]:
    p = Path(f)
    content = p.read_text(encoding="utf-8")
    # input_ = build_attempt_input(...) → input_, _ = build_attempt_input(...)
    content = re.sub(
        r"^(\s*)input_\s*=\s*build_attempt_input\(",
        r"\1input_, _ = build_attempt_input(",
        content,
        flags=re.MULTILINE,
    )
    p.write_text(content, encoding="utf-8")
    print(f"updated {f}")
