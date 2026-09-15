"""Plan-mode gate enforcement in ``src/longtask/cli/dispatch.py``.

The gate is opt-in per contract: when ``context.gate == "plan"`` is set
on a draft, ``_dispatch_attempt`` refuses to call any executor until
the contract has a recent ``PLAN_APPROVED`` event that is not
superseded by a later ``PLAN_REJECTED``. This prevents the gate from
being bypassed by any path that doesn't go through ``plan submit`` /
``tool_submit_plan``.

Branches covered:
- gate not set on contract → dispatch passes through (default off)
- gate set, no plan events at all → refused with DISPATCH_REFUSED
- gate set, only PLAN_REJECTED in window → refused
- gate set, PLAN_REJECTED newer than PLAN_APPROVED → refused
- gate set, PLAN_APPROVED newer than PLAN_REJECTED → passes
- gate set, only PLAN_APPROVED (no rejections) → passes
- stale approval outside lookback window → refused
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.base import ExecutorAdapter
from longtask.adapters.manifest import (
    Capabilities,
    Enforcement,
    SandboxCapability,
)
from longtask.adapters.registry import (
    CostHint,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.dispatch import (
    PLAN_GATE_LOOKBACK_SECONDS,
    _dispatch_attempt,
    _has_recent_plan_approval,
    _plan_gate_required,
    wake_blocked_after_plan_approval,
    wake_blocked_capacity_full,
)
from longtask.contracts.contract_view import ContractState
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
)
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from longtask.promoter.urgency import UrgencyTier

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)


def _make_draft(*, gate: str | None = None, root: Path | None = None) -> ContractDraft:
    workspace_root = str(root / "plan-gate-ws") if root is not None else "plan-gate-ws"
    ctx: dict[str, object] = {"required": True, "limits": {"max_bytes": 8000}}
    if gate is not None:
        ctx["gate"] = gate
    return ContractDraft(
        title="plan-gate test",
        objective="verify plan gate enforcement",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={
            "file_effects": {
                "mode": "workspace-write",
                "workspace_root": workspace_root,
            }
        },
        acceptance=Acceptance(standard="ok", checks=("ok",)),
        workload_initial_hours=2.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=60,
            max_output_bytes=65536,
        ),
        context=ctx,  # type: ignore[arg-type]
    )


def _open_store(root: Path) -> sqlite3.Connection:
    (root / "data").mkdir()
    conn = connect(StoreConfig(db_path=root / "data" / "state.db"))
    ensure_schema(conn)
    return conn


def _save_active(
    conn: sqlite3.Connection,
    cid: str,
    *,
    gate: str | None = None,
    root: Path | None = None,
):
    save_contract(conn, _make_draft(gate=gate, root=root), contract_id=cid, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    view = get_contract(conn, cid)
    assert view is not None
    return view


class _StubAdapter:
    def __init__(self) -> None:
        self.prepare_calls = 0

    def prepare(self, _input: object) -> None:
        self.prepare_calls += 1


def _stub_factory(adapter: _StubAdapter) -> Callable[[RegistryEntry], ExecutorAdapter | None]:
    def factory(_entry: RegistryEntry) -> ExecutorAdapter | None:
        return adapter

    return factory


def _candidate(executor_id: str = "exec-1") -> RegistryEntry:
    return RegistryEntry(
        id=executor_id,
        kind="stub",
        launch=LaunchSpec(argv=(sys.executable, "-c", "pass")),
        capabilities=Capabilities(
            spawn=True,
            observe=True,
            cancel=True,
            notify=False,
            followup=False,
            steer=False,
            interrupt=False,
            context="optional",
            sandbox=SandboxCapability(
                file_effects="workspace-write",
                network="unsupported",
                process="unsupported",
                enforcement=Enforcement.PARTIAL,
            ),
            acceptance_evidence=False,
        ),
        cost_hint=CostHint.MEDIUM,
        enabled=True,
        models=("*",),
    )


def _event_types(conn: sqlite3.Connection, cid: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT event_type FROM events WHERE contract_id = ?",
            (cid,),
        ).fetchall()
    ]


class TestPlanGateRequired:
    def test_default_off(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-no-gate-flag", gate=None, root=tmp_path)
            assert _plan_gate_required(view) is False
        finally:
            conn.close()

    def test_explicit_plan_gate(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-with-gate-flag", gate="plan", root=tmp_path)
            assert _plan_gate_required(view) is True
        finally:
            conn.close()

    def test_non_string_gate_value_ignored(self, tmp_path: Path) -> None:
        """Defensive: only the literal string "plan" enables the gate.
        Other values (None, booleans, ints) keep the gate off so a
        misconfigured contract doesn't accidentally lock itself out."""
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-other-gate", gate=None, root=tmp_path)
            assert _plan_gate_required(view) is False
        finally:
            conn.close()


class TestHasRecentPlanApproval:
    def test_no_events_returns_false(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            assert _has_recent_plan_approval(conn, "lt-no-events", 1, ["ok"], None, NOW) is False
        finally:
            conn.close()

    def test_only_approved_returns_true(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-app",
                event_type=EventType.PLAN_APPROVED,
                payload={
                    "step_count": 3,
                    "contract_revision": 1,
                    "accepted_check_ids": ["ok"],
                },
                now=NOW,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-app", 1, ["ok"], None, NOW) is True
        finally:
            conn.close()

    def test_rejection_after_approval_supersedes(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-rej",
                event_type=EventType.PLAN_APPROVED,
                payload={
                    "step_count": 2,
                    "contract_revision": 1,
                    "accepted_check_ids": ["ok"],
                },
                now=NOW - timedelta(minutes=5),
                actor="daemon",
            )
            append_event(
                conn,
                contract_id="lt-rej",
                event_type=EventType.PLAN_REJECTED,
                payload={"rejection_reasons": ["no rationale"]},
                now=NOW,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-rej", 1, ["ok"], None, NOW) is False
        finally:
            conn.close()

    def test_approval_after_rejection_reinstates(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-fix",
                event_type=EventType.PLAN_REJECTED,
                payload={},
                now=NOW - timedelta(minutes=5),
                actor="daemon",
            )
            append_event(
                conn,
                contract_id="lt-fix",
                event_type=EventType.PLAN_APPROVED,
                payload={
                    "step_count": 2,
                    "contract_revision": 1,
                    "accepted_check_ids": ["ok"],
                },
                now=NOW,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-fix", 1, ["ok"], None, NOW) is True
        finally:
            conn.close()

    def test_stale_approval_outside_lookback_returns_false(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            stale = NOW - timedelta(seconds=PLAN_GATE_LOOKBACK_SECONDS + 60)
            append_event(
                conn,
                contract_id="lt-stale",
                event_type=EventType.PLAN_APPROVED,
                payload={
                    "contract_revision": 1,
                    "accepted_check_ids": ["ok"],
                },
                now=stale,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-stale", 1, ["ok"], None, NOW) is False
        finally:
            conn.close()

    def test_approval_with_mismatched_revision_returns_false(self, tmp_path: Path) -> None:
        """A PLAN_APPROVED for an older contract revision must not satisfy
        the gate once the contract has been revised. Pins the P1 fix for
        "old plan still valid after acceptance criteria change"."""

        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-rev-mismatch",
                event_type=EventType.PLAN_APPROVED,
                payload={
                    "contract_revision": 1,
                    "accepted_check_ids": ["ok"],
                },
                now=NOW,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-rev-mismatch", 2, ["ok"], None, NOW) is False
        finally:
            conn.close()

    def test_approval_with_mismatched_check_ids_returns_false(self, tmp_path: Path) -> None:
        """A PLAN_APPROVED whose accepted_check_ids no longer match the
        current contract acceptance set must not satisfy the gate.
        Pins the P1 fix for "old plan still valid after acceptance
        criteria change"."""

        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-check-mismatch",
                event_type=EventType.PLAN_APPROVED,
                payload={
                    "contract_revision": 1,
                    "accepted_check_ids": ["ok"],
                },
                now=NOW,
                actor="daemon",
            )
            assert (
                _has_recent_plan_approval(
                    conn,
                    "lt-check-mismatch",
                    1,
                    ["ok", "file-exists:dist/app.js"],
                    None,
                    NOW,
                )
                is False
            )
        finally:
            conn.close()


class TestDispatchGateEnforcement:
    def test_gate_off_lets_dispatch_proceed(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-no-gate", gate=None, root=tmp_path)
            adapter = _StubAdapter()
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=lambda _msg: None,
            )
            assert started is not None
            assert adapter.prepare_calls == 1
        finally:
            conn.close()

    def test_gate_on_without_plan_refuses(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-refused", gate="plan", root=tmp_path)
            adapter = _StubAdapter()
            messages: list[str] = []
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=messages.append,
            )
            assert started is None
            assert adapter.prepare_calls == 0
            types = _event_types(conn, "lt-d-refused")
            assert EventType.DISPATCH_REFUSED in types
            assert any("plan-gate" in m for m in messages), messages
        finally:
            conn.close()

    def test_gate_on_with_approval_proceeds(self, tmp_path: Path) -> None:
        from lhgp.contracts.plan import _extract_check_identifiers

        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-ok", gate="plan", root=tmp_path)
            append_event(
                conn,
                contract_id="lt-d-ok",
                event_type=EventType.PLAN_APPROVED,
                payload={
                    "step_count": 3,
                    "contract_revision": view.revision,
                    "accepted_check_ids": list(_extract_check_identifiers(view)),
                },
                now=NOW - timedelta(seconds=30),
                actor="daemon",
            )
            adapter = _StubAdapter()
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=lambda _msg: None,
            )
            assert started is not None
            assert adapter.prepare_calls == 1
        finally:
            conn.close()

    def test_gate_on_with_rejection_after_approval_refuses(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-supersede", gate="plan", root=tmp_path)
            append_event(
                conn,
                contract_id="lt-d-supersede",
                event_type=EventType.PLAN_APPROVED,
                payload={},
                now=NOW - timedelta(minutes=5),
                actor="daemon",
            )
            append_event(
                conn,
                contract_id="lt-d-supersede",
                event_type=EventType.PLAN_REJECTED,
                payload={"rejection_reasons": ["stale action"]},
                now=NOW,
                actor="daemon",
            )
            adapter = _StubAdapter()
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=lambda _msg: None,
            )
            assert started is None
            assert adapter.prepare_calls == 0
        finally:
            conn.close()


class TestWakeBlockedAfterPlanApproval:
    """P1 review fix: a contract stuck in BLOCKED(NO_EXECUTOR) because the
    plan gate refused dispatch must be re-activated when a plan is later
    approved. Otherwise the next daemon tick (which only walks ACTIVE
    contracts) never re-tries the dispatch and the contract sits blocked
    forever."""

    def test_no_contract_returns_false(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            assert wake_blocked_after_plan_approval(conn, "lt-missing", NOW) is False
        finally:
            conn.close()

    def test_active_contract_left_alone(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            _save_active(conn, "lt-stay-active", gate="plan", root=tmp_path)
            assert wake_blocked_after_plan_approval(conn, "lt-stay-active", NOW) is False
        finally:
            conn.close()

    def test_blocked_no_executor_is_woken(self, tmp_path: Path) -> None:
        from lhgp.contracts.contract_view import BlockReason

        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-wake", gate="plan", root=tmp_path)
            update_contract_state(
                conn,
                contract_id=view.contract_id,
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.NO_EXECUTOR,
            )

            woken = wake_blocked_after_plan_approval(conn, "lt-wake", NOW)
            assert woken is True

            after = get_contract(conn, "lt-wake")
            assert after is not None
            assert after.state == ContractState.ACTIVE
            assert after.blocked_reason is None
            assert after.next_decision_at is not None
            assert after.next_decision_at <= NOW
            types = _event_types(conn, "lt-wake")
            assert EventType.CONTRACT_UNBLOCKED in types
        finally:
            conn.close()

    def test_blocked_for_other_reason_left_alone(self, tmp_path: Path) -> None:
        """A plan approval must not rescue a contract blocked for reasons
        that have nothing to do with the plan gate (e.g. budget exhausted
        or need-user). Pin the safety boundary of the wake-up helper."""

        from lhgp.contracts.contract_view import BlockReason

        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-bad-block", gate="plan", root=tmp_path)
            update_contract_state(
                conn,
                contract_id=view.contract_id,
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.BUDGET_EXHAUSTED,
            )
            woken = wake_blocked_after_plan_approval(conn, "lt-bad-block", NOW)
            assert woken is False
            after = get_contract(conn, "lt-bad-block")
            assert after is not None
            assert after.state == ContractState.BLOCKED
            assert after.blocked_reason == BlockReason.BUDGET_EXHAUSTED
        finally:
            conn.close()

    def test_does_not_overwrite_concurrent_cancellation(self, tmp_path: Path) -> None:
        """P1 review (2026-09-08, 2nd round): the wake helper used to
        read state outside the transaction, then write — a concurrent
        user cancellation could complete between the read and the
        write and the wake would silently overwrite a CANCELLED
        state back to ACTIVE.  Fix: the UPDATE pins state+revision,
        so a row mutated in between fails the WHERE clause and the
        wake is a no-op."""

        from lhgp.contracts.contract_view import BlockReason

        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-cancel-race", gate="plan", root=tmp_path)
            update_contract_state(
                conn,
                contract_id=view.contract_id,
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.NO_EXECUTOR,
            )
            # Simulate a user cancellation that completes before the
            # wake's UPDATE runs.  We bump the revision to invalidate
            # the wake's expected_revision CAS.
            cancelled_view = get_contract(conn, "lt-cancel-race")
            assert cancelled_view is not None
            update_contract_state(
                conn,
                contract_id="lt-cancel-race",
                new_state=ContractState.CANCELLED,
                now=NOW + timedelta(seconds=1),
            )
            # The wake must observe the new (CANCELLED) state via the
            # CAS guard and not touch the row.
            woken = wake_blocked_after_plan_approval(
                conn, "lt-cancel-race", NOW + timedelta(seconds=2)
            )
            assert woken is False
            after = get_contract(conn, "lt-cancel-race")
            assert after is not None
            assert after.state == ContractState.CANCELLED
        finally:
            conn.close()


class TestWakeBlockedCapacityFull:
    """P1 review (2026-09-08, 2nd round): once every eligible executor
    is at its max_concurrent_attempts, the contract must go to
    ``BLOCKED(CAPACITY_FULL)`` — distinct from ``NO_EXECUTOR`` so a
    wake-up can find it when the pool frees up.  The wake helper
    pins state+revision (TOCTOU defense) and only acts on
    ``CAPACITY_FULL`` contracts."""

    def test_active_contract_left_alone(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            _save_active(conn, "lt-cap-active", root=tmp_path)
            assert wake_blocked_capacity_full(conn, "lt-cap-active", NOW) is False
        finally:
            conn.close()

    def test_blocked_no_executor_left_alone(self, tmp_path: Path) -> None:
        """A plan-approval wake or a non-cap block must not be reclaimed
        by the capacity wake helper — they have different triggers."""

        from lhgp.contracts.contract_view import BlockReason

        conn = _open_store(tmp_path)
        try:
            _save_active(conn, "lt-cap-noexec", root=tmp_path)
            update_contract_state(
                conn,
                contract_id="lt-cap-noexec",
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.NO_EXECUTOR,
            )
            assert wake_blocked_capacity_full(conn, "lt-cap-noexec", NOW) is False
            after = get_contract(conn, "lt-cap-noexec")
            assert after is not None
            assert after.state == ContractState.BLOCKED
            assert after.blocked_reason == BlockReason.NO_EXECUTOR
        finally:
            conn.close()

    def test_capacity_full_wakes_to_active(self, tmp_path: Path) -> None:
        from lhgp.contracts.contract_view import BlockReason

        conn = _open_store(tmp_path)
        try:
            _save_active(conn, "lt-cap-wake", root=tmp_path)
            update_contract_state(
                conn,
                contract_id="lt-cap-wake",
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.CAPACITY_FULL,
            )
            woken = wake_blocked_capacity_full(conn, "lt-cap-wake", NOW)
            assert woken is True
            after = get_contract(conn, "lt-cap-wake")
            assert after is not None
            assert after.state == ContractState.ACTIVE
            assert after.blocked_reason is None
            assert after.next_decision_at is not None
            assert after.next_decision_at <= NOW
        finally:
            conn.close()

    def test_does_not_overwrite_cancellation(self, tmp_path: Path) -> None:
        """Same TOCTOU defense as wake_blocked_after_plan_approval: a
        concurrent user cancel must not be overwritten by the wake."""

        from lhgp.contracts.contract_view import BlockReason

        conn = _open_store(tmp_path)
        try:
            _save_active(conn, "lt-cap-cancel", root=tmp_path)
            update_contract_state(
                conn,
                contract_id="lt-cap-cancel",
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.CAPACITY_FULL,
            )
            # Concurrent cancel before the wake's UPDATE runs.
            update_contract_state(
                conn,
                contract_id="lt-cap-cancel",
                new_state=ContractState.CANCELLED,
                now=NOW + timedelta(seconds=1),
            )
            woken = wake_blocked_capacity_full(conn, "lt-cap-cancel", NOW + timedelta(seconds=2))
            assert woken is False
            after = get_contract(conn, "lt-cap-cancel")
            assert after is not None
            assert after.state == ContractState.CANCELLED
        finally:
            conn.close()

    def test_capacity_still_full_leaves_the_contract_blocked(self, tmp_path: Path) -> None:
        """审计 B3：唤醒必须真的检查额度，不能无条件翻 ACTIVE。

        这条负向断言要能失败在正确的地方——如果 registry 因为 capability
        或 authority 不匹配本来就选不出候选，本测试同样会看到 False。所以
        先断言「额度空闲时同一 registry 选得出候选」，把后面的 False 钉死
        在容量这一个原因上。
        """

        from lhgp.contracts.contract_view import BlockReason
        from longtask.adapters.registry import ExecutorRegistry
        from longtask.persistence.attempts import count_running_by_executor
        from longtask.promoter.records import _record_attempt

        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-cap-busy", root=tmp_path)
            _save_active(conn, "lt-cap-other", root=tmp_path)
            registry = ExecutorRegistry([_candidate()])
            assert registry.match_candidates(view.draft, running_attempts={}), (
                "registry 选不出候选，本测试无法证明拦下唤醒的是容量"
            )

            # 占满唯一执行器的额度（admitted/running/orphaned 都算在跑）
            _record_attempt(
                conn,
                goal_id=view.goal_id,
                contract_id="lt-cap-other",
                attempt_id="att-busy-1",
                contract_revision=1,
                role="executor",
                executor_id="exec-1",
                state="running",
                admitted_at=NOW,
                updated_at=NOW,
            )
            assert count_running_by_executor(conn).get("exec-1") == 1

            update_contract_state(
                conn,
                contract_id="lt-cap-busy",
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.CAPACITY_FULL,
            )
            assert wake_blocked_capacity_full(conn, "lt-cap-busy", NOW, registry=registry) is False
            after = get_contract(conn, "lt-cap-busy")
            assert after is not None
            assert after.state == ContractState.BLOCKED
            assert after.blocked_reason == BlockReason.CAPACITY_FULL

            # 额度释放后必须还唤得醒——gate 不能把合同永久卡住。
            _record_attempt(
                conn,
                goal_id=view.goal_id,
                contract_id="lt-cap-other",
                attempt_id="att-busy-1",
                contract_revision=1,
                role="executor",
                executor_id="exec-1",
                state="succeeded",
                admitted_at=NOW,
                terminal_at=NOW,
                updated_at=NOW,
            )
            assert count_running_by_executor(conn).get("exec-1") is None
            assert wake_blocked_capacity_full(conn, "lt-cap-busy", NOW, registry=registry) is True
            woken = get_contract(conn, "lt-cap-busy")
            assert woken is not None
            assert woken.state == ContractState.ACTIVE
        finally:
            conn.close()

    def test_blocked_on_capacity_leaves_no_due_decision_point(self, tmp_path: Path) -> None:
        """审计 B3：阻塞在容量上不得钉一个「立刻到期」的决策点。

        ``earliest_next_decision_at`` 的 states 含 ``blocked``，且对已过期
        的决策点原样返回（R1 审查的有意设计，理由是不能让 deadline 决策
        被拖到下个周期）。于是「阻塞时写 next_decision_at = now」会让
        daemon 的 ``sleep_seconds = min(interval, max(0, until))`` 恒为 0：
        每轮唤醒 → 重新阻塞 → 休眠 0，CPU 空转。
        """

        from lhgp.contracts.contract_view import BlockReason
        from lhgp.persistence.decisions import earliest_next_decision_at
        from longtask.cli.dispatch import mark_blocked_capacity_full

        conn = _open_store(tmp_path)
        try:
            _save_active(conn, "lt-cap-spin", root=tmp_path)
            assert mark_blocked_capacity_full(conn, "lt-cap-spin", NOW) is True
            after = get_contract(conn, "lt-cap-spin")
            assert after is not None
            assert after.blocked_reason == BlockReason.CAPACITY_FULL
            assert after.next_decision_at is None, (
                "容量饱和没有确定的未来决策点：重试是事件驱动的，写 now 会让守护进程每轮休眠 0 秒"
            )
            # 系统级可观察量：无到期决策点时 daemon 回落到心跳间隔休眠。
            assert earliest_next_decision_at(conn, now=NOW) is None
        finally:
            conn.close()
