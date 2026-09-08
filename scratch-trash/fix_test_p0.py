"""Update test_context_p0_fixes.py for the 3-tuple return."""

import re
from pathlib import Path

p = Path("tests/unit/test_context_p0_fixes.py")
content = p.read_text(encoding="utf-8")

# active, _ = compile_context_snapshot(...) -> active, _, _consumed = ...
content = re.sub(
    r"^(\s*)active,\s*_\s*=\s*compile_context_snapshot\(",
    r"\1active, _, _consumed = compile_context_snapshot(",
    content,
    flags=re.MULTILINE,
)
# compile_context_snapshot(...) on its own (used as a statement, not unpack) → tuple-discard
content = re.sub(
    r"^(\s*)compile_context_snapshot\(",
    r"\1_ = compile_context_snapshot(",
    content,
    flags=re.MULTILINE,
)
# multi-line compile_context_snapshot( that started on its own line (call spread across lines)
# has already been touched by the second regex, so the next line should be the same indent + "data_dir"
# but the call close is on a later line. The 3-tuple result is now used as a whole expression
# statement, but Python's `_ =` doesn't allow expression lists spread over multiple lines without parens.
# For multi-line calls we wrap with `(active, _, _consumed) =`.
# We handle by looking for the pattern: line that starts with optional whitespace + "_ = compile_context_snapshot("
# followed by lines until the matching ")".
# Simpler heuristic: find any line with "data_dir" right after the `_ = compile_context_snapshot(`,
# and convert to tuple-unpack form.

# Actually, the simpler fix: wrap the result with a tuple unpack.
# For multi-line calls we change `_ = compile_context_snapshot(` to `active, _, _consumed = compile_context_snapshot(`.

# The 1st regex already converted inline `active, _ = compile_context_snapshot(...)` correctly.
# The 2nd regex turned bare `compile_context_snapshot(...)` into `_ = compile_context_snapshot(...)`.
# For multi-line bare calls (spread over lines), this won't compile because Python treats the
# wrapped lines as continuation. We need to fix those.
# Convert `_ = compile_context_snapshot(` (potentially with line continuations) into
# `active, _, _consumed = compile_context_snapshot(`.
content = re.sub(
    r"^(\s*)_ = compile_context_snapshot\(",
    r"\1active, _, _consumed = compile_context_snapshot(",
    content,
    flags=re.MULTILINE,
)
p.write_text(content, encoding="utf-8")
print("updated")
