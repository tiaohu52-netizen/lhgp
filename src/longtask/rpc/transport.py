"""Legacy compatibility facade for :mod:`lhgp.rpc.transport`."""

from lhgp.rpc.transport import process_lines, serve_stream, serve_unix_socket

__all__ = ["process_lines", "serve_stream", "serve_unix_socket"]
