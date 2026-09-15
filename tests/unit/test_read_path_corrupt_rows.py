"""读路径遇到损坏行必须抛类型化错误（审计 B5）。

回归：``_row_to_contract_view`` 直接 ``json.loads(...)`` 与直取键，坏行抛的是
``json.JSONDecodeError`` / ``KeyError`` / 裸 ``ValueError``——**都不属于
``StoreError`` 层级**。而 RPC 层早就为这种情况写好了专属映射：

```python
except StoreTamperedError as exc:
    raise RpcError(code=ErrorCode.STORE_TAMPERED, message=str(exc)) from exc
```

于是那套精心准备的错误码被读路径旁路：调用方拿到的是一句不含合同、不含列名的
``Expecting value: line 1 column 1 (char 0)``。

这里测的是**失败的可诊断性**，不是失败与否——两种实现都会拒绝读取（fail-closed），
不允许降级成凭空的合同字段：悄悄跳过一行合同会让 daemon 连带漏掉它的 deadline
执行，比大声失败更危险。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.acceptance import Acceptance
from longtask.contracts.schema import Budget, ContractDraft
from longtask.persistence.errors import StoreTamperedError
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    list_contracts,
    save_contract,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
CID = "lt-corrupt-row"

# (列名, 破坏值, 断言消息里必须出现的列名)
CORRUPTIONS = (
    ("acceptance_json", "{not json", "acceptance_json"),
    ("budget_json", "{not json", "budget_json"),
    ("auto_approve_json", "{not json", "auto_approve_json"),
    ("acceptance_json", '{"standard": "s"}', "acceptance_json"),  # 缺必填键 checks
    ("budget_json", '{"max_dispatches": 1}', "budget_json"),  # 缺其余必填键
    ("state", "no_such_state", "state"),
    ("acceptance_status", "no_such_status", "acceptance_status"),
    ("deadline_at", "not-a-timestamp", "deadline_at"),
    ("workload_initial_hours", "not-a-number", "workload_initial_hours"),
)
# 允许被破坏的列白名单：列名不能作为 SQL 参数，只能拼进语句，所以这里显式收口
# （下面的 execute 带 noqa: S608，安全性由这条白名单断言保证，不是靠抑制告警）。
CORRUPTIBLE_COLUMNS = frozenset(column for column, _value, _named in CORRUPTIONS)


def _seed(conn: object) -> None:
    save_contract(
        conn,  # type: ignore[arg-type]
        ContractDraft(
            title="损坏行诊断",
            objective="读路径必须自解释地失败",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={},
            acceptance=Acceptance(standard="全部通过", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=CID,
        now=NOW,
        actor="user",
    )


def _corrupt(tmp_path: Path, column: str, value: str) -> object:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    _seed(conn)
    assert column in CORRUPTIBLE_COLUMNS, f"未登记的列 {column!r}"
    conn.execute(
        f"UPDATE contracts SET {column} = ? WHERE contract_id = ?",  # noqa: S608
        (value, CID),
    )
    conn.commit()
    return conn


@pytest.mark.parametrize(("column", "value", "named"), CORRUPTIONS)
def test_corrupt_row_raises_a_typed_store_error(
    tmp_path: Path, column: str, value: str, named: str
) -> None:
    conn = _corrupt(tmp_path, column, value)
    try:
        with pytest.raises(StoreTamperedError) as excinfo:
            get_contract(conn, CID)
        message = str(excinfo.value)
        assert CID in message, f"错误必须点明是哪个合同: {message}"
        assert named in message, f"错误必须点明是哪一列: {message}"
        # 回归的核心：不能再是 json 模块的裸异常
        assert not isinstance(excinfo.value, json.JSONDecodeError)
    finally:
        conn.close()


def test_listing_contracts_also_reports_instead_of_leaking_a_bare_valueerror(
    tmp_path: Path,
) -> None:
    """daemon 走的是 list_contracts（7 个无兜底调用点），那条路径同样要自解释。"""
    conn = _corrupt(tmp_path, "budget_json", "{not json")
    try:
        with pytest.raises(StoreTamperedError):
            list_contracts(conn, limit=10)
    finally:
        conn.close()


def test_rpc_boundary_maps_the_typed_error_to_its_own_code(tmp_path: Path) -> None:
    """端到端：类型化 + 边界映射之后，专属错误码才真正可达。

    这是本修复的意义所在——``ErrorCode.STORE_TAMPERED``（``RETRYABLE=False``）早就
    存在，但读路径抛的异常不属于 ``StoreError``，那道映射从未被这条缺陷触发过。

    走 ``route``（真实客户端路径）而不是直接调 handler：映射现在收在 RPC 边界，
    handler 是内部 API，边界才是对外的承诺。
    """
    from lhgp.rpc.errors import ErrorCode as CanonicalErrorCode
    from lhgp.rpc.errors import RpcError as CanonicalRpcError
    from lhgp.rpc.methods import Method
    from lhgp.rpc.server import RequestEnvelope as CanonicalEnvelope
    from lhgp.rpc.server import route

    conn = _corrupt(tmp_path, "acceptance_json", "{not json")
    try:
        envelope = CanonicalEnvelope(
            method=Method.CONTRACT_GET,
            request_id="get-corrupt",
            client_id="mcp",
            protocol_version=2,
            params={"contract_id": CID},
        )
        with pytest.raises(CanonicalRpcError) as excinfo:
            route(envelope, conn=conn, now=NOW)
        assert excinfo.value.code == CanonicalErrorCode.STORE_TAMPERED
        assert CID in str(excinfo.value)
        assert excinfo.value.retryable is False, "损坏的存储不是可重试错误"
    finally:
        conn.close()


def test_other_readers_reach_the_boundary_too(tmp_path: Path) -> None:
    """其余漏写映射的 handler 同样被边界覆盖（goal/prepare、admission-check）。

    实测 ``contract/get``、``goal/prepare``、``goal/admission-check`` 三个 handler
    完全没有 store 异常映射，而本包内有三种手写写法——边界统一后它们按构造即被覆盖。
    """
    import ast
    import importlib

    from lhgp.persistence.errors import StoreError, StoreTamperedError

    server = importlib.import_module("lhgp.rpc.server")
    source = ast.parse(open(server.__file__, encoding="utf-8").read())  # noqa: SIM115
    caught = {
        node.type.id
        for node in ast.walk(source)
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name)
    }
    assert {"StoreTamperedError", "StoreError"} <= caught, (
        f"RPC 边界必须同时映射这两个类型，实际={sorted(caught)}"
    )
    assert issubclass(StoreTamperedError, StoreError)
