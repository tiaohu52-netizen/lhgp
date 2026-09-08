"""Update tests for the new directive section header."""

from pathlib import Path

OLD = "## ⚡ 用户指令（必须遵守）"
NEW = "## ⚡ 收到的指令（必须遵守）"

for f in [
    "tests/unit/test_context_p0_fixes.py",
    "tests/unit/test_context.py",
    "tests/unit/test_agent_messaging.py",
    "tests/integration/test_e2e_p1_review_loop.py",
]:
    p = Path(f)
    if not p.exists():
        continue
    content = p.read_text(encoding="utf-8")
    if OLD in content:
        content = content.replace(OLD, NEW)
        p.write_text(content, encoding="utf-8")
        print(f"updated {f}")
