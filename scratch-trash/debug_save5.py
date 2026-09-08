"""Print actual lines of the INSERT VALUES tuple from store.py."""
import re
from pathlib import Path

content = Path("src/longtask/persistence/store.py").read_text(encoding="utf-8")
# Find the INSERT INTO contracts block
start = content.find('INSERT INTO contracts (\n')
# Find the second tuple (the values)
# Look for the end of the values tuple
end_marker = ")\n        )\n"
end = content.find(end_marker, start)
block = content[start:end]
# Get the values tuple (after the SQL string closing """ )
values_section = block.split('""",\n            (', 1)[1]

# Count items at depth 0
depth = 0
items = [""]
for c in values_section:
    if c == "(":
        depth += 1
        items[-1] += c
    elif c == ")":
        depth -= 1
        if depth == 0:
            break
        items[-1] += c
    elif c == "," and depth == 0:
        items.append("")
    else:
        items[-1] += c

print(f"items count: {len(items)}")
for idx, item in enumerate(items, 1):
    print(f"  {idx}: {item[:60].strip()}")
