"""合同状态机必须在唯一写入口被强制（审计 B1）。

缺陷形态：``LEGAL_TRANSITIONS`` 这张表**一直存在**，RPC handler 也一直在守它
（approve ``contract.py:135``、pause ``:1120``、resume ``:1175``、cancel ``:1227``、
arbitrate ``:1316`` 五处调 ``is_valid_transition``），但 ``update_contract_state``
自己不校验——于是**同一个非法转移，走 RPC 被拒、走守护进程或直调却能落库**，
审计记录里的状态链可以与实际状态互相矛盾。修复不是新造策略，而是把已经存在的
策略收到唯一写入口上。

修复前的实测（全量 1630 测试给 ``update_contract_state`` 埋点，记录
``(旧状态, 新状态, 调用点)``）：

- src 侧真实转移 13 种，全部合法或自反；其中**自反写入 13 次**（tick 的
  ``_judge_verifier_outcomes`` 在验收失败时写 ``new_state=ACTIVE`` 而合同本就是
  ACTIVE，只为把 ``acceptance_status`` 置 FAILED——SPEC §12.4 修复闭环）；
- 其余 15 次非法转移**全部来自测试直接调用 store 绕过 handler**（fixture 走捷径：
  ``drafted->blocked`` x13、``drafted->complete`` x4、``drafted->paused`` x2、
  ``active->archived`` x1、``blocked->blocked`` x1），已逐个改为合法路径。

因此本文件断言的是**性质**而不是字面清单：整个状态枚举乘积上，「守卫放行」与
「表 + 自反规则」必须逐对一致；表一变，测试立刻红。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp.contracts.state_machine import is_valid_transition
from lhgp.persistence.errors import IllegalStateTransitionError
from longtask.contracts.acceptance import Acceptance
from longtask.contracts.schema import Budget, ContractDraft, ContractState
from longtask.persistence.store import (
    StoreConfig,
    assert_legal_contract_transition,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
CID = "lt-state-machine"


def _store(tmp_path: Path) -> Any:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    return conn


def _seed(conn: Any, *, state: ContractState = ContractState.DRAFTED) -> None:
    save_contract(
        conn,
        ContractDraft(
            title="状态机强制",
            objective="非法转移不得落库",
            deadline_at=NOW + timedelta(hours=8),
            hard_constraints={"file_effects": {"mode": "read-only"}},
            acceptance=Acceptance(standard="通过", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=CID,
        now=NOW,
        state=state,
    )


# --------------------------------------------------------------------------
# 性质 1：整个状态枚举乘积上，守卫的判定 == 表 + 自反规则
# --------------------------------------------------------------------------


@pytest.mark.parametrize("from_state", list(ContractState))
def test_the_gate_agrees_with_the_table_on_every_pair(from_state: ContractState) -> None:
    """穷举 9x9：``from != to`` 时以 ``is_valid_transition`` 为准，``from == to`` 放行。

    刻意不写任何字面转移清单——手写清单正是这类漂移的成因（同样的教训在
    B6 的 ``dataclasses.replace`` 与 B4 的 ``ATTEMPT_NON_TERMINAL_STATES`` 上
    已经出现过）。
    """
    for to_state in ContractState:
        if from_state == to_state:
            # 自反不是「放水」：实测守护进程真实依赖它写其它三轴。
            assert_legal_contract_transition(from_state, to_state, contract_id=CID)
            continue
        if is_valid_transition(from_state, to_state):
            assert_legal_contract_transition(from_state, to_state, contract_id=CID)
        else:
            with pytest.raises(IllegalStateTransitionError) as excinfo:
                assert_legal_contract_transition(from_state, to_state, contract_id=CID)
            # 消息必须点名合同与两端状态：运维只看到这条日志。
            assert CID in str(excinfo.value)
            assert from_state.value in str(excinfo.value)
            assert to_state.value in str(excinfo.value)


# --------------------------------------------------------------------------
# 性质 2：写入口真的拒绝，并且拒绝是原子的（不留半个写入）
# --------------------------------------------------------------------------


def test_the_store_refuses_an_illegal_transition_and_writes_nothing(tmp_path: Path) -> None:
    """DRAFTED→COMPLETE 被拒，且行保持原样（state/revision 都不动）。

    这是 B1 的核心承诺：非法转移不再「静默落库」。修复前同一调用会成功，并把
    合同标成已完成——一个从未被激活、从未派工、从未验收的合同。
    """
    conn = _store(tmp_path)
    try:
        _seed(conn)
        before = get_contract(conn, CID)
        assert before is not None
        assert before.state == ContractState.DRAFTED

        with pytest.raises(IllegalStateTransitionError):
            update_contract_state(conn, contract_id=CID, new_state=ContractState.COMPLETE, now=NOW)

        after = get_contract(conn, CID)
        assert after is not None
        assert after.state == ContractState.DRAFTED, "非法转移竟然落库了"
        assert after.revision == before.revision, "被拒绝的写入不该递增 revision"
    finally:
        conn.close()


def test_a_daemon_style_illegal_write_is_refused_too(tmp_path: Path) -> None:
    """守护进程路径不是后门：同样的非法转移从 store 直调一样被拒。

    修复前「走 RPC 被拒、走守护进程能落库」正是这条缺陷的要害——handler 有五处
    守卫，而 tick/直调没有。
    """
    conn = _store(tmp_path)
    try:
        _seed(conn)
        for target in (ContractState.COMPLETE, ContractState.BLOCKED, ContractState.PAUSED):
            with pytest.raises(IllegalStateTransitionError):
                update_contract_state(conn, contract_id=CID, new_state=target, now=NOW)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 性质 3：自反转移必须放行（实测出来的生产依赖）
# --------------------------------------------------------------------------


def test_self_transition_is_allowed_so_the_axes_can_be_rewritten(tmp_path: Path) -> None:
    """ACTIVE→ACTIVE 携带 ``acceptance_status=FAILED`` 必须成功。

    实测依据：tick 的 ``_judge_verifier_outcomes``（``tick.py:848``）在验收失败
    时正是这样写的——状态不变，改的是验收轴。若把自反也当非法，SPEC §12.4 的
    修复闭环会直接抛错，守护进程整轮 tick 终止（``daemon_loop.py:235`` 调用
    ``run_daemon_tick`` 时没有兜底）。
    """
    from longtask.contracts.schema import AcceptanceStatus

    conn = _store(tmp_path)
    try:
        _seed(conn, state=ContractState.ACTIVE)
        updated = update_contract_state(
            conn,
            contract_id=CID,
            new_state=ContractState.ACTIVE,
            now=NOW,
            acceptance_status=AcceptanceStatus.FAILED,
        )
        assert updated.state == ContractState.ACTIVE
        assert updated.acceptance_status == AcceptanceStatus.FAILED
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 性质 4：绕过 store 的簿记直写，其转移必须在表内
# --------------------------------------------------------------------------


def test_bookkeeping_direct_writers_only_use_legal_transitions() -> None:
    """三处 CAS 直写（不 bump revision，故不走 store）的转移必须仍在表内。

    - ``dispatch.py`` ``wake_blocked_after_plan_approval``：BLOCKED→ACTIVE
    - ``dispatch.py`` ``mark_blocked_capacity_full``：ACTIVE→BLOCKED
    - ``dispatch.py`` ``wake_blocked_capacity_full``：BLOCKED→ACTIVE

    它们各自把前置状态钉死（``view.state != ... → return False``），所以按构造
    合法；本测试是**漂移守卫**：谁把这几条边从表里删掉，谁就得同时改这三处直写。
    """
    assert is_valid_transition(ContractState.BLOCKED, ContractState.ACTIVE)
    assert is_valid_transition(ContractState.ACTIVE, ContractState.BLOCKED)


# --------------------------------------------------------------------------
# 性质 5：RPC 边界把它映射成 STATE_FORBIDDEN（语义错误，不是内部错误）
# --------------------------------------------------------------------------


def test_rpc_boundary_maps_the_refusal_to_state_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """边界把 ``IllegalStateTransitionError`` 映射为 ``STATE_FORBIDDEN``。

    handler 的守卫先命中并给出更精确的提示，所以这条映射是**兜底**：只有当某条
    路径绕过 handler 直写合同状态时才会走到。语义上它是「该状态下不允许」而不是
    「内部错误」，因此不能落到 ``INTERNAL``。
    """
    from lhgp.rpc import server as canonical_server
    from lhgp.rpc.errors import ErrorCode as CanonicalErrorCode
    from lhgp.rpc.errors import RpcError as CanonicalRpcError
    from lhgp.rpc.methods import Method

    conn = _store(tmp_path)
    try:

        def _refusing_handler(envelope: Any, **_kwargs: Any) -> dict[str, Any]:
            raise IllegalStateTransitionError(
                f"contract {CID}: illegal state transition drafted -> complete"
            )

        monkeypatch.setitem(canonical_server.HANDLERS, Method.CONTRACT_GET, _refusing_handler)
        envelope = canonical_server.RequestEnvelope(
            method=Method.CONTRACT_GET,
            request_id="state-machine-boundary",
            client_id="mcp",
            protocol_version=2,
            params={"contract_id": CID},
        )
        with pytest.raises(CanonicalRpcError) as excinfo:
            canonical_server.route(envelope, conn=conn, now=NOW)
        assert excinfo.value.code == CanonicalErrorCode.STATE_FORBIDDEN
        assert excinfo.value.retryable is False, "非法转移重试多少次都还是非法"
        assert CID in str(excinfo.value)
    finally:
        conn.close()
