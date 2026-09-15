"""进程身份与存活探测单元测试（SPEC §11.3 process_identity）。

「pid + 启动时间」双重比对的契约：
- pid 不得单独作为身份真相：缺启动时间 → identity_matches 返回 None；
- pid 复用可检出：启动时间对不上 → False（确认不是同一进程）；
- 无法取得就返回 None，绝不猜一个值。

存活探测用本测试进程（pid=os.getpid()）与已退出的子进程做真实判定，
不 mock——ctypes/kernel32 与 /proc 路径必须真的跑过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from longtask.adapters.processes import (
    IDENTITY_TOLERANCE_SECONDS,
    identity_matches,
    process_alive,
    process_start_time,
    terminate_pid,
)
from tests.wait_budget import budget

pytestmark = pytest.mark.unit


class TestProcessStartTime:
    def test_current_process_start_time_obtainable(self) -> None:
        """本进程一定能取到启动时间；取不到说明平台探测整个失效。"""
        start = process_start_time(os.getpid())
        assert start is not None
        # 启动时间在过去，且不会久于本测试进程的合理寿命
        now = time.time()
        assert now - 3600.0 < start <= now

    def test_invalid_pid_returns_none(self) -> None:
        assert process_start_time(0) is None
        assert process_start_time(-1) is None


class TestProcessAlive:
    def test_current_process_is_alive(self) -> None:
        assert process_alive(os.getpid()) is True

    def test_exited_child_is_not_alive(self) -> None:
        """真实子进程退出后：确认不存活（False），不是未知。"""
        proc = subprocess.Popen(  # noqa: S603 —— 测试固定 argv，无不可信输入
            (sys.executable, "-c", "pass"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=budget(30.0))
        # 进程已退出且已被本进程收尸：pid 探测应报 False
        assert process_alive(proc.pid) is False

    def test_invalid_pid_returns_none(self) -> None:
        assert process_alive(0) is None


class TestIdentityMatches:
    def test_same_process_with_real_start_time_matches(self) -> None:
        """同一进程 + 真实启动时间 → True（这是 reattach 的信任根）。"""
        pid = os.getpid()
        start = process_start_time(pid)
        assert start is not None
        assert identity_matches(pid, start) is True

    def test_start_time_within_tolerance_matches(self) -> None:
        """容差内（不同时钟源的微小误差）仍判同一进程。"""
        pid = os.getpid()
        start = process_start_time(pid)
        assert start is not None
        assert identity_matches(pid, start + IDENTITY_TOLERANCE_SECONDS / 2) is True

    def test_start_time_beyond_tolerance_detected_as_reuse(self) -> None:
        """启动时间对不上 → False：pid 被复用，确认不是同一 run。"""
        pid = os.getpid()
        assert identity_matches(pid, time.time() - 3600.0) is False

    def test_missing_start_time_is_none_not_guess(self) -> None:
        """缺启动时间：pid 不得单独作为身份真相 → None（无法确认）。"""
        assert identity_matches(os.getpid(), None) is None

    def test_missing_pid_is_none(self) -> None:
        assert identity_matches(0, 123.0) is None


class TestTerminatePid:
    def test_terminate_spawned_child(self) -> None:
        """对真实存活的子进程 terminate 生效；已退出的不抛错。"""
        proc = subprocess.Popen(  # noqa: S603 —— 测试固定 argv，无不可信输入
            (sys.executable, "-c", "import time; time.sleep(30)"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert terminate_pid(proc.pid) is True
            proc.wait(timeout=budget(30.0))
            assert proc.returncode is not None
        finally:
            proc.kill()
            proc.wait()

    def test_terminate_invalid_pid_is_false(self) -> None:
        assert terminate_pid(0) is False
