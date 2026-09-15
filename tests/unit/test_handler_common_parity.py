"""双树 `rpc/handlers/_common` 的行为一致性（审计 B2）。

`src/lhgp/rpc/handlers/_common.py` 与 `src/longtask/rpc/handlers/_common.py`
是**两份独立实现**，不是门面 + 真身：两边的函数**不是同一个对象**（本文件
最后一条测试会断言这一点，避免哪天有人误以为是 facade 而放松警惕）。
ARCHITECTURE「真身位置地图」把 lhgp 侧记为真身、longtask 侧记为门面——现实
方向相反，而两侧**已经漂移过**：

- `idempotent_replay` 的跨合同归属守卫只长在 legacy 一侧（注释还写着
  「与 canonical 保持一致」），canonical 侧缺失；
- `require_principal` 在 canonical 的 `__all__` 里、在 legacy 的 `__all__` 里
  没有。

「保持一致」写成注释就等于没有保证。这里用同一批场景分别打到两侧实现上，
把不变量变成会红的测试；顺便把 RpcError 同一性也钉住（security-hardening
证据文档曾记「lhgp.rpc.errors vs longtask.rpc.errors 是两个类」，实测已不成立，
legacy 侧现在是正经 facade）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import lhgp.rpc.errors as canonical_errors
import lhgp.rpc.handlers._common as canonical_common
import longtask.rpc.errors as legacy_errors
import longtask.rpc.handlers._common as legacy_common
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.events import EventType
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import append_event, save_contract
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)
CID_A = "lt-parity-a"
CID_B = "lt-parity-b"
SHARED_REQUEST_ID = "req-shared-1"

# (实现模块, 该树自己的错误类)。两侧的错误类实测是同一对象，这里仍各自取用，
# 以便任何一天它们真的分家时，本文件会立刻红。
IMPLEMENTATIONS = [
    pytest.param(canonical_common, canonical_errors.RpcError, id="canonical-lhgp"),
    pytest.param(legacy_common, legacy_errors.RpcError, id="legacy-longtask"),
]


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _draft(title: str) -> ContractDraft:
    return ContractDraft(
        title=title,
        objective="o",
        deadline_at=NOW + timedelta(hours=4),
        hard_constraints={"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=3,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1048576,
        ),
    )


def _seed(tmp_path: Path) -> sqlite3.Connection:
    """两份合同；A 上有一条带 request_id 的事件。"""
    conn = _conn(tmp_path)
    save_contract(conn, contract_id=CID_A, draft=_draft("A"), now=NOW, actor="user")
    save_contract(conn, contract_id=CID_B, draft=_draft("B"), now=NOW, actor="user")
    append_event(
        conn,
        contract_id=CID_A,
        event_type=EventType.CONTRACT_APPROVED,
        payload={},
        now=NOW,
        actor="user",
        request_id=SHARED_REQUEST_ID,
    )
    return conn


def _envelope(request_id: str) -> RequestEnvelope:
    return RequestEnvelope(
        method=Method.CONTRACT_APPROVE,
        request_id=request_id,
        client_id="cli-test",
        protocol_version=1,
    )


def test_error_classes_are_the_same_object() -> None:
    """legacy 的 errors 是 facade；一旦有人把它复制成第二份实现，这里会红。"""
    assert legacy_errors.RpcError is canonical_errors.RpcError
    assert legacy_errors.ErrorCode is canonical_errors.ErrorCode


def test_both_common_modules_export_the_same_names() -> None:
    """导出面必须一致——`require_principal` 曾经只在 canonical 的 __all__ 里。"""
    only_canonical = sorted(set(canonical_common.__all__) - set(legacy_common.__all__))
    only_legacy = sorted(set(legacy_common.__all__) - set(canonical_common.__all__))
    assert only_canonical == [], f"canonical 多出: {only_canonical}"
    assert only_legacy == [], f"legacy 多出: {only_legacy}"


def test_these_are_two_implementations_not_a_facade() -> None:
    """本文件存在的理由：两份实现，所以必须逐场景对齐。

    若哪天收敛成 facade（longtask 侧改为 import *），函数对象会变成同一个，
    这条会红——那时应删除本文件并把一致性交给 `test_dispatch_tables` 式的
    对象同一性断言。
    """
    assert canonical_common.idempotent_replay is not legacy_common.idempotent_replay


@pytest.mark.parametrize(("module", "rpc_error"), IMPLEMENTATIONS)
def test_cross_contract_reuse_rejected(module: object, rpc_error: type, tmp_path: Path) -> None:
    """核心回归：把 A 的 request_id 用到 B 上，必须拒绝而不是假成功。

    修复前的 canonical 侧会返回 B 的快照并回报成功——本次写入被静默吞掉，
    调用方还以为落库了，同时拿到 A 的事件 id。
    """
    conn = _seed(tmp_path)
    try:
        with pytest.raises(rpc_error) as exc_info:
            module.idempotent_replay(conn, _envelope(SHARED_REQUEST_ID), CID_B)  # type: ignore[attr-defined]
        assert exc_info.value.code == canonical_errors.ErrorCode.VALIDATION_FAILED
    finally:
        conn.close()


@pytest.mark.parametrize(("module", "rpc_error"), IMPLEMENTATIONS)
def test_same_contract_replay_returns_the_snapshot(
    module: object, rpc_error: type, tmp_path: Path
) -> None:
    conn = _seed(tmp_path)
    try:
        result = module.idempotent_replay(conn, _envelope(SHARED_REQUEST_ID), CID_A)  # type: ignore[attr-defined]
        assert result is not None
        assert result["ok"] is True
        assert result["result"]["contract_id"] == CID_A
    finally:
        conn.close()


@pytest.mark.parametrize(("module", "rpc_error"), IMPLEMENTATIONS)
def test_unknown_and_absent_request_id_are_not_replays(
    module: object, rpc_error: type, tmp_path: Path
) -> None:
    conn = _seed(tmp_path)
    try:
        assert module.idempotent_replay(conn, _envelope("req-never-seen"), CID_A) is None  # type: ignore[attr-defined]
        assert module.idempotent_replay(conn, _envelope(""), CID_A) is None  # type: ignore[attr-defined]
    finally:
        conn.close()
