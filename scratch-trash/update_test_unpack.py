import re
import pathlib
files = [
    "tests/unit/test_agent_messaging.py",
    "tests/unit/test_context.py",
    "tests/unit/test_context_p0_fixes.py",
]
patterns = [
    # 3-tuple unpack → 4-tuple
    (r"(\b\w+,\s*\w+,\s*_\w*)\s*=\s*compile_context_snapshot", r"\1, _consumed_ids = compile_context_snapshot"),
    (r"(\b\w+,\s*_,\s*_consumed)\s*=\s*compile_context_snapshot", r"\1, _consumed_ids = compile_context_snapshot"),
    (r"(\b\w+,\s*_,\s*_consumed)\s*=\s*compile_context_snapshot", r"\1, _consumed_ids = compile_context_snapshot"),
    (r"(\b\w+,\s*scratch,\s*consumed_\w+)\s*=\s*compile_context_snapshot", r"\1, consumed_ids = compile_context_snapshot"),
    (r"(\b\w+,\s*scratch,\s*_consumed)\s*=\s*compile_context_snapshot", r"\1, _consumed_ids = compile_context_snapshot"),
]
for f in files:
    p = pathlib.Path(f)
    text = p.read_text(encoding="utf-8")
    new_text = text
    for pat, repl in patterns:
        new_text = re.sub(pat, repl, new_text)
    if new_text != text:
        p.write_text(new_text, encoding="utf-8")
        print("updated:", f)
    else:
        print("no change:", f)
