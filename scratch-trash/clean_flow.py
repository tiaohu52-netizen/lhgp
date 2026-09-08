"""Clean contract_flow.py: collapse multi-blank-line artifacts and trim
narrative comments. Also tighten wiki/sync.py and lessons.py."""

import re


def clean(path):
    src = open(path, encoding="utf-8").read()
    src = src.replace("\r\n", "\n").replace("\r", "\n")
    src = re.sub(r"\n{3,}", "\n\n", src)
    open(path, "w", encoding="utf-8", newline="\n").write(src)
    print(f"cleaned: {path}")


for p in [
    "src/lhgp/flow/contract_flow.py",
    "src/lhgp/wiki/sync.py",
    "src/lhgp/feedback/lessons.py",
    "src/lhgp/wiki/__init__.py",
    "src/lhgp/flow/cli.py",
]:
    clean(p)
