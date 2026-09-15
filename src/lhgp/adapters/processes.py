"""Process identity and liveness probes for restart-safe adapters.

三平台实现（审计遗留的 macOS 身份模型适配）：
- Windows: OpenProcess + GetExitCodeProcess / GetProcessTimes；
- Linux: /proc/<pid>/stat（状态判活含僵尸 + 字段 22 启动时刻）；
- macOS: libproc.proc_pidinfo(PROC_PIDTBSDINFO)——pbi_status 判活含
  SZOMB，pbi_start_tvsec/tvusec 给出与 Linux 同精度的稳定启动时刻。

另外提供**进程树终止**（``terminate_tree``）：harness 普遍是「主进程拉起
worker」的两级结构，只杀直接子进程会把 worker 留成孤儿继续写工作区——
对合同来说这是「已取消却还在改盘」，比取消失败更难查。
"""

from __future__ import annotations

import os
import sys

IDENTITY_TOLERANCE_SECONDS = 2.0

# 进程树终止的等待上限：taskkill 在大树上可能需要一点时间，但不能无限等。
TREE_KILL_TIMEOUT_SECONDS = 10.0


def taskkill_argv(executable: str, pid: int, *, force: bool) -> list[str]:
    """Windows 树终止的 argv（纯函数，便于在任意平台单测锁住形态）。

    ``/T`` 连子孙一起终止；``/F`` 强杀。不加 ``/F`` 时 taskkill 发的是
    可被目标处理的关闭请求，给了 harness 收尾的机会。
    """
    argv = [executable, "/T"]
    if force:
        argv.append("/F")
    argv += ["/PID", str(pid)]
    return argv


def safe_process_group(pid: int) -> int | None:
    """返回可以安全 ``killpg`` 的进程组号；不确定就返回 ``None``。

    只有「该进程自己就是组长」时才安全（spawn 时的 ``start_new_session``
    保证了这一点）。否则 ``getpgid`` 返回的可能是**推动者自己所在的组**，
    killpg 会把守护进程连坐杀掉——这是本模块最不能出的错，所以判定条件
    取 ``group == pid`` 而不是「和我不在同一组」。

    这里是默认实现（Windows：没有 POSIX 进程组，恒 ``None``，取消走
    ``taskkill /T``）；POSIX 分支各自覆盖它。
    """
    return None


if sys.platform == "win32":  # pragma: no cover - platform-specific branch
    import ctypes
    import shutil
    import subprocess
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _EPOCH_AS_FILETIME = 11644473600.0

    def _filetime_to_epoch(ft: wintypes.FILETIME) -> float:
        value = (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)
        return value / 1e7 - _EPOCH_AS_FILETIME

    def _open(pid: int, access: int) -> int:
        ctypes.set_last_error(0)
        return int(_kernel32.OpenProcess(access, False, pid))

    def process_start_time(pid: int) -> float | None:
        if pid <= 0:
            return None
        handle = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return _filetime_to_epoch(creation)
        finally:
            _kernel32.CloseHandle(handle)

    def process_alive(pid: int) -> bool | None:
        if pid <= 0:
            return None
        handle = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            if ctypes.get_last_error() == _ERROR_ACCESS_DENIED:
                return None
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return int(code.value) == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    def terminate_pid(pid: int) -> bool:
        if pid <= 0:
            return False
        handle = _open(pid, _PROCESS_TERMINATE)
        if not handle:
            return False
        try:
            return bool(_kernel32.TerminateProcess(handle, 1))
        finally:
            _kernel32.CloseHandle(handle)

    def terminate_tree(pid: int, *, force: bool = False) -> bool:
        """终止整棵进程树（Windows）。

        用 ``taskkill /T`` 按父子关系遍历，先子后父。它按快照遍历，因此
        「遍历开始后才 new 出来的曾孙」可能被漏掉——这是本实现的诚实边界，
        不做「保证全灭」的声明。

        Windows 上**没有可用的「礼貌的树终止」**：``taskkill`` 不带 ``/F``
        只能给 GUI 目标发关闭请求，控制台 harness 一律回「只能被强制终止」。
        所以 ``force=False`` 是先试一次不带 ``/F``（对 GUI 目标有效），
        **一失败立刻升级 ``/F``**——那个失败是立即且可判定的，不是「再等等看」，
        在这里偷懒会把树杀变成只杀直接子进程。两条都不可用时退回单进程
        ``TerminateProcess``，绝不静默变成 no-op。
        """
        if pid <= 0:
            return False
        executable = shutil.which("taskkill")
        if executable is not None:
            if not force and _run_taskkill(executable, pid, force=False):
                return True
            if _run_taskkill(executable, pid, force=True):
                return True
        return terminate_pid(pid)

    def _run_taskkill(executable: str, pid: int, *, force: bool) -> bool:
        completed: subprocess.CompletedProcess[bytes] | None
        try:
            completed = subprocess.run(  # noqa: S603 — 固定 argv + 解析出的绝对路径
                taskkill_argv(executable, pid, force=force),
                capture_output=True,
                timeout=TREE_KILL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        return completed is not None and completed.returncode == 0

elif sys.platform == "darwin":  # pragma: no cover - exercised on macOS CI
    import signal
    import subprocess
    import time as _time

    # macOS 无 /proc；libproc 结构体偏移随架构/版本有漂移风险，这里改用
    # /bin/ps 的机器可读字段（etimes/state）——无偏移依赖、全版本一致，
    # 且 tick 频率下的子进程开销可接受（每次 <15ms）。

    def _ps_value(pid: int, keyword: str) -> str | None:
        """ps -p pid -o <keyword>=：进程不存在返回 None，其余原样去空白。"""
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, pid is an int
                ["/bin/ps", "-p", str(pid), "-o", keyword + "="],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _etime_to_seconds(text: str) -> int | None:
        """解析 BSD ps 的 etime：[[dd-]hh:]mm:ss。"""
        try:
            days = 0
            rest = text
            if "-" in rest:
                day_part, rest = rest.split("-", 1)
                days = int(day_part)
            total = 0
            for part in rest.split(":"):
                total = total * 60 + int(part)
            return days * 86400 + total
        except ValueError:
            return None

    def process_start_time(pid: int) -> float | None:
        """Epoch 启动时刻 = now - etime；进程退出后 ps 不再列出 → None。

        macOS 的 BSD ps 没有 Linux 的 etimes 关键字——只有 etime
        （[[dd-]hh:]mm:ss 格式），这里做解析。
        """
        if pid <= 0:
            return None
        etime = _ps_value(pid, "etime")
        if etime is None:
            return None
        seconds = _etime_to_seconds(etime)
        if seconds is None or seconds < 0:
            return None
        return _time.time() - float(seconds)

    def process_alive(pid: int) -> bool | None:
        if pid <= 0:
            return None
        state = _ps_value(pid, "state")
        if state is None:
            # ps 不在本机进程表里看到它：可能刚被收尸
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except (PermissionError, OSError):
                return None
            # ps 看不到但 kill 探到活进程：不可判定（权限/边界）
            return None
        # BSD state 含 Z 即 zombie：已退出未收尸（macOS 上 kill(pid,0)
        # 同样把僵尸当活进程，这里显式排除）
        return "Z" not in state

    def terminate_pid(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True

    def safe_process_group(pid: int) -> int | None:
        """POSIX：仅当该进程自己是组长时返回组号，否则 None（见模块说明）。"""
        if pid <= 0:
            return None
        try:
            group = os.getpgid(pid)
        except OSError:
            return None
        return group if group == pid else None

    def terminate_tree(pid: int, *, force: bool = False) -> bool:
        """终止整个进程组（macOS）。

        spawn 时用了 ``start_new_session``，所以子进程自成一组的组长；
        组内所有成员（harness 的 worker、worker 再拉起的孙进程）一次信号
        全覆盖。不是组长时退回单进程终止，绝不 killpg——那可能打到推动者
        自己所在的组。
        """
        if pid <= 0:
            return False
        group = safe_process_group(pid)
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL if force else signal.SIGTERM)
            except OSError:
                pass
            else:
                return True
        return terminate_pid(pid)


else:  # pragma: no cover - exercised on Linux CI
    import signal
    from pathlib import Path

    def _read_proc_stat(pid: int) -> list[str] | None:
        """Fields of /proc/<pid>/stat after comm, or None without /proc.

        comm may contain spaces and parentheses, so parse after the final ')'
        rather than splitting the whole line.
        """
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        return stat_line.rpartition(")")[2].split() or None

    def _boot_time_epoch() -> float | None:
        try:
            for stat_line in (
                Path("/proc/stat").read_text(encoding="ascii", errors="replace").splitlines()
            ):
                if stat_line.startswith("btime "):
                    return float(stat_line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    def process_start_time(pid: int) -> float | None:
        """Epoch start time from /proc field 22 (stable after process exit).

        st_ctime is not a start-time proxy: it changes when the process exits
        or is reaped, breaking identity checks for dead-but-unreaped runs.
        """
        if pid <= 0:
            return None
        boot = _boot_time_epoch()
        fields = _read_proc_stat(pid)
        if boot is None or not fields or len(fields) < 20:
            return None
        try:
            clk_tck = float(os.sysconf("SC_CLK_TCK"))
        except (ValueError, OSError):
            clk_tck = 100.0
        try:
            ticks = float(fields[19])
        except ValueError:
            return None
        return boot + ticks / clk_tck

    def process_alive(pid: int) -> bool | None:
        if pid <= 0:
            return None
        fields = _read_proc_stat(pid)
        if fields:
            # kill(pid, 0) succeeds on zombies, so without this check an
            # exited-but-unreaped detached run looks alive forever on Linux.
            return fields[0] != "Z"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        except OSError:
            return None
        return True

    def terminate_pid(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True

    def safe_process_group(pid: int) -> int | None:
        """POSIX：仅当该进程自己是组长时返回组号，否则 None（见模块说明）。"""
        if pid <= 0:
            return None
        try:
            group = os.getpgid(pid)
        except OSError:
            return None
        return group if group == pid else None

    def terminate_tree(pid: int, *, force: bool = False) -> bool:
        """终止整个进程组（Linux）。

        与 macOS 同款：spawn 用 ``start_new_session`` 让子进程当组长，
        组内一次信号全覆盖；不是组长则退回单进程终止，绝不 killpg。
        """
        if pid <= 0:
            return False
        group = safe_process_group(pid)
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL if force else signal.SIGTERM)
            except OSError:
                pass
            else:
                return True
        return terminate_pid(pid)


def identity_matches(pid: int, recorded_start_time: float | None) -> bool | None:
    """Confirm PID and recorded start time refer to the same process."""
    if pid <= 0 or recorded_start_time is None:
        return None
    actual = process_start_time(pid)
    if actual is None:
        return None
    return abs(actual - float(recorded_start_time)) <= IDENTITY_TOLERANCE_SECONDS


__all__ = [
    "IDENTITY_TOLERANCE_SECONDS",
    "TREE_KILL_TIMEOUT_SECONDS",
    "identity_matches",
    "process_alive",
    "process_start_time",
    "safe_process_group",
    "taskkill_argv",
    "terminate_pid",
    "terminate_tree",
]
