"""``patch_contract`` 写的不可变修订快照必须与活合同逐字段一致（审计 B6）。

回归：``patch_contract`` 曾用**手写枚举** 14 个字段重建 ``ContractDraft`` 交给
``_write_revision_snapshot``，而该类有 15 个字段——漏掉的 ``auto_approve`` 带
``default_factory``，所以既不报错也不为空，而是静默填入安全基线
（``AutoApprove(enabled=False)``）。后果：``contract_revisions.auto_approve_json``
对**每一个被 patch 的修订**都记录成「未授予自动批准」，而活行仍保留真实授权——
审计记录里一次静默的授权降级（SPEC §798 把该表记为「不可变合同版本与批准信息」）。

测试刻意**不**手写字段清单：断言「快照 draft 与活 draft 只在被 patch 的三个字段
上不同」，覆盖 ``dataclasses.fields(ContractDraft)`` 的全部字段。手写清单正是
这个缺陷的成因，用它来当断言等于把同一个坑再挖一遍。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.contracts.acceptance import Acceptance
from longtask.contracts.auto_approve import AutoApprove
from longtask.contracts.schema import Budget, ContractDraft
from longtask.persistence import store
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    patch_contract,
    save_contract,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
CID = "lt-patch-snapshot"
# patch_contract 唯一允许改写的三个字段；其余字段必须原样进入快照。
PATCHED_FIELDS = frozenset({"soft_guidance", "acceptance", "workload_initial_hours"})
GRANTED = AutoApprove(
    enabled=True,
    actions=("contract/auto-approve",),
    max_budget_increment=3,
    max_spec_changes=1,
)


def _seed(conn: Any) -> None:
    save_contract(
        conn,
        ContractDraft(
            title="patch 快照字段完整性",
            objective="确认 patch 不丢 draft 字段",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={"file_effects": {"mode": "read-only"}},
            acceptance=Acceptance(standard="全部通过", checks=("c1",), spec_hash="hash-1"),
            workload_initial_hours=2.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
            auto_approve=GRANTED,
            soft_guidance={"tone": "简洁"},
            context={"notes": "n"},
            execution={"target": "src/"},
            client_meta={"origin": "test"},
        ),
        contract_id=CID,
        now=NOW,
        actor="user",
    )


def _snapshot_auto_approve(conn: Any, revision: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT auto_approve_json FROM contract_revisions WHERE contract_id = ? AND revision = ?",
        (CID, revision),
    ).fetchone()
    assert row is not None, "patch 必须写出新修订的不可变快照"
    import json

    return json.loads(row[0])


def test_patched_revision_snapshot_keeps_the_granted_auto_approve(tmp_path: Path) -> None:
    """核心回归：被 patch 的修订快照不得把 auto_approve 降级成安全基线。"""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _seed(conn)
        before = get_contract(conn, CID)
        assert before is not None
        assert before.draft.auto_approve == GRANTED

        patched = patch_contract(
            conn,
            contract_id=CID,
            expected_revision=before.revision,
            now=NOW + timedelta(seconds=1),
            soft_guidance={"tone": "更简洁"},
            actor="user",
        )

        stored = _snapshot_auto_approve(conn, patched.revision)
        assert stored == GRANTED.to_dict(), (
            f"修订快照把授权记成了 {stored!r}，真实授权是 {GRANTED.to_dict()!r}"
        )
        assert stored["enabled"] is True, "审计记录不得声称未授予自动批准"
        # 对照组：活行仍然是真实授权（说明问题只在快照，不在持久化本身）
        live = get_contract(conn, CID)
        assert live is not None
        assert live.draft.auto_approve == GRANTED
    finally:
        conn.close()


def test_snapshot_draft_differs_from_live_only_in_patched_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """结构守卫：快照 draft 与活 draft 只允许在被 patch 的字段上不同。

    逐字段遍历 ``dataclasses.fields(ContractDraft)``——以后给 ContractDraft 新增
    字段时，这条断言自动覆盖它，不需要谁记得回来补测试。
    """
    captured: dict[str, Any] = {}
    original = store._write_revision_snapshot

    def _spy(conn: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        original(conn, **kwargs)

    monkeypatch.setattr(store, "_write_revision_snapshot", _spy)

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _seed(conn)
        before = get_contract(conn, CID)
        assert before is not None
        patch_contract(
            conn,
            contract_id=CID,
            expected_revision=before.revision,
            now=NOW + timedelta(seconds=1),
            soft_guidance={"tone": "更简洁"},
            actor="user",
        )
        assert captured, "_write_revision_snapshot 未被调用"
        snapshot_draft = captured["draft"]
        live = get_contract(conn, CID)
        assert live is not None

        assert snapshot_draft.soft_guidance == {"tone": "更简洁"}
        drifted = [
            field.name
            for field in dataclasses.fields(ContractDraft)
            if field.name not in PATCHED_FIELDS
            and getattr(snapshot_draft, field.name) != getattr(live.draft, field.name)
        ]
        assert drifted == [], (
            f"这些 draft 字段在修订快照里被改写或丢失: {drifted}；"
            "patch 只应覆盖 soft_guidance/acceptance/workload_initial_hours"
        )
    finally:
        conn.close()


def test_contract_draft_field_count_is_what_the_guard_covers(tmp_path: Path) -> None:
    """把「快照必须逐字段一致」的覆盖面钉住（防止有人把 replace 换回手写枚举）。"""
    names = {field.name for field in dataclasses.fields(ContractDraft)}
    assert names >= PATCHED_FIELDS, "字段改名了，测试与被测代码都要同步"
    assert len(names) - len(PATCHED_FIELDS) >= 10, (
        "非 patch 字段数量异常减少——守卫的覆盖面变小了，请复核 patch_contract"
    )

    # 再直接从库上验证一遍：新快照的 auto_approve 列确实来自活行（端到端）
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _seed(conn)
        before = get_contract(conn, CID)
        assert before is not None
        # 初始快照（save_contract 写的第一版）本来就该带授权
        assert _snapshot_auto_approve(conn, before.revision)["enabled"] is True
    finally:
        conn.close()
