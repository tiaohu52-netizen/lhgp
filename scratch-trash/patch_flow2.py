import re

content = open("src/lhgp/flow/contract_flow.py", "rb").read().decode("utf-8")
# Find the read_text call (allowing any whitespace between)
m = re.search(
    r'(\s*)try:\n(\s*)source_text = file_path\.read_text\(encoding="utf-8"\)\n(\s*)except \(OSError, UnicodeDecodeError\):\n\s*continue',
    content,
)
print("match:", m is not None)
if m:
    indent = m.group(1) + "    "
    indent_inner = m.group(2) + "    "
    new = (
        m.group(1)
        + "try:\n"
        + indent
        + "# Pre-check size before read_text so a multi-GB file\n"
        + indent
        + "# is refused without ever allocating the buffer.\n"
        + indent
        + "if file_path.stat().st_size > _MAX_SOURCE_BYTES:\n"
        + indent
        + "    continue\n"
        + m.group(2)
        + 'source_text = file_path.read_text(encoding="utf-8")\n'
        + m.group(3)
        + "except (OSError, UnicodeDecodeError):\n"
        + "            continue"
    )
    content = content.replace(m.group(), new, 1)
    open("src/lhgp/flow/contract_flow.py", "w", encoding="utf-8", newline="").write(content)
    print("OK")
