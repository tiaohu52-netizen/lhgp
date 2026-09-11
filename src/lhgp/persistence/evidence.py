"""Evidence persistence (SPEC §13.1).

验收证据此前只落在 verifier 终态事件的 ``payload_json`` 里——能查，但要
解析 JSON 才能回答"合同 X 哪些 check 通过了"，撑不住 SPEC §13.1 自己要求
的 ``verification_history`` API。本模块把每条 check 的产出落成独立行，
写路径在 :mod:`longtask.cli.runner` verifier 落库的同一事务内。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One check outcome from one verification attempt."""

    contract_id: str
    attempt_id: str
    contract_revision: int
    check_id: str
    outcome: str
    source: str
    is_deterministic: bool
    model_outcome: str | None = None
    details: str | None = None
    check_spec_hash: str | None = None


def record_evidence(
    conn: sqlite3.Connection,
    rows: list[EvidenceRow],
    *,
    now: datetime,
    schema_version: int = 1,
) -> None:
    """把一批 evidence 行写入表。由 runner 在 verifier 落库的同一事务调用。"""
    if not rows:
        return
    now_iso = now.isoformat()
    conn.executemany(
        """
        INSERT INTO evidence (
            contract_id, attempt_id, contract_revision, check_id, outcome,
            source, is_deterministic, model_outcome, details, check_spec_hash,
            recorded_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.contract_id,
                row.attempt_id,
                row.contract_revision,
                row.check_id,
                row.outcome,
                row.source,
                1 if row.is_deterministic else 0,
                row.model_outcome,
                row.details,
                row.check_spec_hash,
                now_iso,
                schema_version,
            )
            for row in rows
        ],
    )


def get_evidence_for_contract(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按时间倒序返回某合同的验收证据（SPEC §13.1 verification_history 口径）。"""
    rows = conn.execute(
        """
        SELECT evidence_id, attempt_id, contract_revision, check_id, outcome,
               source, is_deterministic, model_outcome, details, check_spec_hash,
               recorded_at
        FROM evidence
        WHERE contract_id = ?
        ORDER BY recorded_at DESC, evidence_id DESC
        LIMIT ?
        """,
        (contract_id, limit),
    ).fetchall()
    return [
        {
            "evidence_id": r[0],
            "attempt_id": r[1],
            "contract_revision": r[2],
            "check_id": r[3],
            "outcome": r[4],
            "source": r[5],
            "is_deterministic": bool(r[6]),
            "model_outcome": r[7],
            "details": r[8],
            "check_spec_hash": r[9],
            "recorded_at": r[10],
        }
        for r in rows
    ]


__all__ = ["EvidenceRow", "get_evidence_for_contract", "record_evidence"]
