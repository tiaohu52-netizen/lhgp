"""进程树终止的纯判定面（``lhgp.adapters.processes``）。

真进程的树终止由 ``tests/integration`` 覆盖；这里锁的是**决策**：
argv 形态与「什么时候允许 killpg」——后者判错会把推动者自己杀掉。
"""

from __future__ import annotations

import os

import pytest

from lhgp.adapters import processes


def test_taskkill_argv_without_force_requests_graceful_close() -> None:
    assert processes.taskkill_argv("tk.exe", 42, force=False) == [
        "tk.exe",
        "/T",
        "/PID",
        "42",
    ]


def test_taskkill_argv_with_force_adds_slash_f() -> None:
    assert processes.taskkill_argv("tk.exe", 42, force=True) == [
        "tk.exe",
        "/T",
        "/F",
        "/PID",
        "42",
    ]


def test_safe_process_group_rejects_non_positive_pid() -> None:
    assert processes.safe_process_group(0) is None
    assert processes.safe_process_group(-1) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_safe_process_group_returns_group_when_process_leads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processes.os, "getpgid", lambda pid: pid)
    assert processes.safe_process_group(4242) == 4242


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_safe_process_group_refuses_group_it_does_not_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不是组长就拒绝 killpg：组号可能指向推动者自己所在的组。"""
    monkeypatch.setattr(processes.os, "getpgid", lambda pid: pid - 1)
    assert processes.safe_process_group(4242) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_safe_process_group_refuses_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(pid: int) -> int:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(processes.os, "getpgid", _boom)
    assert processes.safe_process_group(4242) is None
