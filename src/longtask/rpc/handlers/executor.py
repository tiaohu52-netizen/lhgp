"""Legacy compatibility facade for :mod:`lhgp.rpc.handlers.executor`."""

from lhgp.rpc.handlers.executor import (
    handle_executor_disable,
    handle_executor_enable,
    handle_executor_health,
    handle_executor_list,
)

__all__ = [
    "handle_executor_disable",
    "handle_executor_enable",
    "handle_executor_health",
    "handle_executor_list",
]
