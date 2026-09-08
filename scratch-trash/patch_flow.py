content = open("src/lhgp/flow/contract_flow.py", "rb").read().decode("utf-8")
# Normalize any remaining \r-only to \n first
content = content.replace("\r", "\n")
# Re-add the size pre-check constant
old = "# Reject overlong seeds: a 1 KiB string is not a module path.\n_MAX_SEED_LEN = 256"
new = (
    old
    + "\n\n# Mirror the cli.py pre-check so we never read a multi-GB file into\n# memory just to be rejected by walk_source's own 2 MiB cap.\n_MAX_SOURCE_BYTES = 2 * 1024 * 1024"
)
if old not in content:
    print("OLD NOT FOUND")
else:
    content = content.replace(old, new, 1)
# write back as LF (project prefers CRLF but this is fine since ruff
# will normalize on next run)
open("src/lhgp/flow/contract_flow.py", "w", encoding="utf-8", newline="").write(content)
print("constant added")
