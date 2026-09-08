import re
import pathlib
p = pathlib.Path("tests/unit/test_plan_gate.py")
text = p.read_text(encoding="utf-8")
# Pattern: _has_recent_plan_approval(conn, "id", N, [...], NOW) → add None before NOW
# Use a regex that handles both single-line and multi-line forms
# Single-line:
new_text = re.sub(
    r"_has_recent_plan_approval\(conn,\s*(\"[^\"]+\"),\s*(\d+),\s*(\[[^\]]+\]),\s*NOW\)",
    r"_has_recent_plan_approval(conn, \1, \2, \3, None, NOW)",
    text,
)
# Multi-line: _has_recent_plan_approval(\n conn,\n "id",\n N,\n [...],\n NOW,\n)
# Pad: insert None, before NOW, but only in calls
new_text = re.sub(
    r"(_has_recent_plan_approval\(\s*conn,\s*\"[^\"]+\",\s*\d+,\s*\[[^\]]*\],)\s*NOW,",
    r"\1 None,\n                NOW,",
    new_text,
)
if new_text != text:
    p.write_text(new_text, encoding="utf-8")
    print("updated")
else:
    print("no change")
