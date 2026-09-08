"""Print actual lines of the INSERT VALUES tuple from store.py."""
import re
from pathlib import Path

content = Path("src/longtask/persistence/store.py").read_text(encoding="utf-8")
# Find the INSERT INTO contracts block
start = content.find("INSERT INTO contracts (\n")
# Find the end (next )
end = content.find("\n        )\n", start)
block = content[start:end]
print(block)
