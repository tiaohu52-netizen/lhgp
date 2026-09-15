from datetime import UTC, datetime, timedelta

from longtask.contracts.attention import Attention, QuietHours
from longtask.contracts.schema import Acceptance, Budget, ContractDraft, ContractState
from longtask.persistence.notifications import (
    claim_notifications,
    drain_notifications,
    enqueue_notification,
    list_notifications,
    mark_failed,
    mark_sent,
    prune_sent,
)
from longtask.persistence.store import (
    StoreConfig,
    _notification_available_at,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)


def test_outbox_is_idempotent_and_retryable(tmp_path) -> None:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    first = enqueue_notification(
        conn,
        idempotency_key="goal-1:satisfied",
        goal_id="goal-1",
        event_type="satisfied",
        channel="local",
        payload={"text": "done"},
        now=now,
    )
    duplicate = enqueue_notification(
        conn,
        idempotency_key="goal-1:satisfied",
        goal_id="goal-1",
        event_type="satisfied",
        channel="local",
        payload={"text": "duplicate"},
        now=now,
    )
    assert duplicate.notification_id == first.notification_id
    claimed = claim_notifications(conn, now=now)
    assert len(claimed) == 1 and claimed[0].attempts == 1
    mark_failed(
        conn,
        notification_id=first.notification_id,
        now=now,
        error="temporary",
        retry_at=now + timedelta(minutes=1),
    )
    assert claim_notifications(conn, now=now) == []
    retried = claim_notifications(conn, now=now + timedelta(minutes=1))
    assert retried[0].attempts == 2
    mark_sent(conn, notification_id=first.notification_id, now=now)
    assert claim_notifications(conn, now=now + timedelta(hours=1)) == []
    conn.close()


def test_list_notifications_filters_by_goal_id(tmp_path) -> None:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    enqueue_notification(
        conn,
        idempotency_key="goal-a",
        goal_id="goal-a",
        event_type="need_user",
        channel="local",
        payload={},
        now=now,
    )
    enqueue_notification(
        conn,
        idempotency_key="goal-b",
        goal_id="goal-b",
        event_type="need_user",
        channel="local",
        payload={},
        now=now,
    )
    assert [item.goal_id for item in list_notifications(conn, goal_id="goal-a")] == ["goal-a"]
    conn.close()


def test_drain_retries_channel_failure(tmp_path) -> None:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    enqueue_notification(
        conn,
        idempotency_key="goal-2:need-user",
        event_type="need_user",
        channel="local",
        payload={"text": "action required"},
        now=now,
    )
    delivered = []
    assert drain_notifications(conn, now=now, deliver=delivered.append) == 1
    assert delivered[0].event_type == "need_user"

    enqueue_notification(
        conn,
        idempotency_key="goal-3:need-user",
        event_type="need_user",
        channel="local",
        payload={},
        now=now,
    )

    def fail_delivery(_):
        raise RuntimeError("offline")

    assert drain_notifications(conn, now=now, deliver=fail_delivery) == 0
    assert claim_notifications(conn, now=now) == []
    assert claim_notifications(conn, now=now + timedelta(seconds=60))
    conn.close()


def test_quiet_hours_delay_non_bypass_and_allow_bypass() -> None:
    attention = Attention(
        notify_on=("need_user",),
        quiet_hours=QuietHours("22:00", "08:00", "UTC"),
        bypass_quiet_hours_on=("satisfied",),
    )
    now = datetime(2026, 9, 3, 23, 0, tzinfo=UTC)
    delayed = _notification_available_at(attention, "need_user", now)
    assert delayed.hour == 8 and delayed.day == 4
    assert _notification_available_at(attention, "satisfied", now) == now


def test_state_transition_routes_notification_by_attention(tmp_path) -> None:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    draft = ContractDraft(
        title="notify",
        objective="test",
        deadline_at=now + timedelta(days=1),
        hard_constraints={"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="done", checks=("check",)),
        workload_initial_hours=1,
        budget=Budget(1, 1, 1, 10, 1000),
        attention=Attention(notify_on=("satisfied",)),
    )
    save_contract(conn, contract_id="notify-goal", draft=draft, now=now)
    update_contract_state(conn, contract_id="notify-goal", new_state=ContractState.ACTIVE, now=now)
    update_contract_state(
        conn,
        contract_id="notify-goal",
        new_state=ContractState.COMPLETE,
        now=now,
    )
    queued = conn.execute(
        "SELECT event_type FROM notification_outbox WHERE goal_id = ?",
        ("notify-goal",),
    ).fetchall()
    assert [row[0] for row in queued] == ["satisfied"]
    conn.close()


def test_prune_sent_keeps_recent_audit_window(tmp_path) -> None:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    for index in range(3):
        item = enqueue_notification(
            conn,
            idempotency_key=f"prune-{index}",
            event_type="satisfied",
            channel="local",
            payload={},
            now=now - timedelta(days=60 - index),
        )
        claim = claim_notifications(conn, now=now - timedelta(days=60 - index))
        assert claim and claim[0].notification_id == item.notification_id
        mark_sent(conn, notification_id=item.notification_id, now=now - timedelta(days=60 - index))
    assert prune_sent(conn, before=now - timedelta(days=30), keep_latest=1) == 2
    assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    conn.close()
