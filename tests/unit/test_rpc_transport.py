# ruff: noqa: S106 - test fixture token is intentionally deterministic

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from lhgp.rpc.transport import _serve_loopback_tcp
from longtask import PROTOCOL_VERSION
from longtask.rpc.client import call_unix_socket
from longtask.rpc.errors import ErrorCode
from longtask.rpc.transport import process_lines, serve_unix_socket
from tests.wait_budget import budget


def request() -> str:
    return json.dumps(
        {
            "method": "attempt/status",
            "request_id": "r1",
            "client_id": "client",
            "protocol_version": PROTOCOL_VERSION,
            "params": {},
        }
    )


def test_token_handshake_and_dispatch() -> None:
    seen: list[dict[str, object]] = []
    out = process_lines(
        [json.dumps({"token": "secret"}), request()],
        token="secret",
        dispatch=lambda raw: seen.append(raw) or {"ok": True, "result": {"ready": True}},
    )
    assert json.loads(out[0])["ok"] is True
    assert seen[0]["request_id"] == "r1"


def test_invalid_token_closes_without_dispatch() -> None:
    called = False

    def dispatch(_: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    out = process_lines(
        [json.dumps({"token": "wrong"}), request()], token="secret", dispatch=dispatch
    )
    payload = json.loads(out[0])
    assert payload["error"]["code"] == ErrorCode.AUTH_FAILED.value
    assert called is False


def test_malformed_request_is_recoverable() -> None:
    out = process_lines(
        [json.dumps({"token": "secret"}), "{bad", request()],
        token="secret",
        dispatch=lambda _: {"ok": True},
    )
    assert json.loads(out[0])["error"]["code"] == ErrorCode.VALIDATION_FAILED.value
    assert json.loads(out[1])["ok"] is True


def test_unix_socket_round_trip(tmp_path) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX unavailable")
    # macOS 的 AF_UNIX 路径上限约 104 字节，pytest tmp 目录层级深——
    # 用临时目录 + 内容哈希保证短而稳定（与产品 rpc_socket_path 同策略）。
    endpoint = Path(tempfile.gettempdir()) / (
        "lhgp-test-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16] + ".sock"
    )
    stopped = threading.Event()
    thread = threading.Thread(
        target=serve_unix_socket,
        kwargs={
            "endpoint": endpoint,
            "token": "secret",
            "dispatch": lambda _: {"ok": True, "result": {"ready": True}},
            "stop_event": stopped,
        },
        daemon=True,
    )
    thread.start()
    # Wait until the server is actually connectable.  ``endpoint.exists()`` is
    # not that condition: bind() creates the socket node and listen() follows
    # it, so polling the file and then connecting raced that gap -- macOS
    # raised ConnectionRefusedError there while Linux quietly queued the
    # connection.  The predicate is now the thing we depend on.
    deadline = time.monotonic() + budget(10.0)
    client: socket.socket | None = None
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(endpoint))
        except OSError as exc:
            last_error = exc
            probe.close()
            time.sleep(0.02)
            continue
        client = probe
        break
    assert client is not None, f"server never became connectable: {last_error}"
    with client:
        client.sendall((json.dumps({"token": "secret"}) + "\n").encode())
        client.sendall((request() + "\n").encode())
        client.shutdown(socket.SHUT_WR)
        payloads = client.recv(4096).decode().splitlines()
    stopped.set()
    thread.join(timeout=2)
    assert json.loads(payloads[0])["ok"] is True


def test_unix_socket_does_not_delete_regular_file(tmp_path) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX unavailable")
    endpoint = tmp_path / "rpc.sock"
    endpoint.write_text("sentinel", encoding="utf-8")
    stopped = threading.Event()
    try:
        serve_unix_socket(
            endpoint,
            token="secret",
            dispatch=lambda _: {"ok": True},
            stop_event=stopped,
        )
    except OSError as exc:
        assert "not a socket" in str(exc)
    else:
        raise AssertionError("regular endpoint file must not be deleted")
    assert endpoint.read_text(encoding="utf-8") == "sentinel"


def test_windows_loopback_fallback_round_trip(tmp_path) -> None:
    """Windows Python 无 AF_UNIX 时仍能通过本机 token RPC 通信。"""
    if hasattr(socket, "AF_UNIX"):
        return
    endpoint = tmp_path / "rpc.sock"
    stopped = threading.Event()
    thread = threading.Thread(
        target=serve_unix_socket,
        kwargs={
            "endpoint": endpoint,
            "token": "secret",
            "dispatch": lambda _: {"ok": True, "result": {"ready": True}},
            "stop_event": stopped,
        },
        daemon=True,
    )
    thread.start()
    for _ in range(50):
        if endpoint.exists():
            break
        time.sleep(0.01)
    response = call_unix_socket(
        endpoint,
        token="secret",
        method="attempt/status",
        request_id="r-loopback",
        client_id="client",
    )
    assert response["result"]["ready"] is True
    stopped.set()
    thread.join(timeout=2)
    assert not endpoint.exists()


def test_loopback_fallback_does_not_displace_live_daemon(tmp_path) -> None:
    """活动 daemon 的 endpoint metadata 不能被第二个 listener 覆盖。"""
    endpoint = tmp_path / "rpc.sock"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as live:
        live.bind(("127.0.0.1", 0))
        live.listen(1)
        endpoint.write_text(
            json.dumps(
                {
                    "transport": "tcp",
                    "host": "127.0.0.1",
                    "port": live.getsockname()[1],
                }
            ),
            encoding="utf-8",
        )
        try:
            _serve_loopback_tcp(
                endpoint,
                token="secret",
                dispatch=lambda _: {"ok": True},
                stop_event=threading.Event(),
            )
        except OSError as exc:
            assert "already in use" in str(exc)
        else:
            raise AssertionError("live endpoint must not be displaced")
        assert endpoint.exists()
