"""分层唤醒（DESIGN §6.4、ADR-0002）：L0 电源守卫 + L1 计划任务唤醒。

核心不变式（ADR-0002）：唤醒源永远不是权威——外部唤醒只能「推」
（通知 + 唤醒信号），不能读合同内容、不能仲裁、不能写状态；仲裁仍只
发生在 longtaskd 醒来后的首轮扫描（仲裁时刻语义不变）。

实现范围（与 claims 注册表如实对齐）：
- L0 SleepGuard：active 租约存活或 u ≥ 1.0 时持有系统电源请求
  （Windows SetThreadExecutionState），事件 wakeup/sleep-guard；
- L1 RtcAlarm：为 active 合同注册带 wake 标志的计划任务
  （取 next_wakeup_at、next_decision_at 与 deadline 安全边距中的最早时刻），事件
  wakeup/rtc-armed / wakeup/rtc-fired；
- 任一层失效：记 wakeup/degraded 事件并降级声明（§11.4），绝不静默。
  L2（云侧准时通知）与 L3（常在线中继）依赖外部基础设施，
  本参考实现不部署，strict_deadline 的 notify_only/notify_and_wake
  相应能力如实报 degraded（fail-closed，不假装 strict）。

所有系统交互（电源 API、计划任务程序）经可注入端口，测试零外部副作用。
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from xml.sax.saxutils import escape as xml_escape

from longtask.persistence.events import EventType
from longtask.persistence.store import append_event, get_lease, list_contracts

# ES_CONTINUOUS | ES_SYSTEM_REQUIRED：持续持有「系统勿睡」请求
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# L1 安全边距：deadline 前这么久就注册唤醒，给仲裁与补跑留余量
DEFAULT_SAFETY_MARGIN = timedelta(minutes=30)


class PowerPort(Protocol):
    """电源请求端口（L0）：acquire 返回句柄，release 归还。"""

    def acquire(self, reason: str) -> Any: ...

    def release(self, handle: Any) -> None: ...


class SchedulePort(Protocol):
    """计划任务端口（L1）：注册/注销带 wake 标志的一次性定时任务。"""

    def arm(self, task_id: str, at: datetime) -> None: ...

    def disarm(self, task_id: str) -> None: ...

    def is_available(self) -> bool: ...


class NullSchedulePort:
    """L1 空通道：平台无计划任务唤醒可用时如实报不可用（记 degraded）。"""

    def is_available(self) -> bool:
        return False

    def arm(self, task_id: str, at: datetime) -> None:
        raise OSError(f"schedule port unavailable: cannot arm {task_id}")

    def disarm(self, task_id: str) -> None:
        raise OSError(f"schedule port unavailable: cannot disarm {task_id}")


class WindowsTaskSchedulerPort:
    """Windows Task Scheduler 的一次性唤醒端口（L1）。

    任务动作只调用本机认证 RPC ``daemon/wake``，不直接读取或修改合同；
    daemon 仍是唯一仲裁者。所有 ``schtasks.exe`` 参数以 argv 传入，避免
    shell 解释用户提供的合同 ID。``schtasks`` 只有分钟精度，因此带秒的
    目标一律向下取整，确保唤醒不晚于 Deadline 安全边界；提前唤醒由
    daemon 的事件幂等性吸收。
    """

    _TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}\Z")

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    def is_available(self) -> bool:
        """仅在 Windows 且系统能找到 schtasks.exe 时报告可用。"""
        return sys.platform == "win32" and shutil.which("schtasks.exe") is not None

    @classmethod
    def _task_name(cls, task_id: str) -> str:
        if not isinstance(task_id, str) or not cls._TASK_ID_RE.fullmatch(task_id):
            raise ValueError("task_id must contain only ASCII letters, digits, '.', '_' or '-'")
        return rf"\LHGP\{task_id}"

    @staticmethod
    def _schedule_time(at: datetime) -> tuple[str, str]:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("scheduled wakeup time must include a timezone")
        local = at.astimezone()
        local = local.replace(second=0, microsecond=0)
        # Windows 11 的 schtasks 解析器要求 yyyy/mm/dd，即使系统区域设置
        # 使用其他日期显示格式；固定该格式避免本地化导致 arm 失败。
        return local.strftime("%H:%M"), local.strftime("%Y/%m/%d")

    def _wake_command(self, task_id: str) -> str:
        return subprocess.list2cmdline(self._wake_argv(task_id))

    def _wake_argv(self, task_id: str) -> list[str]:
        params = json.dumps({"task_id": task_id}, separators=(",", ":"))
        params_b64 = base64.urlsafe_b64encode(params.encode("utf-8")).decode("ascii").rstrip("=")
        return [
            sys.executable,
            "-m",
            "longtask.cli.main",
            "--data-dir",
            str(self._root),
            "rpc-call",
            "daemon/wake",
            "--client-id",
            "daemon-wakeup",
            "--params-b64",
            params_b64,
        ]

    def _task_xml(self, task_id: str, at: datetime) -> str:
        """生成允许唤醒/电池运行的 Task Scheduler XML。"""
        local = at.astimezone()
        local = local.replace(second=0, microsecond=0, tzinfo=None)
        argv = self._wake_argv(task_id)
        command = xml_escape(argv[0])
        arguments = xml_escape(subprocess.list2cmdline(argv[1:]))
        working_directory = xml_escape(str(self._root))
        author = xml_escape(getpass.getuser())
        boundary = local.isoformat(timespec="seconds")
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" version="1.4">
  <RegistrationInfo>
    <Author>{author}</Author><Description>LHGP local deadline wakeup</Description>
  </RegistrationInfo>
  <Triggers><TimeTrigger><StartBoundary>{boundary}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{command}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{working_directory}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""

    @staticmethod
    def _run(args: list[str]) -> None:
        try:
            completed = subprocess.run(  # noqa: S603 — fixed schtasks argv, shell disabled
                args,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise OSError(f"failed to invoke schtasks.exe: {exc}") from exc
        if completed.returncode == 0:
            return
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise OSError(f"schtasks.exe failed ({completed.returncode}): {detail}")

    def arm(self, task_id: str, at: datetime) -> None:
        task_name = self._task_name(task_id)
        fd, raw_path = tempfile.mkstemp(prefix="lhgp-task-", suffix=".xml")
        os.close(fd)
        xml_path = Path(raw_path)
        try:
            xml_path.write_text(self._task_xml(task_id, at), encoding="utf-16")
            self._run(
                [
                    "schtasks.exe",
                    "/Create",
                    "/TN",
                    task_name,
                    "/XML",
                    str(xml_path),
                    "/F",
                ]
            )
        finally:
            with suppress(FileNotFoundError):
                xml_path.unlink()

    def disarm(self, task_id: str) -> None:
        task_name = self._task_name(task_id)
        try:
            self._run(["schtasks.exe", "/Delete", "/TN", task_name, "/F"])
        except OSError as exc:
            # 删除本来就不存在的任务是幂等成功；权限、参数和系统错误仍必须暴露。
            detail = str(exc).lower()
            if not any(
                marker in detail for marker in ("does not exist", "cannot find", "not found")
            ):
                raise


def default_schedule_port(root: Path) -> SchedulePort:
    """返回本机可用的 L1 端口；不可用时显式降级为 NullSchedulePort。"""
    port = WindowsTaskSchedulerPort(root)
    return port if port.is_available() else NullSchedulePort()


class WindowsPowerPort:
    """SetThreadExecutionState 实现（仅 Windows；其他平台如实报不可用）。"""

    def __init__(self) -> None:
        import sys

        self._available = sys.platform == "win32"

    def is_available(self) -> bool:
        return self._available

    def acquire(self, reason: str) -> Any:
        if not self._available:
            raise OSError("SetThreadExecutionState is Windows-only")
        import ctypes

        # 返回值非 0 即持有成功；句柄语义由 EXECUTION_STATE 值本身承载
        state = ctypes.c_uint(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        result = ctypes.windll.kernel32.SetThreadExecutionState(state)
        if not result:
            raise OSError("SetThreadExecutionState failed")
        return result

    def release(self, handle: Any) -> None:
        if not self._available:
            return
        import ctypes

        # 归还 INFINITE 恢复默认；失败再抛（调用方记 degraded）
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ctypes.c_uint(0x80000000)  # ES_CONTINUOUS
        )
        if not result:
            raise OSError("SetThreadExecutionState release failed")


@dataclass(frozen=True, slots=True)
class WakeupDecision:
    """一轮 tick 的唤醒决策结果（audit 用）。"""

    guard_held: bool
    armed_contracts: tuple[str, ...]
    degraded_layers: tuple[str, ...]


class SleepGuard:
    """L0 电源守卫：按「active 租约存活或 u ≥ 1.0」持有电源请求。

    状态转换即事件（挂在触发守卫的合同上，进该合同的 log.jsonl 审计）：
    持有记 wakeup/sleep-guard(held=true)，释放记 held=false；
    acquire/release 失败记 wakeup/degraded(layer=L0)。
    """

    def __init__(self, power: PowerPort) -> None:
        self._power = power
        self._handle: Any = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def update(
        self,
        conn: sqlite3.Connection,
        *,
        now: datetime,
        guard_needed: bool,
        reason: str,
        contract_id: str,
    ) -> bool:
        """按需持有/释放；返回当前持有状态。失败降级记事件，不抛出。"""
        if guard_needed and self._handle is None:
            try:
                self._handle = self._power.acquire(reason)
                append_event(
                    conn,
                    contract_id=contract_id,
                    event_type=EventType.WAKEUP_SLEEP_GUARD,
                    payload={"held": True, "reason": reason},
                    now=now,
                    actor="daemon",
                )
            except OSError as exc:
                append_event(
                    conn,
                    contract_id=contract_id,
                    event_type=EventType.WAKEUP_DEGRADED,
                    payload={"layer": "L0", "reason": str(exc)},
                    now=now,
                    actor="daemon",
                )
        elif not guard_needed and self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                self._power.release(handle)
                append_event(
                    conn,
                    contract_id=contract_id,
                    event_type=EventType.WAKEUP_SLEEP_GUARD,
                    payload={"held": False, "reason": reason},
                    now=now,
                    actor="daemon",
                )
            except OSError as exc:
                append_event(
                    conn,
                    contract_id=contract_id,
                    event_type=EventType.WAKEUP_DEGRADED,
                    payload={"layer": "L0", "reason": str(exc)},
                    now=now,
                    actor="daemon",
                )
        return self._handle is not None


class RtcAlarm:
    """L1 计划任务唤醒：为 active 合同注册带 wake 位的一次性任务。

    每个 active 合同一个 task_id（longtask-wakeup-<cid>）；目标时刻取
    next_wakeup_at、next_decision_at 与 deadline_at - safety_margin 中的最早值。
    任一时刻到达都必须唤醒 daemon，不能让较晚的 deadline 安全边距遮蔽更早的
    决策点。注册/注销失败记 wakeup/degraded(layer=L1)，绝不静默假装已注册。
    """

    def __init__(self, schedule: SchedulePort, safety_margin: timedelta = DEFAULT_SAFETY_MARGIN):
        self._schedule = schedule
        self._safety_margin = safety_margin
        self._armed: dict[str, datetime] = {}
        self._degraded_reported = False
        self._degraded_contracts: set[str] = set()

    def note_fired(self, task_id: str) -> bool:
        """消费一次性任务的 fired 信号，使下一轮能按新决策点重新登记。"""
        prefix = "longtask-wakeup-"
        if not isinstance(task_id, str) or not task_id.startswith(prefix):
            return False
        contract_id = task_id[len(prefix) :]
        if not contract_id:
            return False
        return self._armed.pop(contract_id, None) is not None

    def refresh(
        self,
        conn: sqlite3.Connection,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """对齐 active 合同与已注册任务集；返回当前 armed 的合同 id 元组。"""
        if not self._schedule.is_available():
            # 不可用状态可能持续数小时；每个 tick 重复写一条 degraded
            # 会制造事件噪声并掩盖真正的风险。状态恢复后允许再次报告。
            if not self._degraded_reported:
                append_event(
                    conn,
                    contract_id=None,
                    event_type=EventType.WAKEUP_DEGRADED,
                    payload={"layer": "L1", "reason": "schedule port unavailable on this platform"},
                    now=now,
                    actor="daemon",
                )
                self._degraded_reported = True
            self._armed.clear()
            return ()

        self._degraded_reported = False

        from longtask.contracts.schema import ContractState

        targets: dict[str, datetime] = {}
        for contract in list_contracts(conn, limit=1000):
            if contract.state != ContractState.ACTIVE:
                continue
            cid = contract.contract_id
            wakeup = contract.next_wakeup_at
            deadline_minus_margin = contract.draft.deadline_at - self._safety_margin
            # 每个候选点都代表一次必须重新审视的最早时刻；取 min，
            # 否则较晚的 deadline 安全边距会推迟更早的决策/唤醒。
            candidates = [
                t
                for t in (wakeup, contract.next_decision_at, deadline_minus_margin)
                if t is not None
            ]
            if not candidates:
                continue
            # 过去的决策点代表“立即重算”，不能把过去时间交给平台调度器；
            # 统一钳制到当前时刻，让可用的 L1 端口执行一次即时唤醒。
            targets[cid] = max(now, min(candidates))

        # 目标时刻已变或新出现的合同：重新注册
        for cid, at in targets.items():
            if self._armed.get(cid) != at:
                task_id = f"longtask-wakeup-{cid}"
                try:
                    self._schedule.arm(task_id, at)
                    self._armed[cid] = at
                    self._degraded_contracts.discard(cid)
                    append_event(
                        conn,
                        contract_id=cid,
                        event_type=EventType.WAKEUP_RTC_ARMED,
                        payload={"task_id": task_id, "at": at.isoformat()},
                        now=now,
                        actor="daemon",
                    )
                except OSError as exc:
                    if cid not in self._degraded_contracts:
                        append_event(
                            conn,
                            contract_id=cid,
                            event_type=EventType.WAKEUP_DEGRADED,
                            payload={"layer": "L1", "reason": str(exc)},
                            now=now,
                            actor="daemon",
                        )
                        self._degraded_contracts.add(cid)

        # 终态/消失的合同：注销
        for cid in list(self._armed):
            if cid not in targets:
                try:
                    self._schedule.disarm(f"longtask-wakeup-{cid}")
                    # 只有注销成功才移除内存登记；失败时保留，下一轮重试，
                    # 避免系统计划任务残留而协议误以为已清理。
                    del self._armed[cid]
                    self._degraded_contracts.discard(cid)
                except OSError as exc:
                    if cid not in self._degraded_contracts:
                        append_event(
                            conn,
                            contract_id=cid,
                            event_type=EventType.WAKEUP_DEGRADED,
                            payload={"layer": "L1", "reason": str(exc)},
                            now=now,
                            actor="daemon",
                        )
                        self._degraded_contracts.add(cid)
        return tuple(sorted(self._armed))


def guard_needed(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    urgency_threshold: float = 1.0,
) -> tuple[bool, str | None]:
    """L0 持有条件（ADR-0002）：任一合同 active 租约存活，或 u ≥ 阈值。

    返回 (needed, 触发守卫的合同 id)：事件挂到触发合同上做审计。
    """
    from longtask.contracts.schema import ContractState

    for contract in list_contracts(conn, limit=1000):
        if contract.state != ContractState.ACTIVE:
            continue
        lease = get_lease(conn, contract.contract_id)
        if lease is not None and lease.is_alive(now):
            return True, contract.contract_id
        # u = 剩余工作量 / 剩余时间；越 deadline 视为无穷紧迫，同样需要守卫
        time_left_hours = (contract.draft.deadline_at - now).total_seconds() / 3600.0
        if time_left_hours <= 0:
            return True, contract.contract_id
        if contract.draft.workload_initial_hours / time_left_hours >= urgency_threshold:
            return True, contract.contract_id
    return False, None


__all__ = [
    "DEFAULT_SAFETY_MARGIN",
    "NullSchedulePort",
    "PowerPort",
    "RtcAlarm",
    "SchedulePort",
    "SleepGuard",
    "WakeupDecision",
    "WindowsPowerPort",
    "guard_needed",
]
