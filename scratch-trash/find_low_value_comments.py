"""Scan new source files for low-value comments.

Heuristics:
- A comment that restates the next line's code (e.g. "# close conn" before
  conn.close()).
- A comment that just narrates the obvious (e.g. "# loop over rows"
  before "for row in rows:").
- A single-line comment that doesn't add information beyond the code.
- A comment that just labels the next section without context (e.g. "# now").
"""

import re

paths = [
    "src/lhgp/feedback/lessons.py",
    "src/lhgp/flow/contract_flow.py",
    "src/lhgp/flow/cli.py",
    "src/lhgp/wiki/sync.py",
    "src/lhgp/wiki/__init__.py",
    "src/lhgp/memory/store.py",
    "src/lhgp/memory/index.py",
    "src/lhgp/feedback/store.py",
]

# Patterns that are likely low-value
LOW_VALUE_PATTERNS = [
    r"^\s*#\s*[Aa]ssert( that)?\b",
    r"^\s*#\s*[Ll]oop (over|through)\b",
    r"^\s*#\s*[Cc]lose (the )?(conn|file|handle)\b",
    r"^\s*#\s*#\s*$",  # banner separators
    r"^\s*#\s*[Nn]ow (we |do |just )?",
    r"^\s*#\s*[Tt]hen\b",
    r"^\s*#\s*[Ss]et up\b",
    r"^\s*#\s*[Cc]leanup\b",
    r"^\s*#\s*[Bb]uild (the |a )?(path|list|sql|query)\b",
    r"^\s*#\s*[Cc]reate (the |a )?(conn|table|file)\b",
    r"^\s*#\s*[Rr]eturn\b",
    r"^\s*#\s*[Mm]ain (entry|loop|flow)\b",
    r"^\s*#\s*[Ll]oad\b",
    r"^\s*#\s*[Pp]arse\b",
    r"^\s*#\s*Get (the |a )?\w+",  # "# Get the value"
    r"^\s*#\s*Use \w+",  # "# Use list_memories"
]

for path in paths:
    try:
        src = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        continue
    lines = src.splitlines()
    in_docstring = False
    quote = None
    for i, line in enumerate(lines, 1):
        # Track triple-quoted string boundaries (naive).
        stripped = line.strip()
        if not in_docstring and ('"""' in stripped or "'''" in stripped):
            count = stripped.count('"""') if '"""' in stripped else stripped.count("'''")
            if count == 1:
                in_docstring = True
                quote = '"""' if '"""' in stripped else "'''"
            continue
        if in_docstring:
            if quote in stripped:
                in_docstring = False
            continue
        # Only look at line-leading comments.
        m = re.match(r"^(\s*)#\s?(.*)$", line)
        if not m:
            continue
        comment = m.group(2).strip()
        if not comment:
            continue
        for pat in LOW_VALUE_PATTERNS:
            if re.match(pat, line):
                print(f"{path}:{i}: {comment[:100]}")
                break
