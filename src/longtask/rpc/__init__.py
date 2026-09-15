"""Legacy compatibility facade for :mod:`lhgp.rpc`."""

from lhgp.rpc import ErrorCode, Method, RpcError, call_unix_socket

__all__ = ["ErrorCode", "Method", "RpcError", "call_unix_socket"]
