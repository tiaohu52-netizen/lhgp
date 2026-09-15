"""本机 JSON-RPC 线传输实现（DESIGN §11.1）。"""

from __future__ import annotations

import hmac
import json
import os
import socket
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.server import parse_envelope


def _error(code: ErrorCode, message: str) -> dict[str, Any]:
    return RpcError(code=code, message=message).to_payload()


def process_lines(
    lines: Iterable[str],
    *,
    token: str,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[str]:
    """处理连接上的 JSON 行，首行为 token 握手，后续逐行返回响应。"""

    iterator = iter(lines)
    try:
        raw_handshake = next(iterator)
    except StopIteration:
        return []
    try:
        handshake = json.loads(raw_handshake)
    except (TypeError, ValueError):
        return [json.dumps(_error(ErrorCode.AUTH_REQUIRED, "token handshake required"))]
    supplied = handshake.get("token") if isinstance(handshake, dict) else None
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
        return [json.dumps(_error(ErrorCode.AUTH_FAILED, "invalid endpoint token"))]

    responses: list[str] = []
    for raw_line in iterator:
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            envelope = parse_envelope(request)
            response = dispatch(request)
            if not isinstance(response, dict):
                raise TypeError("dispatch must return a JSON object")
        except RpcError as exc:
            response = exc.to_payload()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            response = _error(ErrorCode.VALIDATION_FAILED, f"malformed JSON-RPC request: {exc}")
        except Exception as exc:  # transport must not tear down on handler bugs
            response = _error(ErrorCode.INTERNAL, f"internal dispatch error: {exc}")
        _ = envelope if "envelope" in locals() else None
        responses.append(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return responses


def serve_stream(
    reader: TextIO,
    writer: TextIO,
    *,
    token: str,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """在已建立的文本流上服务一条连接，每个响应立即 flush。"""

    for response in process_lines(reader, token=token, dispatch=dispatch):
        writer.write(response + "\n")
        writer.flush()


def serve_unix_socket(
    endpoint: Path,
    *,
    token: str,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]],
    stop_event: threading.Event | None = None,
) -> None:
    """监听 Unix domain socket，逐连接服务 JSON-RPC。"""

    endpoint = Path(endpoint)
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    if not hasattr(socket, "AF_UNIX"):
        _serve_loopback_tcp(endpoint, token=token, dispatch=dispatch, stop_event=stop_event)
        return
    if endpoint.exists():
        if not stat.S_ISSOCK(endpoint.stat().st_mode):
            raise OSError(f"RPC endpoint exists and is not a socket: {endpoint}")
        endpoint.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # listen() goes first after bind(): ``bind`` is what creates the
        # filesystem node, so from that instant a client can see the endpoint
        # and get ECONNREFUSED if nothing is listening yet.  Reordering shrinks
        # the gap to one syscall; it cannot close it, so connecting clients must
        # retry rather than treat "the file exists" as "the RPC is up".
        server.bind(str(endpoint))
        server.listen(8)
        with suppress(OSError):
            endpoint.chmod(0o600)
        server.settimeout(0.5)
        while stop_event is None or not stop_event.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                reader = connection.makefile("r", encoding="utf-8", newline="\n")
                writer = connection.makefile("w", encoding="utf-8", newline="\n")
                try:
                    serve_stream(reader, writer, token=token, dispatch=dispatch)
                finally:
                    reader.close()
                    writer.close()
    finally:
        server.close()
        with suppress(FileNotFoundError):
            endpoint.unlink()


def _serve_loopback_tcp(
    endpoint: Path,
    *,
    token: str,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]],
    stop_event: threading.Event | None,
) -> None:
    """Windows fallback: loopback-only TCP with an endpoint metadata file.

    Python builds without ``AF_UNIX`` are common on Windows. The endpoint file
    contains only ``127.0.0.1`` and an ephemeral port; the existing token
    handshake remains the authentication boundary. No non-loopback bind is
    permitted, so this does not widen the protocol to a network transport.
    """
    if endpoint.exists():
        try:
            metadata = json.loads(endpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OSError(f"RPC endpoint exists and is not TCP metadata: {endpoint}") from exc
        if not isinstance(metadata, dict) or metadata.get("transport") != "tcp":
            raise OSError(f"RPC endpoint exists and is not TCP metadata: {endpoint}")
        if _loopback_endpoint_is_live(metadata):
            raise OSError(f"RPC endpoint is already in use: {endpoint}")
        endpoint.unlink()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    owned_endpoint = False
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        port = int(server.getsockname()[1])
        fd, temporary = tempfile.mkstemp(prefix=f".{endpoint.name}.", dir=endpoint.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as metadata_file:
                json.dump(
                    {"transport": "tcp", "host": "127.0.0.1", "port": port},
                    metadata_file,
                    separators=(",", ":"),
                )
            os.replace(temporary, endpoint)
            owned_endpoint = True
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
        with suppress(OSError):
            endpoint.chmod(0o600)
        _serve_connections(server, token=token, dispatch=dispatch, stop_event=stop_event)
    finally:
        server.close()
        if owned_endpoint:
            with suppress(FileNotFoundError):
                endpoint.unlink()


def _loopback_endpoint_is_live(metadata: object) -> bool:
    """Return whether an existing loopback endpoint still accepts connections.

    A stale metadata file left by a crashed daemon may be replaced safely, but
    an active daemon must never be displaced by a second listener.  Only the
    exact loopback address and a validated TCP port are probed; malformed data
    is treated as not live and is rejected by the caller's schema check.
    """
    if not isinstance(metadata, dict) or metadata.get("host") != "127.0.0.1":
        return False
    try:
        port = int(metadata["port"])
    except (KeyError, TypeError, ValueError):
        return False
    if not 1 <= port <= 65535:
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _serve_connections(
    server: socket.socket,
    *,
    token: str,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]],
    stop_event: threading.Event | None,
) -> None:
    server.settimeout(0.5)
    while stop_event is None or not stop_event.is_set():
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
        with connection:
            reader = connection.makefile("r", encoding="utf-8", newline="\n")
            writer = connection.makefile("w", encoding="utf-8", newline="\n")
            try:
                serve_stream(reader, writer, token=token, dispatch=dispatch)
            finally:
                reader.close()
                writer.close()


__all__ = ["process_lines", "serve_stream", "serve_unix_socket"]
