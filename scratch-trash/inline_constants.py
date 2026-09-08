"""Inline comment + blank-line + constant blocks."""

import re
import sys

for path in sys.argv[1:]:
    src = open(path, encoding="utf-8").read()
    # Collapse a single blank line between a # comment and the next
    # non-blank line. Two newlines (\n + \n) -> one (\n).
    new = re.sub(r"(# [^\n]*)\n\n+(?=\S)", r"\1\n", src)
    if new != src:
        open(path, "w", encoding="utf-8", newline="\n").write(new)
        print(f"cleaned: {path}")
    else:
        print(f"no change: {path}")
