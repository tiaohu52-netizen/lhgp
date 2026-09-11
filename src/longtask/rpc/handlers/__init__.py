"""JSON-RPC 控制面方法分发表（DESIGN §11.2、§11.3、§11.7）。

执行者侧方法（attempt/*、lease/renew、control/interrupt）见 rpc.executor_api。
拆分结构：
- contract/*  → handlers.contract
- executor/*  → handlers.executor
- protocol/*  → handlers.protocol
- _common     → resolve_actor / parse_contract_draft / require_contract_id /
                idempotent_replay（P1 修复重复模式）

本 __init__ 只做三件事：(1) 暴露公共 handler 给测试与 route 模块；
(2) 装配 HANDLERS 字典；(3) 暴露 _common helpers 给需要直接调用的调用方
（legacy path 与 future custom router）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from longtask.rpc.handlers import contract, executor, goal, protocol
from longtask.rpc.handlers._common import (
    idempotent_replay,
    parse_contract_draft,
    require_contract_id,
    resolve_actor,
)
from longtask.rpc.methods import Method

__all__ = [
    "HANDLERS",
    "contract",
    "executor",
    "idempotent_replay",
    "parse_contract_draft",
    "protocol",
    "require_contract_id",
    "resolve_actor",
]


def _build_handlers() -> dict[Method, Callable[..., dict[str, Any]]]:
    """构造方法→handler 分发表。

    执行者侧方法（attempt/status, attempt/logs, attempt/write-back,
    lease/renew, control/interrupt）由 rpc.executor_api 提供。
    """
    # 局部 import 避免循环（executor_api import handlers.contract.* 不需要）
    from longtask.rpc.executor_api import (
        handle_attempt_status,
        handle_attempt_write_back,
        handle_control_interrupt,
        handle_lease_renew,
    )

    return {
        Method.PROTOCOL_HELLO: protocol.handle_protocol_hello,
        Method.PROTOCOL_EVENTS: protocol.handle_protocol_events,
        Method.DAEMON_WAKE: protocol.handle_daemon_wake,
        Method.CONTRACT_PREPARE: contract.handle_contract_prepare,
        Method.CONTRACT_APPROVE: contract.handle_contract_approve,
        Method.CONTRACT_GET: contract.handle_contract_get,
        Method.CONTRACT_LIST: contract.handle_contract_list,
        Method.CONTRACT_PATCH: contract.handle_contract_patch,
        Method.CONTRACT_PAUSE: contract.handle_contract_pause,
        Method.CONTRACT_RESUME: contract.handle_contract_resume,
        Method.CONTRACT_CANCEL: contract.handle_contract_cancel,
        Method.CONTRACT_ARBITRATE: contract.handle_contract_arbitrate,
        Method.CONTRACT_REQUEST_VERIFICATION: contract.handle_contract_request_verification,
        Method.CONTRACT_AUTO_APPROVE: contract.handle_contract_auto_approve,
        Method.CONTRACT_USER_CONFIRM: contract.handle_contract_user_confirm,
        Method.GOAL_PREPARE: goal.handle_goal_prepare,
        Method.GOAL_ADMISSION_CHECK: goal.handle_goal_admission_check,
        Method.GOAL_GET: goal.handle_goal_get,
        Method.GOAL_LIST: goal.handle_goal_list,
        Method.GOAL_UPDATE: goal.handle_goal_update,
        Method.GOAL_ADVANCE: goal.handle_goal_advance,
        Method.GOAL_NEXT: goal.handle_goal_next,
        Method.GOAL_CONTRACT_DRAFT: goal.handle_goal_contract_draft,
        Method.EXECUTOR_LIST: executor.handle_executor_list,
        Method.EXECUTOR_ENABLE: executor.handle_executor_enable,
        Method.EXECUTOR_DISABLE: executor.handle_executor_disable,
        Method.EXECUTOR_HEALTH: executor.handle_executor_health,
        # 执行者侧（DESIGN §11.2）
        Method.ATTEMPT_STATUS: handle_attempt_status,
        Method.ATTEMPT_LOGS: handle_attempt_status,
        Method.ATTEMPT_WRITE_BACK: handle_attempt_write_back,
        Method.LEASE_RENEW: handle_lease_renew,
        Method.CONTROL_INTERRUPT: handle_control_interrupt,
    }


HANDLERS: dict[Method, Callable[..., dict[str, Any]]] = _build_handlers()
