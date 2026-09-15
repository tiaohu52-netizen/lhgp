"""longtaskd 守护进程与调度驱动核心（DESIGN §3.3、§6.2、§8.3、§10、§15.2）。

本模块是兼容门面：实现已按职责拆分到
- promoter/killswitch.py —— 全局 Kill Switch 判定（§15.2）
- promoter/records.py    —— attempts/decisions 簿记与预算判定
- cli/dispatch.py        —— 逐候选执行器分发（§8.3、§9、§10 时序）
- cli/tick.py            —— 单次调度推进轮次 run_daemon_tick
- cli/daemon_proc.py     —— 进程生命周期（spawn/halt/status）
- cli/daemon_loop.py     —— 常驻主循环与 control/interrupt 消费

对外 import 路径保持 longtask.cli.daemon 不变。
"""

from __future__ import annotations

from longtask.cli.daemon_loop import (
    _cancel_terminal_contract_attempts,
    _consume_interrupt_requests,
    run_daemon_loop,
)
from longtask.cli.daemon_proc import (
    DAEMON_LOG_FILE,
    DAEMON_STOP_FILE,
    DEFAULT_TICK_INTERVAL_SECONDS,
    PID_FILE,
    REGISTRY_FILE,
    RPC_SOCKET_FILE,
    STOP_GRACE_SECONDS,
    TOKEN_FILE,
    get_daemon_status,
    halt_daemon,
    rpc_socket_path,
    spawn_daemon,
)
from longtask.cli.tick import run_daemon_tick
from longtask.promoter.killswitch import KILL_SWITCH_FILE, is_kill_switch_active, set_kill_switch

__all__ = [
    "DAEMON_LOG_FILE",
    "DAEMON_STOP_FILE",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "KILL_SWITCH_FILE",
    "PID_FILE",
    "REGISTRY_FILE",
    "RPC_SOCKET_FILE",
    "STOP_GRACE_SECONDS",
    "TOKEN_FILE",
    "_cancel_terminal_contract_attempts",
    "_consume_interrupt_requests",
    "get_daemon_status",
    "halt_daemon",
    "is_kill_switch_active",
    "rpc_socket_path",
    "run_daemon_loop",
    "run_daemon_tick",
    "set_kill_switch",
    "spawn_daemon",
]
