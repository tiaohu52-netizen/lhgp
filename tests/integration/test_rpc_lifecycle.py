"""JSON-RPC 控制面方法层端到端与生命周期集成测试（DESIGN §5、§11.1、§11.2、§11.3、§11.6、§11.7）。

真实 SQLite（tmp_path）+ 时间注入：
1. 全生命周期正向流：prepare → approve → patch → pause → resume → cancel / arbitrate；
2. 状态机非法迁移：抛 STATE_FORBIDDEN（drafted 暂停、active 再批准、终态取消等）；
3. patch 修订版本冲突：expected_revision 冲突抛 REVISION_CONFLICT，修改冻结字段抛异常；
4. 分页续读：contract/list 与 protocol/events cursor 分页及过滤；
5. request_id 重放幂等：多次提交同一 request_id 返回原结果且不重复自增 revision；
6. 未知合同错误码：UNKNOWN_CONTRACT；
7. 协议问候：protocol/hello。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask import PROTOCOL_VERSION
from longtask.contracts.schema import ContractState
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    update_contract_state,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope, route

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
DEADLINE = datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)


def make_env(
    method: Method | str,
    request_id: str = "req-001",
    params: dict[str, Any] | None = None,
    client_id: str = "cli-test",
) -> RequestEnvelope:
    """构造合法请求信封（DESIGN §11.1）。"""
    method_enum = method if isinstance(method, Method) else Method(method)
    return RequestEnvelope(
        method=method_enum,
        request_id=request_id,
        client_id=client_id,
        protocol_version=PROTOCOL_VERSION,
        params=params or {},
    )


def make_valid_draft_payload(title: str = "集成测试合同") -> dict[str, Any]:
    """构造合法合同草稿 payload（DESIGN §4、§11.6）。"""
    return {
        "title": title,
        "objective": "完成 RPC 控制面全生命周期验证",
        "deadline_at": DEADLINE.isoformat(),
        "hard_constraints": {
            "file_effects": {"mode": "workspace-write", "workspace_root": "./workspace"},
            "network": {"mode": "deny"},
            "process": {"mode": "restricted"},
            "package_install": {"mode": "deny"},
        },
        "acceptance": {
            "standard": "所有门测试全绿",
            "checks": ["check-1", "check-2"],
            "verifier": "cross_check",
        },
        "workload_estimate": {
            "initial_hours": 8.0,
        },
        "budget": {
            "max_dispatches": 10,
            "max_escalations": 3,
            "max_concurrent_attempts": 2,
            "max_attempt_minutes": 120,
            "max_output_bytes": 1048576,
        },
        "soft_guidance": {"notes": "优先完成核心分支"},
        "context": {"required": False},
        "execution": {"allowed_control": ["notify", "steer"]},
    }


def setup_test_db(tmp_path: Path) -> StoreConfig:
    """初始化测试用 store 数据库（DESIGN §13.3）。"""
    db_path = tmp_path / "state.db"
    config = StoreConfig(db_path=db_path)
    conn = connect(config)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return config


class TestRpcLifecycleAndPositiveFlow:
    """RPC 方法全生命周期正向流集成测试（DESIGN §5、§11.2、§11.5 时序 A）。"""

    def test_full_lifecycle_positive_flow(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            # 1. protocol/hello
            hello_env = make_env(Method.PROTOCOL_HELLO, "req-hello-01")
            hello_res = route(hello_env, conn=conn, now=NOW)
            assert hello_res["ok"] is True
            assert hello_res["result"]["protocol_version"] == PROTOCOL_VERSION
            assert "protocol/events" in hello_res["result"]["methods"]

            # 2. contract/prepare
            contract_id = "lt-20260831-100"
            prep_env = make_env(
                Method.CONTRACT_PREPARE,
                "req-prep-01",
                {"contract_id": contract_id, "draft": make_valid_draft_payload()},
            )
            prep_res = route(prep_env, conn=conn, now=NOW)
            assert prep_res["ok"] is True
            view = prep_res["result"]
            assert view["contract_id"] == contract_id
            assert view["state"] == "drafted"
            assert view["revision"] == 1

            # 3. contract/get
            get_env = make_env(Method.CONTRACT_GET, "req-get-01", {"contract_id": contract_id})
            get_res = route(get_env, conn=conn, now=NOW)
            assert get_res["ok"] is True
            assert get_res["result"]["contract_id"] == contract_id

            # 4. contract/approve (drafted -> active)
            app_env = make_env(
                Method.CONTRACT_APPROVE,
                "req-app-01",
                {"contract_id": contract_id, "expected_revision": 1},
            )
            app_res = route(app_env, conn=conn, now=NOW + timedelta(seconds=1))
            assert app_res["ok"] is True
            assert app_res["result"]["state"] == "active"
            assert app_res["result"]["revision"] == 2

            # 5. contract/patch (修订 soft_guidance / workload)
            patch_env = make_env(
                Method.CONTRACT_PATCH,
                "req-patch-01",
                {
                    "contract_id": contract_id,
                    "expected_revision": 2,
                    "soft_guidance": {"notes": "加紧测试覆盖"},
                    "workload_estimate": {"initial_hours": 12.0},
                },
            )
            patch_res = route(patch_env, conn=conn, now=NOW + timedelta(seconds=2))
            assert patch_res["ok"] is True
            assert patch_res["result"]["revision"] == 3
            assert patch_res["result"]["soft_guidance"] == {"notes": "加紧测试覆盖"}
            assert patch_res["result"]["workload_estimate"]["initial_hours"] == 12.0

            # 6. contract/pause (active -> paused)
            pause_env = make_env(
                Method.CONTRACT_PAUSE,
                "req-pause-01",
                {"contract_id": contract_id, "expected_revision": 3, "reason": "等待外部依赖"},
            )
            pause_res = route(pause_env, conn=conn, now=NOW + timedelta(seconds=3))
            assert pause_res["ok"] is True
            assert pause_res["result"]["state"] == "paused"
            assert pause_res["result"]["revision"] == 4

            # 7. contract/resume (paused -> active)
            resume_env = make_env(
                Method.CONTRACT_RESUME,
                "req-resume-01",
                {"contract_id": contract_id, "expected_revision": 4},
            )
            resume_res = route(resume_env, conn=conn, now=NOW + timedelta(seconds=4))
            assert resume_res["ok"] is True
            assert resume_res["result"]["state"] == "active"
            assert resume_res["result"]["revision"] == 5

            # 8. contract/cancel (active -> cancelled)
            cancel_env = make_env(
                Method.CONTRACT_CANCEL,
                "req-cancel-01",
                {"contract_id": contract_id, "expected_revision": 5, "reason": "用户主动停止"},
            )
            cancel_res = route(cancel_env, conn=conn, now=NOW + timedelta(seconds=5))
            assert cancel_res["ok"] is True
            assert cancel_res["result"]["state"] == "cancelled"
            assert cancel_res["result"]["revision"] == 6

            # 9. protocol/events 检查事件流
            events_env = make_env(
                Method.PROTOCOL_EVENTS,
                "req-events-01",
                {"contract_id": contract_id},
            )
            events_res = route(events_env, conn=conn, now=NOW + timedelta(seconds=6))
            assert events_res["ok"] is True
            events = events_res["result"]["events"]
            event_types = [e["event_type"] for e in events]
            assert event_types == [
                "contract/prepared",
                "contract/approved",
                "contract/patched",
                "contract/paused",
                "contract/resumed",
                "contract/cancelled",
            ]
        finally:
            conn.close()


class TestStateForbiddenTransitions:
    """状态机非法迁移拦截测试（DESIGN §5）。"""

    def test_approve_non_drafted_rejected(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-201"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            route(
                make_env(Method.CONTRACT_APPROVE, "r2", {"contract_id": contract_id}),
                conn=conn,
                now=NOW + timedelta(seconds=1),
            )

            # active 状态再次 approve
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(Method.CONTRACT_APPROVE, "r3", {"contract_id": contract_id}),
                    conn=conn,
                    now=NOW + timedelta(seconds=2),
                )
            assert exc_info.value.code is ErrorCode.STATE_FORBIDDEN
        finally:
            conn.close()

    def test_pause_drafted_or_paused_rejected(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-202"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            # 对 drafted 暂停
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(Method.CONTRACT_PAUSE, "r2", {"contract_id": contract_id}),
                    conn=conn,
                    now=NOW + timedelta(seconds=1),
                )
            assert exc_info.value.code is ErrorCode.STATE_FORBIDDEN
        finally:
            conn.close()

    def test_resume_active_rejected(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-203"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            route(
                make_env(Method.CONTRACT_APPROVE, "r2", {"contract_id": contract_id}),
                conn=conn,
                now=NOW + timedelta(seconds=1),
            )
            # 对 active 恢复
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(Method.CONTRACT_RESUME, "r3", {"contract_id": contract_id}),
                    conn=conn,
                    now=NOW + timedelta(seconds=2),
                )
            assert exc_info.value.code is ErrorCode.STATE_FORBIDDEN
        finally:
            conn.close()

    def test_cancel_or_patch_terminal_contract_rejected(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-204"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            route(
                make_env(Method.CONTRACT_CANCEL, "r2", {"contract_id": contract_id}),
                conn=conn,
                now=NOW + timedelta(seconds=1),
            )

            # 对已 cancelled 合同再次 cancel
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(Method.CONTRACT_CANCEL, "r3", {"contract_id": contract_id}),
                    conn=conn,
                    now=NOW + timedelta(seconds=2),
                )
            assert exc_info.value.code is ErrorCode.STATE_FORBIDDEN

            # 对已 cancelled 合同 patch
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(
                        Method.CONTRACT_PATCH,
                        "r4",
                        {
                            "contract_id": contract_id,
                            "expected_revision": 2,
                            "soft_guidance": {"foo": "bar"},
                        },
                    ),
                    conn=conn,
                    now=NOW + timedelta(seconds=3),
                )
            assert exc_info.value.code is ErrorCode.STATE_FORBIDDEN
        finally:
            conn.close()


class TestArbitration:
    """人工裁决流程测试（DESIGN §5、§11.2、§11.5 时序 C）。"""

    def test_arbitrate_expired_to_complete_and_archived(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-301"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            route(
                make_env(Method.CONTRACT_APPROVE, "r2", {"contract_id": contract_id}),
                conn=conn,
                now=NOW + timedelta(seconds=1),
            )
            # 模拟 Deadline 到期由守护进程置为 EXPIRED
            update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=ContractState.EXPIRED,
                now=NOW + timedelta(days=6),
            )

            # 用户人工裁决：采纳部分成果 complete
            arb_res = route(
                make_env(
                    Method.CONTRACT_ARBITRATE,
                    "r3",
                    {
                        "contract_id": contract_id,
                        "decision": "complete",
                        "note": "采纳阶段性产物",
                    },
                ),
                conn=conn,
                now=NOW + timedelta(days=6, seconds=10),
            )
            assert arb_res["ok"] is True
            assert arb_res["result"]["state"] == "complete"

            # 再次裁决已是终态的 complete：被拒
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(
                        Method.CONTRACT_ARBITRATE,
                        "r4",
                        {"contract_id": contract_id, "decision": "archived"},
                    ),
                    conn=conn,
                    now=NOW + timedelta(days=6, seconds=20),
                )
            assert exc_info.value.code is ErrorCode.STATE_FORBIDDEN
        finally:
            conn.close()

    def test_arbitrate_blocked_to_active(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-302"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            route(
                make_env(Method.CONTRACT_APPROVE, "r2", {"contract_id": contract_id}),
                conn=conn,
                now=NOW + timedelta(seconds=1),
            )
            update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=ContractState.BLOCKED,
                now=NOW + timedelta(seconds=2),
            )

            # 人工裁决：延期续跑 active
            arb_res = route(
                make_env(
                    Method.CONTRACT_ARBITRATE,
                    "r3",
                    {"contract_id": contract_id, "decision": "active"},
                ),
                conn=conn,
                now=NOW + timedelta(seconds=3),
            )
            assert arb_res["ok"] is True
            assert arb_res["result"]["state"] == "active"
        finally:
            conn.close()


class TestPatchAndRevisionConflict:
    """Patch 校验与并发 CAS 冲突测试（DESIGN §4、§11.2、§11.7）。"""

    def test_patch_frozen_fields_rejected(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-401"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )

            # 试图修改冻结区 objective
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(
                        Method.CONTRACT_PATCH,
                        "r2",
                        {
                            "contract_id": contract_id,
                            "expected_revision": 1,
                            "objective": "试图偷改目标",
                        },
                    ),
                    conn=conn,
                    now=NOW + timedelta(seconds=1),
                )
            assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()

    def test_patch_stale_expected_revision_raises_conflict(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-402"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            # patch 1 -> revision 2
            route(
                make_env(
                    Method.CONTRACT_PATCH,
                    "r2",
                    {
                        "contract_id": contract_id,
                        "expected_revision": 1,
                        "soft_guidance": {"round": 1},
                    },
                ),
                conn=conn,
                now=NOW + timedelta(seconds=1),
            )

            # 用旧 expected_revision=1 再次 patch -> REVISION_CONFLICT
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(
                        Method.CONTRACT_PATCH,
                        "r3",
                        {
                            "contract_id": contract_id,
                            "expected_revision": 1,
                            "soft_guidance": {"round": 2},
                        },
                    ),
                    conn=conn,
                    now=NOW + timedelta(seconds=2),
                )
            assert exc_info.value.code is ErrorCode.REVISION_CONFLICT
            assert exc_info.value.retryable is True
        finally:
            conn.close()


class TestPagination:
    """contract/list 与 protocol/events cursor 分页续读测试（DESIGN §11.2、§11.3）。"""

    def test_contract_list_cursor_pagination(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            # 建立 5 个测试合同
            for i in range(1, 6):
                cid = f"lt-20260831-{i:03d}"
                route(
                    make_env(
                        Method.CONTRACT_PREPARE,
                        f"req-c-{i}",
                        {"contract_id": cid, "draft": make_valid_draft_payload(f"合同-{i}")},
                    ),
                    conn=conn,
                    now=NOW + timedelta(seconds=i),
                )
            append_event(
                conn,
                contract_id="lt-20260831-001",
                event_type=EventType.FORECAST_UPDATED,
                payload={"risk": "red", "slack_p90_minutes": -3.0},
                now=NOW + timedelta(seconds=6),
                actor="promoter",
            )

            # 分页 1：limit=2
            p1_res = route(
                make_env(Method.CONTRACT_LIST, "r-l1", {"limit": 2}),
                conn=conn,
                now=NOW + timedelta(seconds=10),
            )
            assert len(p1_res["result"]["contracts"]) == 2
            assert p1_res["result"]["has_more"] is True
            assert p1_res["result"]["contracts"][0]["deadline_snapshot"]["risk"] == "red"
            cursor1 = p1_res["result"]["next_cursor"]
            assert cursor1 == "lt-20260831-002"

            # 分页 2：cursor=cursor1, limit=2
            p2_res = route(
                make_env(Method.CONTRACT_LIST, "r-l2", {"cursor": cursor1, "limit": 2}),
                conn=conn,
                now=NOW + timedelta(seconds=11),
            )
            assert len(p2_res["result"]["contracts"]) == 2
            assert p2_res["result"]["contracts"][0]["contract_id"] == "lt-20260831-003"
            cursor2 = p2_res["result"]["next_cursor"]

            # 分页 3：cursor=cursor2, limit=2 (最后一页仅剩 1 条)
            p3_res = route(
                make_env(Method.CONTRACT_LIST, "r-l3", {"cursor": cursor2, "limit": 2}),
                conn=conn,
                now=NOW + timedelta(seconds=12),
            )
            assert len(p3_res["result"]["contracts"]) == 1
            assert p3_res["result"]["has_more"] is False
        finally:
            conn.close()

    def test_events_cursor_pagination(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            cid = "lt-20260831-501"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": cid, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            route(
                make_env(Method.CONTRACT_APPROVE, "r2", {"contract_id": cid}),
                conn=conn,
                now=NOW + timedelta(seconds=1),
            )
            route(
                make_env(
                    Method.CONTRACT_PATCH,
                    "r3",
                    {"contract_id": cid, "expected_revision": 2, "soft_guidance": {"g": 1}},
                ),
                conn=conn,
                now=NOW + timedelta(seconds=2),
            )

            # 事件分页读取：limit=2
            p1 = route(
                make_env(Method.PROTOCOL_EVENTS, "r-ev1", {"contract_id": cid, "limit": 2}),
                conn=conn,
                now=NOW + timedelta(seconds=3),
            )
            assert len(p1["result"]["events"]) == 2
            assert p1["result"]["has_more"] is True
            c1 = p1["result"]["next_cursor"]

            # 读取后续事件
            p2 = route(
                make_env(
                    Method.PROTOCOL_EVENTS,
                    "r-ev2",
                    {"contract_id": cid, "cursor": c1, "limit": 2},
                ),
                conn=conn,
                now=NOW + timedelta(seconds=4),
            )
            assert len(p2["result"]["events"]) == 1
            assert p2["result"]["has_more"] is False
        finally:
            conn.close()


class TestIdempotencyReplay:
    """基于 request_id 的 RPC 幂等重放测试（DESIGN §11.1、§11.3）。"""

    def test_request_id_replay_returns_same_result(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-601"
            req_prepare = "req-idempotent-prepare-001"

            # 1. 首次 prepare
            res1 = route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    req_prepare,
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )
            # 重放同一 prepare
            res2 = route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    req_prepare,
                    {"contract_id": contract_id, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW + timedelta(seconds=5),
            )
            assert res1["result"]["contract_id"] == res2["result"]["contract_id"]
            assert res1["result"]["revision"] == res2["result"]["revision"]

            # 2. approve 首次与重放
            req_app = "req-idempotent-approve-001"
            app1 = route(
                make_env(
                    Method.CONTRACT_APPROVE,
                    req_app,
                    {"contract_id": contract_id, "expected_revision": 1},
                ),
                conn=conn,
                now=NOW + timedelta(seconds=10),
            )
            assert app1["result"]["revision"] == 2

            app2 = route(
                make_env(
                    Method.CONTRACT_APPROVE,
                    req_app,
                    {"contract_id": contract_id, "expected_revision": 1},
                ),
                conn=conn,
                now=NOW + timedelta(seconds=15),
            )
            assert app2["result"]["revision"] == 2  # revision 未重复递增

            # 检查总事件数：仅 1 (prepared) + 1 (approved) = 2
            ev = route(
                make_env(Method.PROTOCOL_EVENTS, "r-chk", {"contract_id": contract_id}),
                conn=conn,
                now=NOW + timedelta(seconds=20),
            )
            assert len(ev["result"]["events"]) == 2
        finally:
            conn.close()


class TestUnknownContract:
    """未知合同错误码映射测试（DESIGN §11.7）。"""

    def test_unknown_contract_returns_correct_code(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            unknown_id = "lt-99999999-999"
            for meth in (
                Method.CONTRACT_GET,
                Method.CONTRACT_APPROVE,
                Method.CONTRACT_PAUSE,
                Method.CONTRACT_RESUME,
                Method.CONTRACT_CANCEL,
                Method.CONTRACT_ARBITRATE,
            ):
                params: dict[str, Any] = {"contract_id": unknown_id}
                if meth is Method.CONTRACT_ARBITRATE:
                    params["decision"] = "complete"
                with pytest.raises(RpcError) as exc_info:
                    route(
                        make_env(meth, f"req-{meth.value}", params),
                        conn=conn,
                        now=NOW,
                    )
                assert exc_info.value.code is ErrorCode.UNKNOWN_CONTRACT
        finally:
            conn.close()


class TestHandlerValidationBranches:
    """入参格式校验与边缘分支覆盖测试（DESIGN §11.7 VALIDATION_FAILED）。"""

    def test_prepare_rejects_boolean_budget_value(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            payload = make_valid_draft_payload()
            payload["budget"]["max_dispatches"] = True
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(Method.CONTRACT_PREPARE, "budget-bool", {"draft": payload}),
                    conn=conn,
                    now=NOW,
                )
            assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
            assert "budget.max_dispatches" in exc_info.value.message
        finally:
            conn.close()

    def test_list_rejects_boolean_limit(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            with pytest.raises(RpcError) as exc_info:
                route(
                    make_env(Method.CONTRACT_LIST, "limit-bool", {"limit": True}),
                    conn=conn,
                    now=NOW,
                )
            assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()

    def test_prepare_auto_generated_id_and_validation(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            # 1. 自动生成 contract_id
            env = make_env(Method.CONTRACT_PREPARE, "r1", {"draft": make_valid_draft_payload()})
            res = route(env, conn=conn, now=NOW)
            assert res["ok"] is True
            assert res["result"]["contract_id"].startswith("lt-20260831-")

            # 2. 非法 contract_id：含路径成分（安全审查 RPC-C2 后 slug
            # 校验拦路径穿越，命名风格不再强制 lt- 前缀）
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(
                        Method.CONTRACT_PREPARE,
                        "r2",
                        {"contract_id": "lt-x/../escape", "draft": make_valid_draft_payload()},
                    ),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 3. 缺失草稿字段
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(
                        Method.CONTRACT_PREPARE,
                        "r3",
                        {"draft": {"title": "incomplete"}},
                    ),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 4. 草稿校验不通过（如空标题）
            bad_draft = make_valid_draft_payload()
            bad_draft["title"] = ""
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(Method.CONTRACT_PREPARE, "r4", {"draft": bad_draft}),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

        finally:
            conn.close()

    def test_events_invalid_parameters(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            # 非法 cursor
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(Method.PROTOCOL_EVENTS, "r1", {"cursor": "not-int"}),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # JSON boolean 不是协议整数；拒绝 Python 的 bool-as-int 子类行为。
            for request_id, params in (
                ("r-bool-cursor", {"cursor": True}),
                ("r-bool-limit", {"limit": False}),
            ):
                with pytest.raises(RpcError) as exc:
                    route(
                        make_env(Method.PROTOCOL_EVENTS, request_id, params),
                        conn=conn,
                        now=NOW,
                    )
                assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 非正 limit
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(Method.PROTOCOL_EVENTS, "r2", {"limit": 0}),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 过大 limit 也必须拒绝，防止一次性构造过大的模型上下文
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(Method.CONTRACT_LIST, "r3", {"limit": 201}),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()

    def test_list_invalid_parameters(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            # 未知状态
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(Method.CONTRACT_LIST, "r1", {"state": "not_a_state"}),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 非正 limit
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(Method.CONTRACT_LIST, "r2", {"limit": -5}),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()

    def test_patch_invalid_parameters(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            cid = "lt-20260831-701"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": cid, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )

            # 缺失 expected_revision
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(Method.CONTRACT_PATCH, "r2", {"contract_id": cid}),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 非法 expected_revision
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(
                        Method.CONTRACT_PATCH,
                        "r3",
                        {"contract_id": cid, "expected_revision": "abc"},
                    ),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 非法 acceptance
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(
                        Method.CONTRACT_PATCH,
                        "r4",
                        {
                            "contract_id": cid,
                            "expected_revision": 1,
                            "acceptance": {"standard": "", "checks": []},
                        },
                    ),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED

            # 非正 workload
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(
                        Method.CONTRACT_PATCH,
                        "r5",
                        {
                            "contract_id": cid,
                            "expected_revision": 1,
                            "workload_initial_hours": -1.0,
                        },
                    ),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()

    def test_arbitrate_invalid_decision(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            cid = "lt-20260831-702"
            route(
                make_env(
                    Method.CONTRACT_PREPARE,
                    "r1",
                    {"contract_id": cid, "draft": make_valid_draft_payload()},
                ),
                conn=conn,
                now=NOW,
            )

            # 未知决策选项
            with pytest.raises(RpcError) as exc:
                route(
                    make_env(
                        Method.CONTRACT_ARBITRATE,
                        "r2",
                        {"contract_id": cid, "decision": "fly_to_mars"},
                    ),
                    conn=conn,
                    now=NOW,
                )
            assert exc.value.code is ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()

    def test_missing_contract_id_on_methods(self, tmp_path: Path) -> None:
        config = setup_test_db(tmp_path)
        conn = connect(config)
        try:
            for meth in (
                Method.CONTRACT_GET,
                Method.CONTRACT_APPROVE,
                Method.CONTRACT_PATCH,
                Method.CONTRACT_PAUSE,
                Method.CONTRACT_RESUME,
                Method.CONTRACT_CANCEL,
                Method.CONTRACT_ARBITRATE,
            ):
                with pytest.raises(RpcError) as exc:
                    route(make_env(meth, f"req-{meth.value}", {}), conn=conn, now=NOW)
                assert exc.value.code is ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()


def test_contract_get_exposes_isolated_decision_history(tmp_path: Path) -> None:
    """模型读取合同时可解释本合同的风险决策，且不会串入同 Goal 记录。"""
    conn = connect(setup_test_db(tmp_path))
    try:
        cid = "lt-20260831-decision-history"
        route(
            make_env(
                Method.CONTRACT_PREPARE,
                "req-decision-prep",
                {"contract_id": cid, "draft": make_valid_draft_payload()},
            ),
            conn=conn,
            now=NOW,
        )
        goal_id = conn.execute(
            "SELECT goal_id FROM contracts WHERE contract_id = ?", (cid,)
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO attempts (
                attempt_id, goal_id, contract_id, contract_revision, role,
                state, admitted_at, updated_at
            ) VALUES (?, ?, ?, 1, 'verifier', 'failed', ?, ?)""",
            ("ver-decision-history", goal_id, cid, NOW.isoformat(), NOW.isoformat()),
        )
        # 遗留行没有可证明的 contract_id：合同级视图必须 fail-closed 排除。
        conn.execute(
            """INSERT INTO attempts (
                attempt_id, goal_id, contract_revision, role,
                state, admitted_at, updated_at
            ) VALUES ('legacy-unbound', ?, 1, 'executor', 'failed', ?, ?)""",
            (goal_id, NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """INSERT INTO decisions (
                goal_id, contract_id, contract_revision, tier, decision_type,
                reason, budget_dispatches_left, budget_escalations_left,
                payload_json, recorded_at, actor
            ) VALUES (?, ?, 1, '3', 'hand-to-user', 'deadline risk', 2, 1, '{}', ?, 'promoter')""",
            (goal_id, cid, NOW.isoformat()),
        )
        conn.commit()

        result = route(
            make_env(Method.CONTRACT_GET, "req-decision-get", {"contract_id": cid}),
            conn=conn,
            now=NOW,
        )
        history = result["result"]["decision_history"]
        assert len(history) == 1
        assert history[0]["contract_id"] == cid
        assert history[0]["decision_type"] == "hand-to-user"
        assert history[0]["tier"] == 3
        attempts = result["result"]["attempt_history"]
        assert attempts[0]["attempt_id"] == "ver-decision-history"
        assert attempts[0]["role"] == "verifier"
        assert all(item["attempt_id"] != "legacy-unbound" for item in attempts)

        limited = route(
            make_env(
                Method.CONTRACT_GET,
                "req-decision-get-limited",
                {"contract_id": cid, "attempt_limit": 1},
            ),
            conn=conn,
            now=NOW,
        )
        assert len(limited["result"]["attempt_history"]) == 1
    finally:
        conn.close()


def test_contract_get_exposes_latest_deadline_snapshot(tmp_path: Path) -> None:
    """合同查询直接提供最新风险快照，模型无需重放全量事件。"""
    conn = connect(setup_test_db(tmp_path))
    try:
        cid = "lt-20260831-deadline-snapshot"
        route(
            make_env(
                Method.CONTRACT_PREPARE,
                "req-snapshot-prep",
                {"contract_id": cid, "draft": make_valid_draft_payload()},
            ),
            conn=conn,
            now=NOW,
        )
        for index in range(205):
            append_event(
                conn,
                contract_id=cid,
                event_type=EventType.FORECAST_UPDATED,
                payload={"risk": "yellow", "sequence": index},
                now=NOW + timedelta(seconds=index),
                actor="promoter",
            )
        append_event(
            conn,
            contract_id=cid,
            event_type=EventType.FORECAST_UPDATED,
            payload={"risk": "orange", "forecast_p90_minutes": 42.0},
            now=NOW + timedelta(seconds=205),
            actor="promoter",
        )
        result = route(
            make_env(Method.CONTRACT_GET, "req-snapshot-get", {"contract_id": cid}),
            conn=conn,
            now=NOW,
        )
        assert result["result"]["deadline_snapshot"] == {
            "risk": "orange",
            "forecast_p90_minutes": 42.0,
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision_limit", 201),
        ("attempt_limit", 101),
        ("decision_limit", 0),
        ("attempt_limit", True),
        ("attempt_limit", 1.5),
    ],
)
def test_contract_get_rejects_history_limit_out_of_range(
    tmp_path: Path, field: str, value: int
) -> None:
    conn = connect(setup_test_db(tmp_path))
    try:
        cid = "lt-20260831-limit-validation"
        route(
            make_env(
                Method.CONTRACT_PREPARE,
                "req-limit-prep",
                {"contract_id": cid, "draft": make_valid_draft_payload()},
            ),
            conn=conn,
            now=NOW,
        )
        with pytest.raises(RpcError) as exc:
            route(
                make_env(
                    Method.CONTRACT_GET,
                    f"req-limit-get-{field}-{value}",
                    {"contract_id": cid, field: value},
                ),
                conn=conn,
                now=NOW,
            )
        assert exc.value.code is ErrorCode.VALIDATION_FAILED
    finally:
        conn.close()
