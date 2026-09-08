import os
import sys
import tempfile
from pathlib import Path

# Replicate the wiki sync behavior locally
sys.path.insert(0, "src")
from lhgp.wiki.sync import (
    _yaml_list,
)

# Test 1: _yaml_list with empty/single/multi values and quotes
print("Test _yaml_list")
print("  empty:", repr(_yaml_list(())))
print("  single:", repr(_yaml_list(("foo",))))
print("  multi:", repr(_yaml_list(("foo", "bar"))))
print("  with quote:", repr(_yaml_list(('foo "bar"',))))
print("  list not tuple:", repr(_yaml_list(["a", "b"])))

# Test 2: Path traversal via contract_id
print()
print("Test path traversal")
tmpdir = Path(tempfile.mkdtemp())
os.makedirs(tmpdir / "auto", exist_ok=True)
bad = "..\\..\\..\\evil"
target = tmpdir / "auto" / f"{bad}.md"
print("  would write to:", target)
print(
    "  target outside auto?:",
    not str(target.resolve()).startswith(str((tmpdir / "auto").resolve())),
)
