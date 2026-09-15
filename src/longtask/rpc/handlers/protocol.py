"""Legacy compatibility facade for :mod:`lhgp.rpc.handlers.protocol`."""

from lhgp.rpc.handlers.protocol import (
    handle_daemon_wake,
    handle_protocol_events,
    handle_protocol_hello,
)

__all__ = ["handle_daemon_wake", "handle_protocol_events", "handle_protocol_hello"]
