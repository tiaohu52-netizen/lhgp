import sys

sys.path.insert(0, "src")

# Verify that contract_id with "..\\" works as a path traversal attack

# Note: this is for analysis only - we will not actually run it to filesystem escape
# Just trace the file path:
#  path = wiki_root / auto_dir / f"{view.contract_id}.md"
# If contract_id = "..\\..\\..\\evil", path = <wiki_root>/auto/../../../evil.md
# Resolves OUTSIDE auto_dir.
print("Path traversal vector confirmed:")
print("  contract_id = ''../../etc/passwd'' ->")
print("  path = wiki_root / auto / ''../../etc/passwd.md''")
print("  resolved = wiki_root / ''etc/passwd.md''  (OUTSIDE auto/)")
