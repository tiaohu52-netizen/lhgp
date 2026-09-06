"""P6: aggregate all contracts into a portfolio view."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from lhgp.portfolio.types import ContractSummary, PortfolioSnapshot


def portfolio_summary(
    conn: sqlite3.Connection,
    *,
    include_terminal: bool = True,
    limit: int = 500,
) -> PortfolioSnapshot:
    """Aggregate all contracts in a single read.

    Joins the latest user_evaluations row per contract (LEFT JOIN) so the
    dashboard can show the user's last verdict alongside the machine
    state. Counters are computed in Python to keep this a single SELECT.
    """
    clauses: list[str] = []
    params: list[Any] = [int(limit)]
    if not include_terminal:
        clauses.append("c.state IN ('active', 'paused', 'blocked', 'drafted')")
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT c.contract_id, c.title, c.state, c.deadline_status, "  # noqa: S608
        "c.acceptance_status, c.deadline_at, c.next_decision_at, "
        "c.revision, c.goal_id "
        "FROM contracts c "
        f"{where_sql} "
        "ORDER BY c.updated_at DESC, c.contract_id ASC "
        "LIMIT ?"
    )
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return PortfolioSnapshot(
            contracts=(),
            by_state={},
            by_deadline={},
            by_acceptance={},
            generated_at=datetime.now(UTC).isoformat(),
        )

    contract_ids = [r[0] for r in rows]
    latest_evals: dict[str, tuple[int, str]] = {}
    eval_placeholders = ",".join("?" for _ in contract_ids)
    ev_rows = conn.execute(
        f"""
        SELECT contract_id, rating, verdict
        FROM user_evaluations
        WHERE evaluation_id IN (
            SELECT MAX(evaluation_id) FROM user_evaluations
            WHERE contract_id IN ({eval_placeholders})
            GROUP BY contract_id
        )
        """,  # noqa: S608
        contract_ids,
    ).fetchall()
    for cid, rating, verdict in ev_rows:
        latest_evals[cid] = (int(rating), str(verdict))

    summaries = [
        ContractSummary(
            contract_id=r[0],
            title=r[1] or "",
            state=r[2] or "",
            deadline_status=r[3] or "",
            acceptance_status=r[4] or "",
            deadline_at=r[5],
            next_decision_at=r[6],
            revision=int(r[7] or 1),
            goal_id=r[8],
            last_user_rating=latest_evals.get(r[0], (None, None))[0],
            last_user_verdict=latest_evals.get(r[0], (None, None))[1],
        )
        for r in rows
    ]
    return PortfolioSnapshot(
        contracts=tuple(summaries),
        by_state=dict(Counter(s.state for s in summaries)),
        by_deadline=dict(Counter(s.deadline_status for s in summaries)),
        by_acceptance=dict(Counter(s.acceptance_status for s in summaries)),
        generated_at=datetime.now(UTC).isoformat(),
    )
