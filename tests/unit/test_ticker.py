"""ticker 单轮扫描 run_tick 的仲裁时刻语义（DESIGN §3.3、§5、§6.4）。

钉住的语义：过期判定与仲裁只发生一次、到点只触发不执行、
睡眠/关机期间不伪造「已经推进」、now == deadline_at 不算过期。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from longtask.cli.tick import _remaining_workload_hours
from longtask.scheduler.ticker import ClockEntry, ContractClock, run_tick

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=1)


def make_entry(
    contract_id: str = "lt-20260831-001",
    *,
    deadline_at: datetime = FUTURE,
    next_wakeup_at: datetime = FUTURE,
    arbitrated_at: datetime | None = None,
) -> ClockEntry:
    """构造一个扫描条目，默认「远未到期、唤醒点在未来、未仲裁」。"""
    return ClockEntry(
        contract_id=contract_id,
        clock=ContractClock(
            deadline_at=deadline_at,
            next_wakeup_at=next_wakeup_at,
            arbitrated_at=arbitrated_at,
        ),
    )


class Recorder:
    """emit 通道探针：只记录事件，不 mock 被测对象（CONTRIBUTING 测试纪律）。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def __call__(self, event: str) -> None:
        self.events.append(event)

    def run(self, now: datetime, entries: Sequence[ClockEntry]) -> tuple[ClockEntry, ...]:
        return run_tick(now, entries, self)


def test_remaining_workload_prefers_valid_handover(tmp_path: Path) -> None:
    contract_id = "lt-estimate-001"
    contract_dir = tmp_path / "contracts" / contract_id
    contract_dir.mkdir(parents=True)
    (contract_dir / "handover.md").write_text(
        """## current_stage
executor

## completed_evidence
- c1

## remaining
- c2

## estimate_remaining_hours
0.75

## next_action
continue

## constraints_digest
{}

## source_attempt_id
att-1
""",
        encoding="utf-8",
    )
    contract = SimpleNamespace(
        contract_id=contract_id,
        draft=SimpleNamespace(workload_initial_hours=4.0),
    )
    assert _remaining_workload_hours(tmp_path, contract) == 0.75


def test_remaining_workload_ignores_init_placeholder(tmp_path: Path) -> None:
    """合成占位 handover（source=init）不得覆盖合同工作量修订。

    provisioning 时写入的 init 交接只是准备时刻的初始快照；若把它当真，
    ``contract patch --workload-hours`` 的修订会被静默压回（2026-09-14
    实测：patch 48h 后 urgency 仍按 24h 计算，合同停在派工阈值下不动）。
    """
    contract_id = "lt-estimate-init"
    contract_dir = tmp_path / "contracts" / contract_id
    contract_dir.mkdir(parents=True)
    (contract_dir / "handover.md").write_text(
        """## current_stage
initial

## completed_evidence
- (none)

## remaining
- (none)

## estimate_remaining_hours
0.75

## next_action
init

## constraints_digest
{}

## source_attempt_id
init
""",
        encoding="utf-8",
    )
    contract = SimpleNamespace(
        contract_id=contract_id,
        draft=SimpleNamespace(workload_initial_hours=4.0),
    )
    assert _remaining_workload_hours(tmp_path, contract) == 4.0


def test_repair_brief_preserves_workload_estimate(tmp_path: Path) -> None:
    """修复轮不再对半砍估算。

    逐轮减半会把 urgency 压到派工阈值以下（u<0.5 只剩 REMIND，无重派动作），
    修复轮永远等不到下一次派工——合同卡死在 acceptance=failed
    （2026-09-14 实测）。
    """
    from longtask.acceptance.checks import RepairBrief
    from longtask.cli.tick import _write_repair_brief

    contract = SimpleNamespace(
        contract_id="lt-repair-est",
        draft=SimpleNamespace(workload_initial_hours=4.0, hard_constraints={}),
    )
    _write_repair_brief(
        tmp_path,
        contract,
        "ver-1",
        RepairBrief(failed_checks=("file-exists:x.md",)),
    )
    assert _remaining_workload_hours(tmp_path, contract) == 4.0


class TestExpired:
    def test_overdue_unarbitrated_expires_with_new_clock(self) -> None:
        entry = make_entry(deadline_at=PAST)
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == ["contract/expired:lt-20260831-001"]
        assert len(result) == 1
        assert result[0].contract_id == "lt-20260831-001"
        # 新时钟：仲裁时刻落在本轮 now，其余字段不动（DESIGN §5 保中间成果）
        assert result[0].clock.arbitrated_at == NOW
        assert result[0].clock.deadline_at == PAST
        assert result[0].clock.next_wakeup_at == FUTURE
        # 纯函数：入参条目不被改写
        assert entry.clock.arbitrated_at is None

    def test_overdue_already_arbitrated_not_repeated(self) -> None:
        # 睡眠/关机期间不伪造已推进：已仲裁的不再重复判过期（DESIGN §6.4）
        entry = make_entry(deadline_at=PAST, arbitrated_at=PAST)
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == []
        assert result == (entry,)

    def test_expired_takes_priority_over_promote(self) -> None:
        # 过期未仲裁且唤醒点已到：只判过期，不触发推动（DESIGN §5.1）
        entry = make_entry(deadline_at=PAST, next_wakeup_at=PAST)
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == ["contract/expired:lt-20260831-001"]
        assert result[0].clock.arbitrated_at == NOW

    def test_arbitrated_overdue_due_wakeup_does_not_promote(self) -> None:
        # 已仲裁的过期合同即使唤醒点已到也不再推动（DESIGN §5.1）
        entry = make_entry(deadline_at=PAST, next_wakeup_at=PAST, arbitrated_at=PAST)
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == []
        assert result == (entry,)

    def test_now_equals_deadline_not_expired(self) -> None:
        # DESIGN §6.4：now == deadline_at 不算过期（严格大于才判过期）
        entry = make_entry(deadline_at=NOW, next_wakeup_at=FUTURE)
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == []
        assert result == (entry,)


class TestPromote:
    def test_wakeup_reached_emits_promote(self) -> None:
        entry = make_entry(next_wakeup_at=NOW)
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == ["promote:lt-20260831-001"]
        # 时钟不动：本层只决定「何时」，下一次唤醒点由推动层行动后重算
        assert result == (entry,)

    def test_wakeup_already_past_emits_promote(self) -> None:
        entry = make_entry(next_wakeup_at=PAST)
        rec = Recorder()
        rec.run(NOW, [entry])
        assert rec.events == ["promote:lt-20260831-001"]

    def test_wakeup_in_future_untouched(self) -> None:
        entry = make_entry()
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == []
        assert result == (entry,)

    def test_now_equals_deadline_and_wakeup_due_promotes(self) -> None:
        # 未过期（恰好相等）且唤醒点已到：正常触发推动
        entry = make_entry(deadline_at=NOW, next_wakeup_at=NOW)
        rec = Recorder()
        result = rec.run(NOW, [entry])
        assert rec.events == ["promote:lt-20260831-001"]
        assert result == (entry,)


class TestBatch:
    def test_empty_entries(self) -> None:
        rec = Recorder()
        assert rec.run(NOW, []) == ()
        assert rec.events == []

    def test_mixed_entries_keep_order(self) -> None:
        expired = make_entry("lt-a", deadline_at=PAST)
        due = make_entry("lt-b", next_wakeup_at=NOW)
        idle = make_entry("lt-c")
        arbitrated = make_entry("lt-d", deadline_at=PAST, arbitrated_at=PAST)
        rec = Recorder()
        result = rec.run(NOW, [expired, due, idle, arbitrated])
        # 事件按扫描顺序只覆盖需要动作的合同
        assert rec.events == ["contract/expired:lt-a", "promote:lt-b"]
        assert [e.contract_id for e in result] == ["lt-a", "lt-b", "lt-c", "lt-d"]
        assert result[0].clock.arbitrated_at == NOW
        assert result[1:] == (due, idle, arbitrated)
