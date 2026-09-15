"""本机 JSON-RPC 客户端。

客户端与 :mod:`lhgp.rpc.server` 共享同一套协议版本、错误类型和请求
封装，旧的 ``longtask.rpc.client`` 路径仅作为兼容入口保留。
"""

from __future__ import annotations

import json
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any

from lhgp import PROTOCOL_VERSION
from lhgp.rpc.errors import ErrorCode, RpcError


def call_unix_socket(
    endpoint: Path,
    *,
    token: str,
    method: str,
    request_id: str,
    client_id: str,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """通过认证 Unix socket 调用 JSON-RPC 方法并返回响应。"""

    request = {
        "method": method,
        "request_id": request_id,
        "client_id": client_id,
        "protocol_version": PROTOCOL_VERSION,
        "params": params or {},
    }
    if hasattr(socket, "AF_UNIX"):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    else:
        host, port = _read_loopback_endpoint(endpoint)
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.connect((host, port))
    with connection:
        connection.settimeout(timeout)
        if hasattr(socket, "AF_UNIX"):
            connection.connect(str(endpoint))
        connection.sendall((json.dumps({"token": token}) + "\n").encode("utf-8"))
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        # 服务端按连接 EOF 批量处理请求；半关闭写端让它知道请求已完整，
        # 同时保留读端接收响应。没有这一步，loopback/Unix 两种传输都会超时。
        with suppress(OSError):
            connection.shutdown(socket.SHUT_WR)
        reader = connection.makefile("r", encoding="utf-8", newline="\n")
        try:
            line = reader.readline()
        finally:
            reader.close()
    if not line:
        raise RpcError(
            code=ErrorCode.INTERNAL,
            message="RPC server closed connection without response",
        )
    response = json.loads(line)
    if not isinstance(response, dict):
        raise RpcError(code=ErrorCode.INTERNAL, message="RPC server returned a non-object response")
    if response.get("ok") is False:
        raise RpcError.from_payload(response)
    return response


def _read_loopback_endpoint(endpoint: Path) -> tuple[str, int]:
    try:
        metadata = json.loads(Path(endpoint).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RpcError(
            code=ErrorCode.INTERNAL,
            message=f"invalid loopback RPC endpoint metadata: {endpoint}",
        ) from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("transport") != "tcp"
        or metadata.get("host") != "127.0.0.1"
    ):
        raise RpcError(code=ErrorCode.AUTH_FAILED, message="RPC endpoint is not loopback TCP")
    try:
        port = int(metadata["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message="invalid loopback RPC port") from exc
    if not 1 <= port <= 65535:
        raise RpcError(code=ErrorCode.INTERNAL, message="invalid loopback RPC port")
    return "127.0.0.1", port


__all__ = ["call_unix_socket"]
