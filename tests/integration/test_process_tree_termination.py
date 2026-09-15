"""真进程的两级树终止：取消不能只收主进程（DESIGN §7 取消语义）。

harness 普遍是「主进程拉起 worker」的两级结构。只杀直接子进程会把 worker
留成孤儿继续写工作区——对合同来说这是「已取消却还在改盘」：没有事件、
没有报错，只有盘上多出来的改动。本测试真起两级进程，断言孙进程确实死了。

判定用「pid + 启动时间」双重比对而不是裸 pid：Windows 的 pid 会被复用，
只比对 pid 会把「孙进程已死、pid 被别人接管」误判成还活着。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from lhgp.adapters.processes import (
    identity_matches,
    process_alive,
    process_start_time,
    terminate_tree,
)
from tests.wait_budget import budget

# 父进程拉起孙进程，把孙进程 pid 落盘（不读管道：pytest 里读管道阻塞风险高），
# 然后两者都挂住等被杀。
_TREE_SCRIPT = (
    "import pathlib, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(900)'])\n"
    "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
    "time.sleep(900)\n"
)


def _await_grandchild_pid(pid_file: Path) -> int:
    """在预算内等孙进程 pid 落盘（轮询而非定时长睡，落盘即返回）。"""
    deadline = time.monotonic() + budget(30.0)
    while time.monotonic() < deadline:
        if pid_file.is_file():
            raw = pid_file.read_text(encoding="ascii").strip()
            if raw:
                return int(raw)
        time.sleep(0.02)
    raise AssertionError(f"孙进程 pid 未在预算内落盘: {pid_file}")


def _await_process_gone(pid: int, start_time: float) -> bool:
    """在预算内等「同一个进程」消失；pid 被复用不算同一进程。"""
    deadline = time.monotonic() + budget(30.0)
    while time.monotonic() < deadline:
        if identity_matches(pid, start_time) is not True:
            return True
        time.sleep(0.02)
    return False


def test_terminate_tree_kills_the_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    parent = subprocess.Popen(  # noqa: S603 — 测试内固定 argv，无外部输入
        [sys.executable, "-c", _TREE_SCRIPT, str(pid_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # 与 SubprocessAdapter.spawn 一致：POSIX 下子进程自成组长，
        # 否则 terminate_tree 只能退回单进程终止（见 safe_process_group）。
        start_new_session=os.name == "posix",
    )
    grandchild_pid = 0
    try:
        grandchild_pid = _await_grandchild_pid(pid_file)
        grandchild_start = process_start_time(grandchild_pid)
        assert grandchild_start is not None, "前提不成立：读不到孙进程启动时间，本测试的证据链无效"
        assert process_alive(grandchild_pid) is True, "前提不成立：孙进程没在跑"

        assert terminate_tree(parent.pid) is True
        assert _await_process_gone(grandchild_pid, grandchild_start), (
            "孙进程仍然活着：树杀只收掉了直接子进程"
        )
    finally:
        parent.kill()
        if grandchild_pid:
            terminate_tree(grandchild_pid, force=True)
