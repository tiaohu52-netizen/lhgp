# ruff: noqa: I001  (import 块长 ruff isort 假阳性，与 src/longtask/cli/watch.py 同原因)
"""longtask watch 单元测试（DESIGN §10 用户主动观察通道）。

覆盖：事件格式化、折叠、过滤、glob 展开；不测试 stderr 染色
（依赖终端类型）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
)
from longtask.cli import watch as watch_mod
from longtask.cli.watch import (
    _emit_batch,
    _format_line,
    _matches_filter,
    _parse_event_kinds,
    _safe_payload,
    _summary,
)

pytestmark = pytest.mark.unit


def _stub_event(
    *,
    etype: EventType,
    contract_id: str = "lt-x",
    attempt_id: str | None = None,
    payload: dict | None = None,
    ts: datetime | None = None,
):
    """最小事件替身（避免真实 SQL 往返）。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        event_type=etype,
        contract_id=contract_id,
        attempt_id=attempt_id,
        payload_json=__import__("json").dumps(payload or {}),
        created_at=ts or datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
    )


class TestSummary:
    def test_contract_prepared(self) -> None:
        s = _summary(EventType.CONTRACT_PREPARED, {"title": "Hello"})
        assert 'new contract "Hello"' in s

    def test_contract_completed(self) -> None:
        s = _summary(EventType.CONTRACT_COMPLETED, {"verifier": "v-att"})
        assert "completed (verifier=v-att)" in s

    def test_attempt_succeeded_with_returncode(self) -> None:
        s = _summary(EventType.ATTEMPT_SUCCEEDED, {"returncode": 0})
        assert "succeeded (rc=0)" in s

    def test_attempt_failed_falls_back(self) -> None:
        # reason 缺，collect_error 缺，用 reported_by 兜底
        s = _summary(EventType.ATTEMPT_FAILED, {"reported_by": "model"})
        assert "failed: model" in s

    def test_empty_payload_returns_empty(self) -> None:
        assert _summary(EventType.LEASE_RENEWED, {}) == ""

    def test_truncation_at_200(self) -> None:
        s = _summary(EventType.ATTEMPT_FAILED, {"reason": "x" * 500})
        assert len(s) <= 200
        assert s.startswith("failed: ")

    def test_first_scalar_fallback(self) -> None:
        # 没有任何匹配规则、payload 只有一个未知标量键
        s = _summary(EventType.PROJECTION_REBUILT, {"unknown_field": 42})
        assert "unknown_field=42" in s


class TestFormatLine:
    def test_format_includes_time_event_and_contract(self) -> None:
        e = _stub_event(
            etype=EventType.CONTRACT_APPROVED,
            payload={"revision": 2},
        )
        line = _format_line(e)
        assert "contract/approved" in line
        assert "lt-x" in line
        assert "rev 2" in line

    def test_attempt_id_dash_when_none(self) -> None:
        e = _stub_event(etype=EventType.LEASE_RENEWED)
        line = _format_line(e)
        assert "- " in line or line.endswith(" -")


class TestFoldRenewed:
    """_emit_batch 折叠 lease/renewed 连续项；非续约事件保持单条。"""

    def test_single_renewed_kept(self, capsys: pytest.CaptureFixture[str]) -> None:
        e = _stub_event(etype=EventType.LEASE_RENEWED, payload={"generation": 1})
        _emit_batch([e])
        out = capsys.readouterr().out
        assert "lease/renewed" in out

    def test_consecutive_renewed_collapsed(self, capsys: pytest.CaptureFixture[str]) -> None:
        events = [
            _stub_event(etype=EventType.LEASE_RENEWED, payload={"generation": i}) for i in range(7)
        ]
        _emit_batch(events)
        out = capsys.readouterr().out
        # 折叠 marker 出现 + payload 不直接漏出（折叠成功）
        assert "x 7" in out
        assert out.count("gen 0") == 0

    def test_non_renewed_breaks_fold(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 三条 renewed 中夹一个 succeeded → 不折叠，三段独立
        events = [
            _stub_event(etype=EventType.LEASE_RENEWED, payload={"generation": 1}),
            _stub_event(etype=EventType.LEASE_RENEWED, payload={"generation": 2}),
            _stub_event(etype=EventType.ATTEMPT_SUCCEEDED, payload={"returncode": 0}),
            _stub_event(etype=EventType.LEASE_RENEWED, payload={"generation": 3}),
        ]
        _emit_batch(events)
        out = capsys.readouterr().out
        assert "x 2" in out
        assert "x 1" in out
        assert "attempt/succeeded" in out


class TestMatchesFilter:
    def test_kinds_filter_excludes(self) -> None:
        e = _stub_event(etype=EventType.LEASE_RENEWED)
        assert not _matches_filter(e, executor_id=None, kinds={EventType.CONTRACT_APPROVED})

    def test_kinds_filter_includes(self) -> None:
        e = _stub_event(etype=EventType.CONTRACT_APPROVED)
        assert _matches_filter(e, executor_id=None, kinds={EventType.CONTRACT_APPROVED})

    def test_executor_filter_substring_match(self) -> None:
        e = _stub_event(
            etype=EventType.ATTEMPT_STARTED,
            payload={"executor_id": "exec-a", "tier": 3},
        )
        assert _matches_filter(e, executor_id="exec-a", kinds=None)
        assert not _matches_filter(e, executor_id="exec-b", kinds=None)

    def test_no_filters_passes_all(self) -> None:
        e = _stub_event(etype=EventType.LEASE_RENEWED)
        assert _matches_filter(e, executor_id=None, kinds=None)


class TestParseEventKinds:
    def test_empty_returns_none(self) -> None:
        assert _parse_event_kinds([]) is None

    def test_exact_match(self) -> None:
        out = _parse_event_kinds(["lease/renewed"])
        assert out == {EventType.LEASE_RENEWED}

    def test_glob_expansion(self) -> None:
        out = _parse_event_kinds(["contract/*"])
        # 所有 contract/* 事件都应被覆盖
        assert EventType.CONTRACT_APPROVED in out
        assert EventType.CONTRACT_COMPLETED in out
        assert EventType.LEASE_RENEWED not in out

    def test_invalid_name_is_rejected_not_ignored(self) -> None:
        """非法 kind 必须报错，不得静默丢弃（fail-closed）。

        回归：旧实现 ``suppress(ValueError)`` 丢掉非法值，且集合为空时返回
        ``None``——而 ``None`` 的语义是**全放行**。于是 ``--kinds typo``
        会显示所有事件，用户以为自己在看某一类，实际是未过滤的全量流。
        部分非法同样要报：静默收窄和静默放行一样难查。
        """
        with pytest.raises(ValueError) as exc:
            _parse_event_kinds(["contract/approved", "nonsense/not-a-type"])
        assert "nonsense/not-a-type" in str(exc.value)

    def test_all_invalid_never_degrades_to_pass_all(self) -> None:
        """全部非法时绝不能退化成 None（全放行）。"""
        with pytest.raises(ValueError):
            _parse_event_kinds(["typo/one", "typo/two"])

    def test_empty_patterns_still_means_pass_all(self) -> None:
        """不传 --kinds 才是「全放行」——这个语义必须保留。"""
        assert _parse_event_kinds([]) is None


class TestWatchEndToEnd:
    """watch() 真实走 SQL 路径：seed 几个事件后调用 watch + 抓 stdout。"""

    def test_replay_prints_events(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from longtask.persistence.store import append_event

        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        ensure_schema(conn)
        save_contract(conn, _make_draft(), contract_id="lt-watch01", now=datetime.now(UTC))
        ts = datetime.now(UTC)
        append_event(
            conn,
            contract_id="lt-watch01",
            event_type=EventType.CONTRACT_APPROVED,
            payload={"revision": 2},
            now=ts,
            actor="user",
        )
        append_event(
            conn,
            contract_id="lt-watch01",
            event_type=EventType.LEASE_RENEWED,
            payload={"generation": 1},
            now=ts,
            actor="daemon",
        )
        conn.close()

        rc = watch_mod.watch(tmp_path, contract_id="lt-watch01")
        assert rc >= 1  # 至少返回 event_id
        out = capsys.readouterr().out
        assert "lt-watch01" in out
        assert "contract/approved" in out
        # 续约应被折叠（仅 1 条 renewal 不出 fold，但 line 仍出现 1 次）
        assert "lease/renewed" in out


def _make_draft():
    from longtask.contracts.schema import Acceptance, Budget, ContractDraft

    return ContractDraft(
        title="watch test",
        objective="replay events",
        deadline_at=datetime.now(UTC).replace(year=2030),
        hard_constraints={"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="s", checks=("c",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=3,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1048576,
        ),
    )


class TestSafePayload:
    """损坏的 payload_json 不得让 watch 崩溃。

    回归：``_format_line`` 直接 ``json.loads(event.payload_json or "{}")``。
    ``payload_json`` 是文本列，旧库或手工写入可能留下非 JSON——一条坏记录
    就会让整个 tail 退出。
    """

    def test_valid_json_passes_through(self) -> None:
        assert _safe_payload('{"reason": "boom"}') == {"reason": "boom"}

    def test_empty_and_none_return_empty_dict(self) -> None:
        assert _safe_payload(None) == {}
        assert _safe_payload("") == {}

    def test_corrupt_json_is_surfaced_not_crashing(self) -> None:
        assert "_raw" in _safe_payload("{not json")

    def test_non_object_json_is_surfaced(self) -> None:
        # 合法 JSON 但不是对象：也不能当 dict 用
        assert "_raw" in _safe_payload("[1, 2, 3]")

    def test_format_line_survives_corrupt_payload(self) -> None:
        """端到端：坏 payload 的事件仍能打印出来，而不是炸掉整条流。"""
        from types import SimpleNamespace

        event = SimpleNamespace(
            event_type=EventType.ATTEMPT_FAILED,
            created_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
            contract_id="lt-corrupt",
            attempt_id="att-1",
            payload_json="<<<not json at all>>>",
        )
        line = _format_line(event)
        assert "lt-corrupt" in line
