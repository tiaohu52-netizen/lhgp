"""Canonical LHGP RPC method registry.

Handlers are assembled here so the canonical server routes through canonical
module paths while legacy imports remain available as compatibility facades.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lhgp.rpc.handlers import contract, executor, goal, protocol
from lhgp.rpc.handlers._common import (
    idempotent_replay,
    parse_contract_draft,
    require_contract_id,
    resolve_actor,
)
from lhgp.rpc.methods import Method

__all__ = [
    "HANDLERS",
    "contract",
    "executor",
    "goal",
    "idempotent_replay",
    "parse_contract_draft",
    "protocol",
    "require_contract_id",
    "resolve_actor",
]


from longtask.rpc.handlers import contract as _longtask_contract


def _build_handlers() -> dict[Method, Callable[..., dict[str, Any]]]:
    """构造方法到 handler 的 canonical 分发表。"""
    # executor_api 尚未完成物理迁移，暂时通过兼容入口接入。
    from lhgp.rpc.executor_api import (
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
        Method.CONTRACT_USER_CONFIRM: _longtask_contract.handle_contract_user_confirm,
        Method.CONTRACT_AUTO_APPROVE: _longtask_contract.handle_contract_auto_approve,
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
        Method.ATTEMPT_STATUS: handle_attempt_status,
        Method.ATTEMPT_LOGS: handle_attempt_status,
        Method.ATTEMPT_WRITE_BACK: handle_attempt_write_back,
        Method.LEASE_RENEW: handle_lease_renew,
        Method.CONTROL_INTERRUPT: handle_control_interrupt,
    }


HANDLERS: dict[Method, Callable[..., dict[str, Any]]] = _build_handlers()
