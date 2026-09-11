"""SQLite/WAL 权威状态存储与事务写入（DESIGN §3.1、§7、§11.3、§13.3、§14）。

P1 起 schema 升到 v2（DESIGN §13.3）：
- contracts 表加 goal_id / deadline_status / acceptance_status / next_decision_at 列
- events 表加 contract_revision / role / payload_schema_version 列
- 新建 contract_revisions（不可变修订表，替换就地 CAS UPDATE）
- 新建 attempts（attempt 实体表，§7 attempt 轴、C1 修复依据）
- 新建 decisions（决策实体表，§6 escalation 轴历史）
- 新建 idempotency（请求幂等表，§11.3）

提供合同状态变更、租约 CAS、幂等去重与 fencing 写回的单事务原子写入实现。
所有时间均通过参数显式注入，不依赖系统墙钟。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from typing import Any
from zoneinfo import ZoneInfo

from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.auto_approve import from_dict as auto_approve_from_dict
from lhgp.contracts.budget import DEFAULT_VERIFICATION_RESERVED
from longtask.acceptance.checks import parse_check
from longtask.contracts.acceptance import evidence_binding
from longtask.contracts.attention import from_dict as attention_from_dict
from longtask.contracts.attention import to_dict as attention_to_dict
from longtask.contracts.authority import from_dict as authority_from_dict
from longtask.contracts.authority import to_dict as authority_to_dict
from longtask.contracts.continuity import from_dict as continuity_from_dict
from longtask.contracts.continuity import to_dict as continuity_to_dict
from longtask.contracts.schema import (
    Acceptance,
    AcceptanceStatus,
    BlockReason,
    Budget,
    ContractDraft,
    ContractState,
    ContractView,
    DeadlineStatus,
)
from longtask.persistence.errors import (
    IdempotencyMismatchError,
    LeaseCASError,
    LeaseFencedError,
    RevisionConflictError,
    StoreError,
    StoreTamperedError,
)
from longtask.persistence.events import EventType
from longtask.persistence.events_query import (
    append_event,
    get_events,
    get_events_by_request_id,
)
from longtask.persistence.leases import (
    acquire_lease,
    get_lease,
    reclaim_lease,
    release_lease,
    renew_lease,
)
from longtask.persistence.notifications import enqueue_notification
from longtask.persistence.schema import (
    STORE_SCHEMA_VERSION,
    connect,
    ensure_schema,
    transaction,
)
from longtask.persistence.types import (
    EventInput,
    StoreConfig,
    StoredEvent,
    StoredLease,
    WriteBackResult,
)

# STORE_SCHEMA_VERSION re-export via schema.py；本地常量移除以避免双源。

# 显式 re-export，让 `from longtask.persistence.store import StoreConfig` 走 typecheck
# （拆分自 errors.py / types.py 后必须显式列出，否则 mypy 视为私有再导出失败）。
__all__ = [
    "STORE_SCHEMA_VERSION",
    # types
    "EventInput",
    # errors
    "IdempotencyMismatchError",
    "LeaseCASError",
    "LeaseFencedError",
    "RevisionConflictError",
    "StoreConfig",
    "StoreError",
    "StoreTamperedError",
    "StoredEvent",
    "StoredLease",
    "WriteBackResult",
    # functions（按字母序，函数体下方定义）
    "acceptance_at_revision",
    "acquire_lease",
    "advance_goal",
    "append_event",
    "attempt_evidence_binding",
    "connect",
    "ensure_schema",
    "get_contract",
    "get_events",
    "get_events_by_request_id",
    "get_goal",
    "get_lease",
    "goal_contract_draft",
    "goal_next_action",
    "list_contracts",
    "list_goals",
    "patch_contract",
    "patch_goal",
    "reclaim_lease",
    "release_lease",
    "renew_lease",
    "save_contract",
    "transaction",
    "update_contract_state",
    "write_back",
]


def _serialize_acceptance_checks(checks: Sequence[Any]) -> list[Any]:
    """Project acceptance checks into JSON-writable values.

    A typed check is a ``CheckSpec`` and is not JSON serializable on its own,
    so it must go through ``to_dict()`` before reaching the authoritative
    store.  Legacy free-text checks pass through unchanged.
    """
    return [item.to_dict() if hasattr(item, "to_dict") else item for item in checks]


def _parse_acceptance_checks(values: Sequence[Any]) -> tuple[Any, ...]:
    """Rebuild check values from the store into their in-memory form.

    Inverse of :func:`_serialize_acceptance_checks`: typed checks come back as
    ``CheckSpec`` rather than raw mappings, so every caller sees one shape.
    """
    return tuple(parse_check(item) if isinstance(item, dict) else item for item in values)


def acceptance_at_revision(
    conn: sqlite3.Connection, contract_id: str, revision: int
) -> Acceptance | None:
    """The acceptance as it stood at an immutable revision snapshot (§13.3).

    This answers a different question from ``get_contract(...).draft.acceptance``:
    not "what does the contract require now" but "what was the agent that
    started at this revision asked to do".  ``contract_revisions`` rows are
    write-once and are never pruned, so the answer stays available for the life
    of the database.
    """
    row = conn.execute(
        "SELECT acceptance_json FROM contract_revisions WHERE contract_id = ? AND revision = ?",
        (contract_id, revision),
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        return Acceptance(
            standard=data["standard"],
            checks=_parse_acceptance_checks(data["checks"]),
            verifier=data.get("verifier", "cross_check"),
            spec=data.get("spec"),
            spec_hash=data.get("spec_hash"),
        )
    except (AttributeError, KeyError, TypeError, ValueError):  # pragma: no cover - corrupt row
        return None


def attempt_evidence_binding(
    conn: sqlite3.Connection, contract_id: str | None, contract_revision: int | None
) -> dict[str, Any]:
    """Binding payload for evidence produced by an attempt.

    9th-round P1: the fingerprint used to be read from the *live* contract row
    when the attempt was collected.  Patching the acceptance while a verifier
    was running therefore stamped fresh evidence with the *new* requirement's
    identity -- so ``user_confirm`` matched it, and the contract completed on a
    check that had never seen the edited criterion.  Binding to the revision the
    attempt was admitted at describes what the verifier actually received.

    Returns ``{}`` when the snapshot cannot be resolved.  That is deliberately
    not "fall back to the current acceptance": an event with no identity is
    refused downstream and costs a re-verification, which is the honest result
    when the truth is unknown.
    """
    if contract_id is None or contract_revision is None:
        return {}
    acceptance = acceptance_at_revision(conn, contract_id, contract_revision)
    return evidence_binding(acceptance) if acceptance is not None else {}


def _row_to_contract_view(row: sqlite3.Row | tuple[Any, ...]) -> ContractView:
    """数据库记录转 ContractView（DESIGN §4、§11.6、§7 四轴）。

    P1：从 contracts 表读 goal_id / deadline_status / acceptance_status / next_decision_at，
    四个字段必须非空（迁移已兜底）。
    """
    (
        contract_id,
        goal_id,
        revision,
        state_str,
        deadline_status_str,
        acceptance_status_str,
        blocked_reason_str,
        title,
        objective,
        deadline_at_str,
        hard_constraints_json,
        acceptance_json,
        workload_initial_hours,
        budget_json,
        soft_guidance_json,
        context_json,
        execution_json,
        client_meta_json,
        authority_json,
        attention_json,
        continuity_json,
        auto_approve_json,
        created_at_str,
        updated_at_str,
        next_wakeup_at_str,
        next_decision_at_str,
        _schema_version,
    ) = row

    acceptance_dict = json.loads(acceptance_json)
    acceptance = Acceptance(
        standard=acceptance_dict["standard"],
        checks=_parse_acceptance_checks(acceptance_dict["checks"]),
        verifier=acceptance_dict.get("verifier", "cross_check"),
        spec=acceptance_dict.get("spec"),
        spec_hash=acceptance_dict.get("spec_hash"),
    )
    budget_dict = json.loads(budget_json)
    budget = Budget(
        max_dispatches=budget_dict["max_dispatches"],
        max_escalations=budget_dict["max_escalations"],
        max_concurrent_attempts=budget_dict["max_concurrent_attempts"],
        max_attempt_minutes=budget_dict["max_attempt_minutes"],
        max_output_bytes=budget_dict["max_output_bytes"],
        # P5 验证预算：老库存 JSON 无此字段 → 兜底 2（允许失败后重验）
        verification_attempts_reserved=int(
            budget_dict.get("verification_attempts_reserved", DEFAULT_VERIFICATION_RESERVED)
        ),
        # 消耗台账的成本线（§6.3）：老库存 JSON 无此字段 → None = 不按成本设限
        max_cost=budget_dict.get("max_cost"),
    )
    draft = ContractDraft(
        title=title,
        objective=objective,
        deadline_at=datetime.fromisoformat(deadline_at_str),
        hard_constraints=json.loads(hard_constraints_json),
        acceptance=acceptance,
        workload_initial_hours=float(workload_initial_hours),
        budget=budget,
        soft_guidance=json.loads(soft_guidance_json),
        context=json.loads(context_json),
        execution=json.loads(execution_json),
        client_meta=json.loads(client_meta_json),
        authority=authority_from_dict(json.loads(authority_json)),
        attention=attention_from_dict(json.loads(attention_json)),
        continuity=continuity_from_dict(json.loads(continuity_json)),
        auto_approve=auto_approve_from_dict(json.loads(auto_approve_json or "{}")),
    )

    try:
        deadline_status = DeadlineStatus(deadline_status_str)
    except ValueError:
        # 兼容迁移前写入的 on_track 等旧值；未知值不应让整个守护进程
        # 因一行损坏数据崩溃，按最保守的 not_due 读取并由 doctor 报告。
        deadline_status = DeadlineStatus.NOT_DUE
    return ContractView(
        draft=draft,
        contract_id=contract_id,
        goal_id=goal_id or contract_id,
        revision=int(revision),
        state=ContractState(state_str),
        deadline_status=deadline_status,
        acceptance_status=AcceptanceStatus(acceptance_status_str),
        created_at=datetime.fromisoformat(created_at_str),
        updated_at=datetime.fromisoformat(updated_at_str),
        next_wakeup_at=(datetime.fromisoformat(next_wakeup_at_str) if next_wakeup_at_str else None),
        next_decision_at=(
            datetime.fromisoformat(next_decision_at_str) if next_decision_at_str else None
        ),
        blocked_reason=BlockReason(blocked_reason_str) if blocked_reason_str else None,
    )


def get_contract(conn: sqlite3.Connection, contract_id: str) -> ContractView | None:
    """获取合同当前权威视图（DESIGN §3.1、§11.6、§7 四轴）。"""
    row = conn.execute(
        """
        SELECT contract_id, goal_id, revision, state,
               deadline_status, acceptance_status, blocked_reason,
               title, objective, deadline_at, hard_constraints_json,
               acceptance_json, workload_initial_hours, budget_json,
               soft_guidance_json, context_json, execution_json,
               client_meta_json, authority_json, attention_json,
               continuity_json, auto_approve_json,
               created_at, updated_at, next_wakeup_at,
               next_decision_at, schema_version
        FROM contracts
        WHERE contract_id = ?
        """,
        (contract_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_contract_view(row)


def get_goal(conn: sqlite3.Connection, goal_id: str) -> dict[str, Any] | None:
    """Return stable Goal identity plus an aggregated progress view."""
    row = conn.execute(
        "SELECT goal_id, revision, title, objective, plan_json, progress_json, "
        "created_at, updated_at, schema_version "
        "FROM goals WHERE goal_id = ?",
        (goal_id,),
    ).fetchone()
    if row is None:
        return None
    contracts = conn.execute(
        "SELECT contract_id FROM contracts WHERE goal_id = ? ORDER BY updated_at DESC, contract_id",
        (goal_id,),
    ).fetchall()
    contract_ids = [str(item[0]) for item in contracts]
    states: dict[str, int] = {}
    latest_contract: ContractView | None = None
    latest_risk: dict[str, Any] | None = None
    for contract_id in contract_ids:
        view = get_contract(conn, contract_id)
        if view is None:
            continue
        states[view.state.value] = states.get(view.state.value, 0) + 1
        if latest_contract is None:
            latest_contract = view
            for event in reversed(get_events(conn, contract_id=contract_id)):
                if event.event_type == EventType.FORECAST_UPDATED:
                    try:
                        payload = json.loads(event.payload_json or "{}")
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        latest_risk = payload
                    break
    timeline_rows = conn.execute(
        "SELECT contract_id, attempt_id, event_type, created_at, actor, payload_json "
        "FROM events WHERE goal_id = ? ORDER BY event_id DESC LIMIT 20",
        (goal_id,),
    ).fetchall()
    timeline: list[dict[str, Any]] = []
    for contract_id, attempt_id, event_type, created_at, actor, payload_json in timeline_rows:
        try:
            payload = json.loads(payload_json or "{}")
        except ValueError:
            payload = {}
        timeline.append(
            {
                "contract_id": contract_id,
                "attempt_id": attempt_id,
                "event_type": event_type,
                "created_at": created_at,
                "actor": actor,
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    active_attempts = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE goal_id = ? AND state IN ('admitted', 'running')",
        (goal_id,),
    ).fetchone()
    return {
        "goal_id": row[0],
        "revision": int(row[1]),
        "title": row[2],
        "objective": row[3],
        "plan": json.loads(row[4] or "{}"),
        "progress": json.loads(row[5] or "{}"),
        "created_at": row[6],
        "updated_at": row[7],
        "schema_version": int(row[8]),
        "contract_ids": contract_ids,
        "contract_count": len(contract_ids),
        "state_counts": states,
        "current_contract": latest_contract.to_dict() if latest_contract else None,
        "deadline_snapshot": latest_risk,
        "active_attempt_count": int(active_attempts[0]) if active_attempts else 0,
        "timeline": timeline,
    }


def list_goals(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """List stable Goals, including their contract history."""
    rows = conn.execute(
        "SELECT goal_id FROM goals ORDER BY updated_at DESC, goal_id ASC LIMIT ?", (limit,)
    ).fetchall()
    return [goal for row in rows if (goal := get_goal(conn, str(row[0]))) is not None]


def patch_goal(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    now: datetime,
    plan: dict[str, Any] | None = None,
    progress: dict[str, Any] | None = None,
    expected_revision: int | None = None,
    actor: str = "user",
) -> dict[str, Any]:
    """CAS-update Goal plan/progress and append one auditable amendment event."""
    with transaction(conn):
        row = conn.execute(
            "SELECT revision, plan_json, progress_json FROM goals WHERE goal_id = ?",
            (goal_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"goal {goal_id} not found")
        current_revision = int(row[0])
        if expected_revision is not None and expected_revision != current_revision:
            raise RevisionConflictError(
                f"goal {goal_id} revision conflict: expected {expected_revision}, "
                f"actual {current_revision}"
            )
        next_plan = plan if plan is not None else json.loads(row[1] or "{}")
        next_progress = progress if progress is not None else json.loads(row[2] or "{}")
        from lhgp.goals.progress import normalize_plan

        next_plan = normalize_plan(next_plan)
        next_revision = current_revision + 1
        conn.execute(
            "UPDATE goals SET revision = ?, plan_json = ?, progress_json = ?, updated_at = ? "
            "WHERE goal_id = ?",
            (
                next_revision,
                json.dumps(next_plan, ensure_ascii=False),
                json.dumps(next_progress, ensure_ascii=False),
                now.isoformat(),
                goal_id,
            ),
        )
        append_event(
            conn,
            contract_id=None,
            goal_id=goal_id,
            event_type=EventType.GOAL_AMENDED,
            payload={"revision": next_revision, "plan": next_plan, "progress": next_progress},
            now=now,
            actor=actor,
            role="user",
        )
    result = get_goal(conn, goal_id)
    if result is None:
        raise StoreError(f"goal {goal_id} disappeared after update")
    return result


def _goal_stage_dicts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a Goal plan's stages to object-shaped entries.

    ``plan.stages`` is free-form JSON supplied by a model caller, so malformed
    entries are dropped rather than trusted.  Centralized here because three
    read paths must agree on what counts as a stage.
    """
    raw = plan.get("stages")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def advance_goal(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    complete_stage: str,
    now: datetime,
    expected_revision: int,
    actor: str = "user",
) -> dict[str, Any]:
    """Advance one Goal stage through the same revision-CAS write path."""
    goal = get_goal(conn, goal_id)
    if goal is None:
        raise StoreError(f"goal {goal_id} not found")
    plan = goal["plan"] if isinstance(goal.get("plan"), dict) else {}
    stages = _goal_stage_dicts(plan)
    bound = next(
        (stage for stage in stages if str(stage.get("id", "")) == complete_stage),
        None,
    )
    if isinstance(bound, dict) and bound.get("contract_id"):
        contract = get_contract(conn, str(bound["contract_id"]))
        if contract is None or contract.goal_id != goal_id:
            raise ValueError("stage contract is missing or belongs to another goal")
        if contract.acceptance_status.value != "passed":
            raise ValueError("stage contract must pass acceptance before advancement")
    from lhgp.goals.progress import advance_progress

    progress = advance_progress(goal["plan"], goal["progress"], complete_stage=complete_stage)
    return patch_goal(
        conn,
        goal_id=goal_id,
        now=now,
        progress=progress,
        expected_revision=expected_revision,
        actor=actor,
    )


def auto_approve_drafted_contract(
    conn: sqlite3.Connection,
    contract: Any,
    now: datetime,
) -> bool:
    """Promote a DRAFTED contract to ACTIVE when pre-authorised.

    Trusted source is the **bound Goal's** ``plan.pre_authorized``
    (a user-pinned dict set via ``goal/update`` — Principal-gated).
    The contract's ``draft.auto_approve`` is the model's *claim*
    (what scope the contract needs); the auto-approve only fires
    when the Goal's user-pinned scope covers that claim.

    A contract with no Goal binding, or a Goal without
    ``pre_authorized``, can never be auto-approved — the model
    cannot self-authorize by writing the per-contract field
    alone.  This is the 5th-round follow-up to the
    ``auto_approve`` self-sign fix: the per-contract field was
    the trusted source, which let an MCP-issued contract
    claim arbitrary pre-authorization.

    6th-round P1 follow-up: an MCP-prepared contract has
    its ``auto_approve`` claim stripped at parse time
    (``rpc/handlers/_common.py``) — the model never
    supplies the claim.  When the bound Goal has a
    bounded ``pre_authorized.actions`` list and the
    contract has a recent ``PLAN_APPROVED`` event, the
    plan's action scope has already been verified as a
    subset of the Goal's grant at submit time
    (``mcp_server.lhgp_submit_plan``).  A bare claim is
    therefore not required: the plan-approval is the
    user-pinned sign-off, and a bounded pre_authorized
    Goal is enough to auto-promote.

    A bare ``wildcard=True`` Goal still works the same
    way it always has; the new branch is for the
    action-bounded Goal + plan-verified case.

    Lives in the persistence layer (not the daemon CLI) so the
    ``rpc → cli is forbidden`` arch rule holds.  Best-effort:
    returns False on RevisionConflictError / StoreError so a
    single bad contract cannot block the tick.
    """
    if contract.state != ContractState.DRAFTED:
        return False
    if not _goal_pre_authorizes_contract(conn, contract, now, plan_approved_override=True):
        return False
    try:
        # Revision CAS engaged (4th-round verifier 2 finding):
        # passing expected_revision prevents a duplicate state
        # write + extra revision bump + duplicate
        # CONTRACT_STATE_CHANGED event if the contract was
        # promoted by another path between the snapshot and
        # the per-contract call.
        update_contract_state(
            conn,
            contract_id=contract.contract_id,
            new_state=ContractState.ACTIVE,
            now=now,
            expected_revision=int(contract.revision),
            actor="daemon",
        )
    except (RevisionConflictError, StoreError):
        # Lost the CAS race (another tick already approved it)
        # or transient store error — skip; the next tick will
        # retry. Never raise: auto-approval is best-effort by
        # design so a single bad contract cannot block the
        # whole tick.
        return False
    return True


def _goal_pre_authorizes_contract(
    conn: sqlite3.Connection,
    contract: Any,
    now: datetime,
    *,
    plan_approved_override: bool = False,
) -> bool:
    """Return True iff the contract's bound Goal has a user-pinned
    ``plan.pre_authorized`` that covers the contract's claimed scope.

    A contract with no Goal binding, or a Goal without
    ``pre_authorized``, returns False.  When the contract claims
    actions not in the user-pinned scope, the answer is also False
    so the model cannot escalate beyond the user's grant.

    5th-round P1 regression fix: the previous implementation
    had a "no claim" branch that returned True for any
    contract whose ``auto_approve.enabled=False``.  A model
    could omit the actions field on its draft to bypass
    the scope check entirely.  The new rule is:

    - If the contract claims actions: they must be a subset
      of the Goal's ``pre_authorized.actions``.
    - If the contract does NOT claim actions: the Goal's
      ``pre_authorized`` must explicitly include
      ``wildcard=True`` (a user-pinned "I trust this whole
      goal" sign-off).  Otherwise the no-claim path is a
      silent bypass; the model could be doing anything the
      Goal's grant didn't pin.

    6th-round P1 follow-up: when ``plan_approved_override=True``
    is set, the no-claim branch is widened to also accept
    the "plan-verified" case — a contract whose bound Goal
    has a bounded ``pre_authorized.actions`` list and a
    recent ``PLAN_APPROVED`` event.  The plan-approval has
    already verified at submit time
    (``mcp_server.lhgp_submit_plan``) that the plan's
    action scope is a subset of the Goal's grant, so the
    user-pinned grant + the plan-verified subset is
    enough to auto-activate.  The caller is responsible
    for ensuring the plan is still binding to the
    current contract revision (lifecycle bumps
    re-stamp; content changes do not).  This lets an
    MCP-issued contract (whose ``auto_approve`` claim
    is stripped at parse time) auto-activate under a
    bounded pre_authorized Goal without forcing the
    user to set ``wildcard=True``.
    """
    goal_id = getattr(contract, "goal_id", None)
    if not goal_id:
        return False
    goal = get_goal(conn, goal_id)
    if goal is None:
        return False
    plan = goal.get("plan")
    if not isinstance(plan, dict):
        return False
    pre_authorized = plan.get("pre_authorized")
    if not isinstance(pre_authorized, dict):
        return False
    if not bool(pre_authorized.get("enabled", False)):
        return False
    granted = pre_authorized.get("actions") or ()
    if not isinstance(granted, (list, tuple)):
        return False
    granted_set = {str(a) for a in granted if a}
    is_wildcard = bool(pre_authorized.get("wildcard", False))
    if not granted_set and not is_wildcard:
        return False
    claimed = getattr(contract.draft, "auto_approve", None)
    if claimed is None or not getattr(claimed, "enabled", False):
        # Contract doesn't claim any action scope.  A bare
        # grant (no wildcard) is not enough — the model
        # could be doing actions the user didn't pin.  Only
        # an explicit ``wildcard=True`` sign-off clears,
        # UNLESS the caller has flagged
        # ``plan_approved_override`` AND a recent
        # ``PLAN_APPROVED`` event exists (whose
        # action scope has been verified at submit time).
        return bool(
            is_wildcard
            or (
                plan_approved_override
                and _has_recent_plan_approval_for_goal(
                    conn,
                    contract.contract_id,
                    granted_set,
                    now,
                    goal_id=goal_id,
                    current_pre_authorized=pre_authorized,
                )
            )
        )
    claimed_actions = {str(a) for a in claimed.actions if a}
    if not claimed_actions and not is_wildcard:
        # Contract explicitly claims ``enabled=True`` with
        # zero actions.  Same bypass: without a wildcard
        # we cannot trust a zero-claim contract to not
        # escalate.
        return False
    return claimed_actions.issubset(granted_set)


# Plan-approval lookback window for the
# ``_goal_pre_authorizes_contract`` plan-approval override.
#
# Deliberately STRICTER than the dispatcher's
# ``longtask.cli.dispatch.PLAN_GATE_LOOKBACK_SECONDS`` (24 h):
# this window gates the irreversible DRAFTED -> ACTIVE
# promotion, that one only keeps an already-approved contract
# dispatchable.  An approval that has aged out of this window
# can no longer auto-promote a contract; the user re-signs via
# ``lhgp_plan_signoff`` (or approves the contract directly).
# The two windows are therefore not interchangeable and are not
# claimed to be: keep this value at or below the dispatch window
# so an auto-activated contract is never refused dispatch for
# being "too fresh".
#
# Both windows are measured against the **caller-supplied
# clock** (the daemon tick's ``now``), never the process wall
# clock: a seeded/replayed clock must produce the same verdict
# as the live one, or the two gates cannot be tested together.
PLAN_OVERRIDE_LOOKBACK_SECONDS = 7200


def _grant_scope(pre_authorized: dict[str, Any]) -> tuple[set[str], bool]:
    """Return ``(actions, wildcard)`` of a ``plan.pre_authorized`` grant.

    ``actions`` is an unordered set as far as authorization is concerned, so
    it is normalized to a ``set[str]``; reordering the stored list is not a
    change of scope.  Malformed ``actions`` degrades to the empty set rather
    than being trusted.
    """
    actions = pre_authorized.get("actions") or ()
    if not isinstance(actions, (list, tuple)):
        actions = ()
    granted = {str(a) for a in actions if a}
    return granted, bool(pre_authorized.get("wildcard", False))


def _grant_still_covers(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """True iff ``current`` grants at least everything ``previous`` did.

    The approval certified that the plan's scope sat inside the grant that
    was live *at approval time*.  Monotonicity is what decides whether that
    certification survives a later grant edit:

    - widening (adding actions, or turning a bounded grant into a
      ``wildcard``) keeps every action the approval relied on covered, so
      the approval stays valid;
    - narrowing or swapping actions drops coverage, so the user must sign
      off again;
    - revoking a ``wildcard`` always drops coverage, because an approval
      made under a wildcard has no enumerable action set to re-check.
    """
    previous_actions, previous_wildcard = _grant_scope(previous)
    current_actions, current_wildcard = _grant_scope(current)
    if previous_wildcard:
        return current_wildcard
    if current_wildcard:
        return True
    return previous_actions <= current_actions


def _grant_as_of(
    conn: sqlite3.Connection,
    goal_id: str,
    approval_created_at: str,
) -> dict[str, Any] | None:
    """The Goal's ``pre_authorized`` grant as of ``approval_created_at``.

    Reconstructed from the audit trail: ``patch_goal`` appends a
    ``GOAL_AMENDED`` event carrying the full post-patch plan, so the newest
    amendment at or before the approval is exactly the grant the approval
    was validated against.  ``None`` means "not reconstructable" (no
    amendment yet, or a malformed payload) — callers treat that as unknown
    rather than as a mismatch, so a Goal whose grant predates this helper
    keeps behaving as it always did.
    """
    row = conn.execute(
        "SELECT payload_json FROM events "
        "WHERE goal_id = ? AND event_type = ? AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (goal_id, EventType.GOAL_AMENDED.value, approval_created_at),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None
    plan = payload.get("plan") if isinstance(payload, dict) else None
    pre_authorized = plan.get("pre_authorized") if isinstance(plan, dict) else None
    if not isinstance(pre_authorized, dict):
        return None
    return pre_authorized


def _has_recent_plan_approval_for_goal(
    conn: sqlite3.Connection,
    contract_id: str,
    granted_set: set[str],
    now: datetime,
    *,
    goal_id: str | None = None,
    current_pre_authorized: dict[str, Any] | None = None,
) -> bool:
    """Return True iff ``contract_id`` has a recent ``PLAN_APPROVED``
    that is still binding to ``granted_set`` and has not been
    superseded by a later ``PLAN_REJECTED``.

    6th-round P1 fix: the previous implementation only checked
    "any PLAN_APPROVED in the lookback window."  That was
    insufficient because:

    - A user can edit the Goal's ``pre_authorized`` to a
      narrower grant after a plan was approved; the user
      has effectively narrowed the auto-activate authority
      and the prior approval must not be enough to push a
      DRAFTED contract through.  The submit-time subset
      check ran against the OLD grant, not the current one.

      Follow-up (2026-09-10): the remedy first shipped here —
      requiring ``granted_set`` to be non-empty — only catches
      a grant emptied to ``[]``.  A grant swapped to a
      different non-empty scope (``["write file"]`` →
      ``["read file"]``) sailed through it, so the invariant
      the docstring promised was not the invariant enforced.
      The real check is grant *drift*: when the caller supplies
      ``goal_id`` / ``current_pre_authorized``, the grant as of
      the approval is reconstructed from the ``GOAL_AMENDED``
      trail and must still cover it (see
      :func:`_grant_still_covers`).  Coverage rather than
      equality, so widening the grant never invalidates an
      approval that is still inside the new scope.

    - A subsequent ``PLAN_REJECTED`` inside the same window
      supersedes an older ``PLAN_APPROVED`` (the planner
      rejected a new plan submission that was supposed to
      replace the old one).  The most-recent verdict wins;
      the approval must be the latest.

    Together with the bounded-grant requirement, the user
    must re-confirm via ``lhgp_plan_signoff`` if they
    want the contract to auto-activate under the new
    grant — the auto-activate helper will not back-door a
    narrower grant onto an old approval.

    Why this branch reads as newly load-bearing: while the
    freshness cutoff below read the process wall clock, every
    seeded-clock test saw a stale window and bailed out before
    reaching these rules, so
    ``test_narrowed_grant_blocks_auto_activate`` passed without
    this code ever running.  Making the cutoff respect ``now``
    is what exposed the gap.

    ``now`` is the caller's clock (the daemon tick / RPC
    timestamp) and is the only clock this helper reads: the
    freshness bound is ``now - PLAN_OVERRIDE_LOOKBACK_SECONDS``
    against the event's ``created_at``.  Reading the process
    wall clock here would make the same event log answer
    differently depending on when the code happens to run.
    """
    if not granted_set:
        # A bounded pre_authorized grant must actually
        # declare actions; an empty grant is not a
        # meaningful auto-activate authority.
        return False
    cutoff_iso = (now - timedelta(seconds=PLAN_OVERRIDE_LOOKBACK_SECONDS)).isoformat()
    # The most recent verdict in the lookback window
    # wins.  A later PLAN_REJECTED supersedes an older
    # PLAN_APPROVED.  This single query also proves an
    # approval exists: if the newest verdict in the window
    # is PLAN_APPROVED then an approved row is in it.
    latest_verdict = conn.execute(
        "SELECT event_type, created_at FROM events "
        "WHERE contract_id = ? AND event_type IN (?, ?) "
        "AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (
            contract_id,
            EventType.PLAN_APPROVED.value,
            EventType.PLAN_REJECTED.value,
            cutoff_iso,
        ),
    ).fetchone()
    if latest_verdict is None:
        return False
    if str(latest_verdict[0]) != EventType.PLAN_APPROVED.value:
        return False
    if goal_id is not None and isinstance(current_pre_authorized, dict):
        # The approval authorized the scope the grant had at
        # the time it was written.  A grant edited afterwards
        # must still cover that scope; otherwise the old
        # approval is being reused as authority the user no
        # longer granted.
        grant_at_approval = _grant_as_of(conn, goal_id, str(latest_verdict[1]))
        if grant_at_approval is not None and not _grant_still_covers(
            grant_at_approval, current_pre_authorized
        ):
            return False
    return True


def list_drafted_contracts(conn: sqlite3.Connection) -> list[Any]:
    """List all DRAFTED contracts the daemon should consider for
    auto-approval.  Bounded by the existing
    :func:`list_contracts` paging defaults so a long backlog
    cannot stall a tick.
    """
    return [c for c in list_contracts(conn, limit=1000) if c.state == ContractState.DRAFTED]


def advance_goal_after_verified_contract(
    conn: sqlite3.Connection, contract: Any, now: datetime
) -> None:
    """Advance a stage only when its bound contract has verifier evidence.

    Lives in ``longtask.persistence.store`` (not the daemon
    ``longtask.cli.tick``) so the contract RPC handler can call
    it without crossing the arch layer boundary
    (rpc → cli is forbidden).  The previous home in
    :mod:`longtask.cli.tick` made user-confirm from the MCP path
    impossible to thread back into Goal advancement.
    """
    goal = get_goal(conn, contract.goal_id)
    if goal is None or not isinstance(goal.get("plan"), dict):
        return
    stages = goal["plan"].get("stages")
    if not isinstance(stages, list):
        return
    bound = next(
        (
            stage
            for stage in stages
            if isinstance(stage, dict) and stage.get("contract_id") == contract.contract_id
        ),
        None,
    )
    if bound is None:
        return
    current = goal.get("progress", {}).get("current")
    stage_id = str(bound.get("id", ""))
    if current is not None and str(current) != stage_id:
        return
    try:
        advance_goal(
            conn,
            goal_id=contract.goal_id,
            complete_stage=stage_id,
            now=now,
            expected_revision=int(goal["revision"]),
            actor="verifier",
        )
    except Exception:
        # Contract completion is authoritative; Goal progress
        # can be retried safely on the next read/advance without
        # hiding verifier evidence.
        return


def goal_next_action(conn: sqlite3.Connection, *, goal_id: str) -> dict[str, Any]:
    """Return a deterministic, read-only next action for model callers."""
    goal = get_goal(conn, goal_id)
    if goal is None:
        raise StoreError(f"goal {goal_id} not found")
    plan = goal["plan"] if isinstance(goal["plan"], dict) else {}
    progress = goal["progress"] if isinstance(goal["progress"], dict) else {}
    stages = _goal_stage_dicts(plan)
    completed = {str(item) for item in progress.get("completed", [])}
    current = progress.get("current")
    if current is None and stages:
        current = next(
            (str(stage.get("id")) for stage in stages if str(stage.get("id")) not in completed),
            None,
        )
    if current is None and stages:
        return {"goal_id": goal_id, "action": "satisfied", "reason": "all planned stages completed"}
    current_stage = next(
        (stage for stage in stages if str(stage.get("id")) == str(current)),
        {"id": current} if current else None,
    )
    found = [item for item in (get_contract(conn, cid) for cid in goal["contract_ids"]) if item]
    active = [
        item
        for item in found
        if item.state.value not in {"complete", "satisfied", "cancelled", "archived"}
    ]
    if active:
        contract = active[0]
        return {
            "goal_id": goal_id,
            "stage_id": current,
            "stage": current_stage,
            "action": "resume_contract",
            "contract_id": contract.contract_id,
            "deadline_status": contract.deadline_status.value,
            "next_decision_at": contract.next_decision_at.isoformat()
            if contract.next_decision_at
            else None,
        }
    return {
        "goal_id": goal_id,
        "stage_id": current,
        "stage": current_stage,
        "action": "create_contract",
        "reason": "current stage has no non-terminal contract",
    }


def goal_contract_draft(
    conn: sqlite3.Connection, *, goal_id: str, stage_id: str | None = None
) -> dict[str, Any]:
    """Build a side-effect-free contract draft from the current Goal stage."""
    goal = get_goal(conn, goal_id)
    if goal is None:
        raise StoreError(f"goal {goal_id} not found")
    plan = goal["plan"] if isinstance(goal["plan"], dict) else {}
    stages = _goal_stage_dicts(plan)
    current = stage_id or goal.get("progress", {}).get("current")
    stage = next(
        (item for item in stages if str(item.get("id")) == str(current)),
        None,
    )
    if stage is None:
        raise ValueError("stage_id does not identify a planned stage")
    raw_draft = stage.get("draft", {})
    draft = dict(raw_draft) if isinstance(raw_draft, dict) else {}
    draft.setdefault("title", stage.get("title", stage.get("id", "Goal stage")))
    draft.setdefault("objective", goal["objective"])
    draft.setdefault("goal_id", goal_id)
    draft.setdefault("stage_id", stage.get("id"))
    return {"goal_id": goal_id, "stage": stage, "draft": draft}


def list_contracts(
    conn: sqlite3.Connection,
    *,
    state: ContractState | str | None = None,
    after_contract_id: str | None = None,
    limit: int = 20,
) -> list[ContractView]:
    """查询合同列表，支持按状态过滤与 cursor 分页（DESIGN §11.2、§11.6）。"""
    query = (
        "SELECT contract_id, goal_id, revision, state, "
        "deadline_status, acceptance_status, blocked_reason, "
        "title, objective, deadline_at, hard_constraints_json, "
        "acceptance_json, workload_initial_hours, budget_json, "
        "soft_guidance_json, context_json, execution_json, "
        "client_meta_json, authority_json, attention_json, "
        "continuity_json, auto_approve_json, created_at, updated_at, "
        "next_wakeup_at, next_decision_at, schema_version "
        "FROM contracts WHERE 1=1"
    )
    params: list[Any] = []
    if state is not None:
        state_str = state.value if isinstance(state, ContractState) else str(state)
        query += " AND state = ?"
        params.append(state_str)
    if after_contract_id is not None:
        query += " AND contract_id > ?"
        params.append(after_contract_id)
    query += " ORDER BY contract_id ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [_row_to_contract_view(r) for r in rows]


def save_contract(
    conn: sqlite3.Connection,
    draft: ContractDraft,
    contract_id: str,
    now: datetime,
    *,
    goal_id: str | None = None,
    state: ContractState = ContractState.DRAFTED,
    revision: int = 1,
    next_wakeup_at: datetime | None = None,
    blocked_reason: BlockReason | None = None,
    deadline_status: DeadlineStatus = DeadlineStatus.NOT_DUE,  # SPEC §7.2
    acceptance_status: AcceptanceStatus = AcceptanceStatus.PENDING,  # SPEC §7.3
    next_decision_at: datetime | None = None,  # P4 预留
    request_id: str | None = None,
    actor: str = "user",
    schema_version: int = STORE_SCHEMA_VERSION,
) -> ContractView:
    """起草/创建合同并在同一事务追加 contract/prepared 事件（DESIGN §5、§11.3、§7 四轴）。

    - P1：同步写入 contract_revisions 第一份不可变快照（§13.3 修订不可变）。
    - 幂等：若 request_id 已存在，直接返回已存合同视图，不重复插入。
    """
    with transaction(conn):
        if request_id:
            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                prepared_event = next(
                    (
                        event
                        for event in existing_events
                        if event.event_type == EventType.CONTRACT_PREPARED and event.contract_id
                    ),
                    None,
                )
                replay_contract_id = (
                    str(prepared_event.contract_id)
                    if prepared_event and prepared_event.contract_id
                    else contract_id
                )
                existing_contract = get_contract(conn, replay_contract_id)
                if existing_contract is not None:
                    if existing_contract.draft.to_dict() != draft.to_dict():
                        raise IdempotencyMismatchError(
                            "request_id already belongs to a different contract draft"
                        )
                    return existing_contract

        resolved_goal_id = goal_id or contract_id
        # 已存在的 Goal 身份字段不被合同创建覆盖（审计持久化-R2）：
        # Goal 计划/标题的修订只能走 patch_goal（CAS + GOAL_AMENDED 事件）。
        # 这里只在 Goal 尚不存在时创建；冲突时仅刷新 updated_at。
        conn.execute(
            """
            INSERT INTO goals (goal_id, title, objective, created_at, updated_at, schema_version)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(goal_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (
                resolved_goal_id,
                draft.title,
                draft.objective,
                now.isoformat(),
                now.isoformat(),
                schema_version,
            ),
        )
        conn.execute(
            """
            INSERT INTO contracts (
                contract_id, goal_id, revision, state,
                deadline_status, acceptance_status, blocked_reason,
                title, objective, deadline_at, hard_constraints_json,
                acceptance_json, workload_initial_hours, budget_json,
                soft_guidance_json, context_json, execution_json,
                client_meta_json, authority_json, attention_json,
                continuity_json, auto_approve_json, created_at,
                updated_at, next_wakeup_at, next_decision_at,
                schema_version
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                contract_id,
                resolved_goal_id,
                revision,
                state.value,
                deadline_status.value,
                acceptance_status.value,
                blocked_reason.value if blocked_reason else None,
                draft.title,
                draft.objective,
                draft.deadline_at.isoformat(),
                json.dumps(draft.hard_constraints, ensure_ascii=False),
                json.dumps(
                    {
                        "standard": draft.acceptance.standard,
                        "checks": _serialize_acceptance_checks(draft.acceptance.checks),
                        "verifier": draft.acceptance.verifier,
                        "spec": draft.acceptance.spec,
                        "spec_hash": draft.acceptance.spec_hash,
                    },
                    ensure_ascii=False,
                ),
                draft.workload_initial_hours,
                json.dumps(
                    {
                        "max_dispatches": draft.budget.max_dispatches,
                        "max_escalations": draft.budget.max_escalations,
                        "max_concurrent_attempts": draft.budget.max_concurrent_attempts,
                        "max_attempt_minutes": draft.budget.max_attempt_minutes,
                        "max_output_bytes": draft.budget.max_output_bytes,
                        "verification_attempts_reserved": (
                            draft.budget.verification_attempts_reserved
                        ),
                        **(
                            {"max_cost": draft.budget.max_cost}
                            if draft.budget.max_cost is not None
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(draft.soft_guidance, ensure_ascii=False),
                json.dumps(draft.context, ensure_ascii=False),
                json.dumps(draft.execution, ensure_ascii=False),
                json.dumps(draft.client_meta, ensure_ascii=False),
                json.dumps(authority_to_dict(draft.authority), ensure_ascii=False),
                json.dumps(attention_to_dict(draft.attention), ensure_ascii=False),
                json.dumps(continuity_to_dict(draft.continuity), ensure_ascii=False),
                json.dumps(
                    (
                        draft.auto_approve.to_dict()
                        if isinstance(draft.auto_approve, AutoApprove)
                        else draft.auto_approve
                    ),
                    ensure_ascii=False,
                ),
                now.isoformat(),
                now.isoformat(),
                next_wakeup_at.isoformat() if next_wakeup_at else None,
                next_decision_at.isoformat() if next_decision_at else None,
                schema_version,
            ),
        )

        # P1：写入首版不可变修订快照（DESIGN §13.3、§7 命名迁移）
        _write_revision_snapshot(
            conn,
            contract_id=contract_id,
            revision=revision,
            draft=draft,
            state=state,
            deadline_status=deadline_status,
            acceptance_status=acceptance_status,
            blocked_reason=blocked_reason,
            recorded_at=now,
            recorded_by=actor,
            change_reason="contract/prepared",
        )

        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.CONTRACT_PREPARED,
            payload={
                "actor": actor,
                "title": draft.title,
                "objective": draft.objective,
                "state": state.value,
                "goal_id": resolved_goal_id,
                "deadline_status": deadline_status.value,
                "acceptance_status": acceptance_status.value,
                "revision": revision,
            },
            now=now,
            request_id=request_id,
            actor=actor,
            schema_version=schema_version,
            goal_id=resolved_goal_id,
            contract_revision=revision,
            role="user",
            payload_schema_version=schema_version,
        )

    view = get_contract(conn, contract_id)
    if view is None:
        raise StoreError(f"contract {contract_id} could not be retrieved after save")
    return view


def _write_revision_snapshot(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    revision: int,
    draft: ContractDraft,
    state: ContractState,
    deadline_status: DeadlineStatus,
    acceptance_status: AcceptanceStatus,
    blocked_reason: BlockReason | None,
    recorded_at: datetime,
    recorded_by: str,
    change_reason: str | None,
) -> None:
    """写入一份不可变 contract_revisions 行（DESIGN §13.3、§7 四轴）。

    主键 (contract_id, revision) 保证同一修订只能写入一次；新一次修订只能 revision+1。
    PRIMARY KEY 冲突时直接抛 IntegrityError——调用方必须先校准 revision。
    """
    conn.execute(
        """
        INSERT INTO contract_revisions (
            contract_id, revision, state, deadline_status, acceptance_status,
            blocked_reason, title, objective, deadline_at, hard_constraints_json,
            acceptance_json, workload_initial_hours, budget_json,
            soft_guidance_json, context_json, execution_json, client_meta_json,
            authority_json, attention_json, continuity_json, auto_approve_json,
            recorded_at, recorded_by, change_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract_id,
            revision,
            state.value,
            deadline_status.value,
            acceptance_status.value,
            blocked_reason.value if blocked_reason else None,
            draft.title,
            draft.objective,
            draft.deadline_at.isoformat(),
            json.dumps(draft.hard_constraints, ensure_ascii=False),
            json.dumps(
                {
                    "standard": draft.acceptance.standard,
                    "checks": _serialize_acceptance_checks(draft.acceptance.checks),
                    "verifier": draft.acceptance.verifier,
                    "spec": draft.acceptance.spec,
                    "spec_hash": draft.acceptance.spec_hash,
                },
                ensure_ascii=False,
            ),
            draft.workload_initial_hours,
            json.dumps(
                {
                    "max_dispatches": draft.budget.max_dispatches,
                    "max_escalations": draft.budget.max_escalations,
                    "max_concurrent_attempts": draft.budget.max_concurrent_attempts,
                    "max_attempt_minutes": draft.budget.max_attempt_minutes,
                    "max_output_bytes": draft.budget.max_output_bytes,
                    "verification_attempts_reserved": (draft.budget.verification_attempts_reserved),
                    **(
                        {"max_cost": draft.budget.max_cost}
                        if draft.budget.max_cost is not None
                        else {}
                    ),
                },
                ensure_ascii=False,
            ),
            json.dumps(draft.soft_guidance, ensure_ascii=False),
            json.dumps(draft.context, ensure_ascii=False),
            json.dumps(draft.execution, ensure_ascii=False),
            json.dumps(draft.client_meta, ensure_ascii=False),
            json.dumps(authority_to_dict(draft.authority), ensure_ascii=False),
            json.dumps(attention_to_dict(draft.attention), ensure_ascii=False),
            json.dumps(continuity_to_dict(draft.continuity), ensure_ascii=False),
            json.dumps(
                (
                    draft.auto_approve.to_dict()
                    if isinstance(draft.auto_approve, AutoApprove)
                    else draft.auto_approve
                ),
                ensure_ascii=False,
            ),
            recorded_at.isoformat(),
            recorded_by,
            change_reason,
        ),
    )


_STATE_TO_EVENT: dict[ContractState, EventType] = {
    ContractState.DRAFTED: EventType.CONTRACT_PREPARED,
    ContractState.ACTIVE: EventType.CONTRACT_APPROVED,
    ContractState.PAUSED: EventType.CONTRACT_PAUSED,
    ContractState.BLOCKED: EventType.CONTRACT_BLOCKED,
    ContractState.COMPLETE: EventType.CONTRACT_COMPLETED,
    ContractState.CANCELLED: EventType.CONTRACT_CANCELLED,
    ContractState.EXPIRED: EventType.CONTRACT_EXPIRED,
    ContractState.ARCHIVED: EventType.CONTRACT_ARBITRATED,
}


def update_contract_state(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    new_state: ContractState,
    now: datetime,
    expected_revision: int | None = None,
    blocked_reason: BlockReason | None = None,
    next_wakeup_at: datetime | None = None,
    deadline_status: DeadlineStatus | None = None,  # P1
    acceptance_status: AcceptanceStatus | None = None,  # P1
    next_decision_at: datetime | None = None,  # P4 预留
    event_type: EventType | str | None = None,
    event_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
    actor: str = "daemon",
    schema_version: int = STORE_SCHEMA_VERSION,
) -> ContractView:
    """原子更新合同状态并追加对应事件（DESIGN §5、§11.3、§11.7、§7 四轴）。

    - revision CAS：若指定 expected_revision 且不符，抛 RevisionConflictError。
    - P1：同步写一份新的 contract_revisions 不可变快照，并落四轴字段。
    - 幂等：若 request_id 已存在，直接返回当前合同状态，不重复递增 revision。
    """
    with transaction(conn):
        if request_id:
            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                existing_contract = get_contract(conn, contract_id)
                if existing_contract is not None:
                    return existing_contract

        current = get_contract(conn, contract_id)
        if current is None:
            raise StoreError(f"contract {contract_id} not found")

        if expected_revision is not None and current.revision != expected_revision:
            raise RevisionConflictError(
                f"revision conflict on contract {contract_id}: "
                f"expected {expected_revision}, got {current.revision}"
            )

        new_revision = current.revision + 1
        new_deadline_status = (
            deadline_status if deadline_status is not None else current.deadline_status
        )
        new_acceptance_status = (
            acceptance_status if acceptance_status is not None else current.acceptance_status
        )

        # next_wakeup_at/next_decision_at 未显式给出时保留现值：
        # 两者是调度簿记（P4 决策点由 set_next_decision_at 轻量维护），
        # 状态迁移不得顺手清空它们。
        new_next_wakeup = next_wakeup_at if next_wakeup_at is not None else current.next_wakeup_at
        new_next_decision = (
            next_decision_at if next_decision_at is not None else current.next_decision_at
        )
        conn.execute(
            """
            UPDATE contracts
            SET revision = ?,
                state = ?,
                deadline_status = ?,
                acceptance_status = ?,
                blocked_reason = ?,
                updated_at = ?,
                next_wakeup_at = ?,
                next_decision_at = ?
            WHERE contract_id = ?
            """,
            (
                new_revision,
                new_state.value,
                new_deadline_status.value,
                new_acceptance_status.value,
                blocked_reason.value if blocked_reason else None,
                now.isoformat(),
                new_next_wakeup.isoformat() if new_next_wakeup else None,
                new_next_decision.isoformat() if new_next_decision else None,
                contract_id,
            ),
        )

        # 5th-round P1 regression fix: when the state transition
        # is a pure lifecycle change (DRAFTED→ACTIVE auto-promote,
        # BLOCKED→ACTIVE re-activation, retry reactivation) the
        # plan approval the user granted at the prior revision
        # is still binding — the spec, the accepted_check_ids,
        # and the acceptance criteria are unchanged.  Without
        # this migration the gate would refuse dispatch with
        # "no recent PLAN_APPROVED event" because the approval's
        # contract_revision lags the new revision.
        #
        # We re-stamp any prior PLAN_APPROVED with the new
        # revision.  The plan approval's accepted_check_ids and
        # spec_hash are unchanged; if a later CONTENT change
        # invalidates the plan, the gate's payload checks
        # (spec_hash, accepted_check_ids) still fire.
        #
        # 6th-round P1 regression discovered by the
        # reviewer: a previous round also re-stamped the
        # verifier ATTEMPT_SUCCEEDED here, but verifier
        # evidence is content-bound (it depends on the
        # acceptance spec the verifier actually ran
        # against), not lifecycle-bound.  An unconditional
        # re-stamp brought stale verifier passes back
        # into scope after a user edit to ``acceptance``,
        # letting the contract land in COMPLETE without a
        # fresh check.  Verifier events are now bound to
        # the spec_hash of the acceptance at the time of
        # the run (stamped in ``_finish_attempt``); the
        # user_confirm path matches the event's spec_hash
        # against the contract's current spec_hash and
        # rejects mismatches.  Lifecycle bumps do not
        # re-stamp verifier events on purpose.
        conn.execute(
            """
            UPDATE events
            SET contract_revision = ?
            WHERE contract_id = ?
              AND event_type = ?
              AND contract_revision IS NOT NULL
              AND contract_revision < ?
            """,
            (new_revision, contract_id, EventType.PLAN_APPROVED.value, new_revision),
        )

        # P1：写入新一份不可变修订快照（基于当前最新 draft 字段；后续 patch 会改 draft）
        _write_revision_snapshot(
            conn,
            contract_id=contract_id,
            revision=new_revision,
            draft=current.draft,
            state=new_state,
            deadline_status=new_deadline_status,
            acceptance_status=new_acceptance_status,
            blocked_reason=blocked_reason,
            recorded_at=now,
            recorded_by=actor,
            change_reason=(
                event_type.value if isinstance(event_type, EventType) else str(event_type)
            )
            if event_type
            else f"transition -> {new_state.value}",
        )

        chosen_event_type = event_type or _STATE_TO_EVENT.get(new_state, EventType.CONTRACT_PATCHED)
        payload = {
            "actor": actor,
            "previous_state": current.state.value,
            "new_state": new_state.value,
            "revision": new_revision,
            "deadline_status": new_deadline_status.value,
            "acceptance_status": new_acceptance_status.value,
            **(event_payload or {}),
        }
        if blocked_reason:
            payload["blocked_reason"] = blocked_reason.value

        event = append_event(
            conn,
            contract_id=contract_id,
            event_type=chosen_event_type,
            payload=payload,
            now=now,
            request_id=request_id,
            actor=actor,
            schema_version=schema_version,
            goal_id=current.goal_id,
            contract_revision=new_revision,
            role=actor,
            payload_schema_version=schema_version,
        )
        notify_kind = _notification_kind(chosen_event_type, new_state)
        if notify_kind in current.draft.attention.notify_on:
            enqueue_notification(
                conn,
                idempotency_key=f"{contract_id}:event:{event.event_id}",
                goal_id=current.goal_id,
                event_type=notify_kind,
                channel="local",
                payload={"contract_id": contract_id, "event_id": event.event_id, **payload},
                now=now,
                available_at=_notification_available_at(current.draft.attention, notify_kind, now),
            )

    updated = get_contract(conn, contract_id)
    if updated is None:
        raise StoreError(f"contract {contract_id} disappeared after update")
    return updated


def _notification_kind(event_type: EventType | str, state: ContractState) -> str | None:
    """把状态事件映射为 attention.notify_on 的稳定类别。"""
    raw = event_type.value if isinstance(event_type, EventType) else str(event_type)
    if state == ContractState.EXPIRED or raw == EventType.CONTRACT_EXPIRED.value:
        return "missed"
    if state == ContractState.COMPLETE or raw in {
        EventType.CONTRACT_COMPLETED.value,
        EventType.CONTRACT_SATISFIED.value,
    }:
        return "satisfied"
    if state == ContractState.BLOCKED or raw == EventType.ESCALATION_HANDED_TO_USER.value:
        return "need_user"
    return None


def _notification_available_at(attention: Any, kind: str, now: datetime) -> datetime:
    """安静时间内延迟普通通知；bypass 类别保持立即可投递。"""
    quiet = attention.quiet_hours
    if quiet is None or kind in attention.bypass_quiet_hours_on:
        return now
    try:
        timezone = UTC if quiet.timezone in {"UTC", "Etc/UTC"} else ZoneInfo(quiet.timezone)
        local_now = now.astimezone(timezone)
        start_h, start_m = (int(part) for part in quiet.start.split(":"))
        end_h, end_m = (int(part) for part in quiet.end.split(":"))
        local_time = local_now.timetz().replace(tzinfo=None)
        start = datetime_time(start_h, start_m)
        end = datetime_time(end_h, end_m)
        in_quiet = (
            (start <= local_time < end)
            if start <= end
            else (local_time >= start or local_time < end)
        )
        if not in_quiet:
            return now
        end_date = local_now.date()
        if start > end and local_time >= start:
            end_date += timedelta(days=1)
        return datetime.combine(end_date, end, tzinfo=timezone).astimezone(now.tzinfo)
    except (ValueError, KeyError):
        # attention.validate 应在合同边界拒绝畸形值；若旧库含坏时区，
        # 不阻塞安全关键通知，退回立即投递。
        return now


def patch_contract(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    expected_revision: int,
    now: datetime,
    soft_guidance: dict[str, Any] | None = None,
    acceptance: Acceptance | None = None,
    workload_initial_hours: float | None = None,
    request_id: str | None = None,
    actor: str = "user",
    schema_version: int = STORE_SCHEMA_VERSION,
) -> ContractView:
    """原子修订合同可修改字段并追加 contract/patched 事件（DESIGN §4、§11.2、§11.7、§13.3）。

    - 仅允许修订 soft_guidance / acceptance / workload_initial_hours；
    - revision CAS：expected_revision 不符抛 RevisionConflictError；
    - P1：写一份新 contract_revisions 不可变快照（state/deadline/acceptance 与当前一致）；
    - 幂等：若 request_id 已存在，直接返回当前合同状态。
    """
    with transaction(conn):
        if request_id:
            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                existing_contract = get_contract(conn, contract_id)
                if existing_contract is not None:
                    return existing_contract

        current = get_contract(conn, contract_id)
        if current is None:
            raise StoreError(f"contract {contract_id} not found")

        if current.revision != expected_revision:
            raise RevisionConflictError(
                f"revision conflict on contract {contract_id}: "
                f"expected {expected_revision}, got {current.revision}"
            )

        new_revision = current.revision + 1
        new_soft_guidance = (
            soft_guidance if soft_guidance is not None else current.draft.soft_guidance
        )
        new_acceptance = acceptance if acceptance is not None else current.draft.acceptance
        new_workload = (
            workload_initial_hours
            if workload_initial_hours is not None
            else current.draft.workload_initial_hours
        )

        # 构造一份 patch 后的 draft 视图用于写新快照。
        #
        # 审计 B6：这里原先是**手写枚举** 14 个字段重建 ContractDraft，而该类有
        # 15 个字段——漏掉的 auto_approve 有 default_factory，于是不报错、只静默
        # 填入安全基线（AutoApprove(enabled=False)）。后果是
        # contract_revisions.auto_approve_json 对**每一个被 patch 的修订**都记成
        # 「未授予自动批准」，而活行仍保留真实授权：审计记录里一次静默的授权降级
        # （SPEC §798 把该表记为「不可变合同版本与批准信息」）。
        #
        # 改用 dataclasses.replace：patch 只覆盖它真正要改的三个字段，其余字段
        # 由类自身拷贝。这样 ContractDraft 以后新增字段也不可能再被漏掉——手写
        # 枚举是这类漂移的根源，不是某一行的疏忽。
        patched_draft = dataclasses.replace(
            current.draft,
            acceptance=new_acceptance,
            soft_guidance=new_soft_guidance,
            workload_initial_hours=new_workload,
        )

        conn.execute(
            """
            UPDATE contracts
            SET revision = ?,
                soft_guidance_json = ?,
                acceptance_json = ?,
                workload_initial_hours = ?,
                updated_at = ?
            WHERE contract_id = ?
            """,
            (
                new_revision,
                json.dumps(new_soft_guidance, ensure_ascii=False),
                json.dumps(
                    {
                        "standard": new_acceptance.standard,
                        "checks": _serialize_acceptance_checks(new_acceptance.checks),
                        "verifier": new_acceptance.verifier,
                        "spec": new_acceptance.spec,
                        "spec_hash": new_acceptance.spec_hash,
                    },
                    ensure_ascii=False,
                ),
                new_workload,
                now.isoformat(),
                contract_id,
            ),
        )

        # P1：写新一份不可变快照（state/deadline/acceptance 与当前一致，draft 字段用 patched）
        _write_revision_snapshot(
            conn,
            contract_id=contract_id,
            revision=new_revision,
            draft=patched_draft,
            state=current.state,
            deadline_status=current.deadline_status,
            acceptance_status=current.acceptance_status,
            blocked_reason=current.blocked_reason,
            recorded_at=now,
            recorded_by=actor,
            change_reason="contract/patched",
        )

        patch_details: dict[str, Any] = {"actor": actor, "revision": new_revision}
        if soft_guidance is not None:
            patch_details["soft_guidance"] = soft_guidance
        if acceptance is not None:
            patch_details["acceptance"] = {
                "standard": acceptance.standard,
                "checks": _serialize_acceptance_checks(acceptance.checks),
                "verifier": acceptance.verifier,
            }
        if workload_initial_hours is not None:
            patch_details["workload_initial_hours"] = workload_initial_hours

        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.CONTRACT_PATCHED,
            payload=patch_details,
            now=now,
            request_id=request_id,
            actor=actor,
            schema_version=schema_version,
            goal_id=current.goal_id,
            contract_revision=new_revision,
            role=actor,
            payload_schema_version=schema_version,
        )

        # Mirror the lifecycle-bump migration that
        # ``update_contract_state`` runs (see e8ce9fd):
        # re-stamp any prior PLAN_APPROVED with the new
        # revision so a soft_guidance-only patch does not
        # invalidate the user's prior plan sign-off.  The
        # gate's payload checks (spec_hash,
        # accepted_check_ids) still catch a content change
        # to ``acceptance``.
        conn.execute(
            """
            UPDATE events
            SET contract_revision = ?
            WHERE contract_id = ?
              AND event_type = ?
              AND contract_revision IS NOT NULL
              AND contract_revision < ?
            """,
            (
                new_revision,
                contract_id,
                EventType.PLAN_APPROVED.value,
                new_revision,
            ),
        )

    updated = get_contract(conn, contract_id)
    if updated is None:
        raise StoreError(f"contract {contract_id} disappeared after patch")
    return updated


def write_back(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    attempt_id: str,
    write_generation: int,
    now: datetime,
    partition_id: str | None = None,
    events: Sequence[EventInput] = (),
    contract_state: ContractState | None = None,
    expected_revision: int | None = None,
    blocked_reason: BlockReason | None = None,
    next_wakeup_at: datetime | None = None,
    request_id: str | None = None,
    fence_checker: Callable[[Any, int, str], None] | None = None,
    actor: str = "model",
    schema_version: int = STORE_SCHEMA_VERSION,
    role: str | None = None,  # P1
    contract_revision: int | None = None,  # P1
    goal_id: str | None = None,  # P1
    model_id: str | None = None,
    usage: dict[str, Any] | None = None,
) -> WriteBackResult:
    """带 generation fencing 的执行结果写回（DESIGN §7、§11.3、§14.1）。

    - 校验 write_generation 与 attempt_id：不符抛 LeaseFencedError；
    - 支持可选注入 fence_checker 回调（如 promoter 校验逻辑），避免破坏分层依赖；
    - 单事务原子提交：合同状态变更 + 所有事件追加；
    - 幂等：若 request_id 已存在，返回原写回结果，不产生新事件。
    """
    with transaction(conn):
        if request_id:
            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                contract = get_contract(conn, contract_id)
                return WriteBackResult(
                    contract_id=contract_id,
                    attempt_id=attempt_id,
                    lease_generation=write_generation,
                    event_ids=tuple(e.event_id for e in existing_events),
                    revision=contract.revision if contract else None,
                )

        lease = get_lease(conn, contract_id, partition_id)
        if lease is None:
            raise LeaseFencedError(f"write fenced on contract {contract_id}: no active lease found")

        if fence_checker is not None:
            fence_checker(lease, write_generation, attempt_id)
        else:
            if write_generation != lease.generation:
                raise LeaseFencedError(
                    f"write generation {write_generation} fenced by lease generation "
                    f"{lease.generation} (contract {contract_id})"
                )
            if attempt_id != lease.holder_attempt_id:
                raise LeaseFencedError(
                    f"write attempt {attempt_id} is not lease holder "
                    f"{lease.holder_attempt_id} (contract {contract_id})"
                )

        # 将执行者自报的实际模型与写回一并持久化，确保重启后审计视图
        # 与终态事件中的 model_id 保持一致。仅接受非空值，避免普通进度
        # 写回意外清空调度阶段已选模型。
        if model_id and str(model_id).strip():
            conn.execute(
                "UPDATE attempts SET model_id = ?, updated_at = ? WHERE attempt_id = ?",
                (str(model_id).strip(), now.isoformat(), attempt_id),
            )
        # 消耗台账（§11.3）：执行者自报，形状已由调用方经 normalize_usage
        # fail-closed 校验；这里只落库。同一 attempt 的重复写回按「最后
        # 一次为准」——台账记的是该 attempt 终局的累计自报，不是逐次增量。
        if usage:
            conn.execute(
                "UPDATE attempts SET usage_json = ?, updated_at = ? WHERE attempt_id = ?",
                (
                    json.dumps(usage, ensure_ascii=False, sort_keys=True),
                    now.isoformat(),
                    attempt_id,
                ),
            )

        new_revision: int | None = None
        if contract_state is not None:
            updated_view = update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=contract_state,
                now=now,
                expected_revision=expected_revision,
                blocked_reason=blocked_reason,
                next_wakeup_at=next_wakeup_at,
                actor=actor,
                schema_version=schema_version,
            )
            new_revision = updated_view.revision

        appended_ids: list[int] = []
        for inp in events:
            evt = append_event(
                conn,
                contract_id=contract_id,
                event_type=inp.event_type,
                payload=inp.payload,
                now=now,
                attempt_id=inp.attempt_id or attempt_id,
                lease_generation=inp.lease_generation or write_generation,
                request_id=inp.request_id or request_id,
                actor=inp.actor or actor,
                schema_version=schema_version,
                goal_id=(inp.goal_id or goal_id or contract_id),
                contract_revision=(inp.contract_revision or contract_revision),
                role=(inp.role or role or actor),
                payload_schema_version=(inp.payload_schema_version or schema_version),
            )
            appended_ids.append(evt.event_id)

        # 空 events 且带 request_id 的写回曾没有可探测的幂等锚点：重放同一
        # request_id 会二次执行状态迁移（安全审查 持久化-C3）。落一条
        # 簿记事件，让 get_events_by_request_id 在重放时命中早退。
        if request_id and not appended_ids:
            bookkeeping = append_event(
                conn,
                contract_id=contract_id,
                event_type=EventType.ATTEMPT_WRITE_BACK,
                payload={
                    "attempt_id": attempt_id,
                    "write_generation": write_generation,
                    "contract_state": (
                        contract_state.value if contract_state is not None else None
                    ),
                    "bookkeeping": True,
                },
                now=now,
                attempt_id=attempt_id,
                lease_generation=write_generation,
                request_id=request_id,
                actor=actor,
                schema_version=schema_version,
                goal_id=goal_id or contract_id,
                contract_revision=contract_revision,
                role=role or actor,
                payload_schema_version=schema_version,
            )
            appended_ids.append(bookkeeping.event_id)

    return WriteBackResult(
        contract_id=contract_id,
        attempt_id=attempt_id,
        lease_generation=write_generation,
        event_ids=tuple(appended_ids),
        revision=new_revision,
    )
