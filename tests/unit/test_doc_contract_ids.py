"""Ensure copy-paste contract IDs in user-facing docs satisfy the CLI contract."""

from __future__ import annotations

import re
from pathlib import Path

_CONTRACT_ID = re.compile(r"^lt-[0-9]{8}-[0-9a-zA-Z_-]+$")
_ID_TOKEN = re.compile(r"\blt-[0-9A-Za-z_-]+\b")


def test_user_facing_examples_use_valid_contract_ids() -> None:
    root = Path(__file__).resolve().parents[2]
    documents = (
        root / "README.md",
        root / "README.zh-CN.md",
        root / "skills" / "longtask-contract" / "SKILL.md",
    )
    for document in documents:
        ids = _ID_TOKEN.findall(document.read_text(encoding="utf-8"))
        assert ids, f"no contract ID examples found in {document}"
        assert all(_CONTRACT_ID.fullmatch(value) for value in ids), document
