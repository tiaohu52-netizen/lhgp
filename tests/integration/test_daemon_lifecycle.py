"""longtaskd 常驻进程生命周期集成测试（DESIGN §3.3、§15.2）。

真实分离子进程：start -> status running -> stop 优雅退出 -> 状态与文件清理。
全部走 main() CLI 入口；唯一真实等待是启动确认与停止轮询（秒级，integration 允许）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from longtask.cli.daemon import (
    DAEMON_STOP_FILE,
    PID_FILE,
    TOKEN_FILE,
    get_daemon_status,
    halt_daemon,
)
from longtask.cli.daemon_loop import run_daemon_loop
from longtask.cli.main import main
from longtask.persistence.store import StoreConfig, connect, ensure_schema

pytestmark = pytest.mark.integration


def test_start_stop_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # 1. start：分离后台进程，写真实 pid/token
    rc = main(["--data-dir", str(data_dir), "start", "--interval", "0.2"])
    assert rc == 0
    started = json.loads(capsys.readouterr().out)
    assert started["ok"] is True
    assert (data_dir / PID_FILE).is_file()
    assert (data_dir / TOKEN_FILE).is_file()
    assert get_daemon_status(data_dir)["running"] is True
    assert "rpc_socket_available" in get_daemon_status(data_dir)

    try:
        # 2. stop：daemon.stop 优雅退出（间隔 0.2s 的循环很快消费掉标记）
        rc = main(["--data-dir", str(data_dir), "stop"])
        assert rc == 0
        halted = json.loads(capsys.readouterr().out)
        assert halted["was_running"] is True
        assert halted["forced"] is False  # 优雅退出，未升级强杀

        status = get_daemon_status(data_dir)
        assert status["running"] is False
        assert not (data_dir / PID_FILE).exists()
        assert not (data_dir / TOKEN_FILE).exists()
        assert not (data_dir / DAEMON_STOP_FILE).exists()
    finally:
        # 兜底清理：断言失败时不遗留孤儿进程
        halt_daemon(data_dir, grace_seconds=1.0)


def test_rpc_unavailable_is_audited_without_thread_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 无 Unix socket 时，RPC 降级不应产生未捕获线程异常。"""
    import longtask.cli.daemon_loop as daemon_loop

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    conn.close()
    (data_dir / TOKEN_FILE).write_text("test-token\n", encoding="utf-8")
    messages: list[str] = []

    def unavailable(**_kwargs: object) -> None:
        raise RuntimeError("this platform does not provide Unix domain sockets")

    monkeypatch.setattr(daemon_loop, "serve_unix_socket", unavailable)
    result = run_daemon_loop(data_dir, interval_seconds=0, max_cycles=1, emit_fn=messages.append)
    assert result["ok"] is True
    assert any(message.startswith("rpc/degraded:") for message in messages)


def test_memory_expire_sweep_runs_each_cycle(tmp_path: Path) -> None:
    """P2 verifier P0 fix: each daemon cycle sweeps expired memories.

    A memory past its ``expires_at`` must be removed and a
    ``memory/expired`` event appended. With no due rows, the sweep is a
    silent no-op (no event, no emit line).
    """
    from datetime import UTC, datetime, timedelta

    from lhgp.memory import Memory, MemoryKind, MemoryScope, record_memory
    from lhgp.persistence.events import EventType

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    # One stale memory (already expired) + one fresh memory.
    stale = Memory(
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.PATTERN,
        title="stale-pattern",
        body_md="too old to keep",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        expires_at=datetime(2024, 6, 1, tzinfo=UTC),
        score=0.5,
        schema_version=4,
    )
    record_memory(conn, stale)
    fresh = Memory(
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.PATTERN,
        title="fresh-pattern",
        body_md="still valid",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=180),
        score=0.5,
        schema_version=4,
    )
    record_memory(conn, fresh)
    conn.close()

    (data_dir / TOKEN_FILE).write_text("test-token\n", encoding="utf-8")
    messages: list[str] = []
    # Pin ``now`` so the 2024-06-01 expiry is firmly in the past.
    fixed_now = datetime(2026, 9, 7, tzinfo=UTC)
    result = run_daemon_loop(
        data_dir,
        interval_seconds=0,
        max_cycles=1,
        emit_fn=messages.append,
        now_fn=lambda: fixed_now,
    )
    assert result["ok"] is True
    assert any("memory/expire: dropped 1 due memories" in m for m in messages), messages

    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        titles = [r[0] for r in conn.execute("SELECT title FROM memories").fetchall()]
        assert "stale-pattern" not in titles
        assert "fresh-pattern" in titles
        evt = conn.execute(
            "SELECT event_type FROM events WHERE event_type = ?",
            (EventType.MEMORY_EXPIRED.value,),
        ).fetchone()
        assert evt is not None
    finally:
        conn.close()


def test_start_rejects_when_already_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    rc = main(["--data-dir", str(data_dir), "start", "--interval", "0.2"])
    assert rc == 0
    capsys.readouterr()

    try:
        rc2 = main(["--data-dir", str(data_dir), "start", "--interval", "0.2"])
        assert rc2 == 1
        err = json.loads(capsys.readouterr().out)
        assert "already running" in err["error"]
    finally:
        halt_daemon(data_dir, grace_seconds=1.0)


def test_start_recovers_from_stale_pid_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # 一个已经退出的进程的 pid：残留 pid 文件不阻塞新 start
    dead = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    dead.wait()
    (data_dir / PID_FILE).write_text(f"{dead.pid}\n", encoding="utf-8")

    rc = main(["--data-dir", str(data_dir), "start", "--interval", "0.2"])
    assert rc == 0
    capsys.readouterr()
    assert get_daemon_status(data_dir)["running"] is True

    halt_daemon(data_dir, grace_seconds=1.0)
    assert get_daemon_status(data_dir)["running"] is False
