"""Transient-error retry decorator for the RPC dispatch layer.

Wraps a callable with exponential backoff for retryable failures:
``httpx.HTTPStatusError`` with status in {429, 502, 503, 504},
``httpx.ConnectError`` / ``ReadTimeout`` / ``RemoteProtocolError``,
and stdlib ``ConnectionError`` / ``TimeoutError``. 4xx (except 429)
and 5xx other than 502/503/504 are surfaced to the caller because
they typically indicate caller or model-side issues, not transient
infrastructure hiccups.

Total attempts are capped at ``1 + max_retries`` (default 4). The
backoff schedule is ``min(base_delay * 2**retry_index, max_delay) +
jitter`` where ``jitter`` is uniform in ``[0, base_delay/2]``.

We duck-type on class names (``ConnectError`` etc.) rather than
importing ``httpx``: the runtime dependency surface is intentionally
empty (DESIGN §13.1), and a hand-rolled detection keeps the decorator
usable even if callers are using a different HTTP client.
"""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

# TODO: use EventType.RETRY_ATTEMPTED once the events.py consolidation
# commit lands. The hardcoded wire string keeps this stream conflict-free
# against the parallel streams that also touch persistence.
RETRY_EVENT_TYPE = "retry/attempted"

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Exponential backoff parameters for transient retries."""

    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0


def _http_status_code(exc: BaseException) -> int | None:
    """Return the HTTP status code if ``exc`` exposes one (httpx-style)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def is_transient_error(exc: BaseException) -> bool:
    """True if ``exc`` is a retryable transient failure.

    Detection is duck-typed on attribute / class-name shape rather than
    importing ``httpx`` (zero-runtime-deps policy, DESIGN §13.1):
    - stdlib ``ConnectionError`` / ``TimeoutError``
    - class names ending in ``ConnectError`` / ``ReadTimeout`` /
      ``RemoteProtocolError`` (httpx transport errors)
    - any exception with an integer ``status_code`` attribute, when that
      code is in {429, 502, 503, 504}; 4xx (except 429) and unlisted
      5xx are surfaced to the caller.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    status = _http_status_code(exc)
    if status is not None:
        return status in _RETRYABLE_STATUS_CODES
    cls_name = type(exc).__name__
    return any(
        cls_name.endswith(suffix)
        for suffix in (
            "ConnectError",
            "ReadTimeout",
            "ReadTimeoutError",
            "RemoteProtocolError",
        )
    )


def _backoff_delay(config: RetryConfig, retry_index: int, jitter_fn: Callable[[], float]) -> float:
    base: float = min(config.base_delay * (2**retry_index), config.max_delay)
    return base + jitter_fn() * (config.base_delay / 2)


def with_transient_retry(
    *,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[], float] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Wrap a callable with transient-error retry and exponential backoff.

    The wrapped callable is invoked up to ``1 + max_retries`` times.
    On a retryable failure the decorator sleeps for an exponentially
    backed-off delay and calls ``on_retry(attempt, exc, delay)`` (when
    provided) before the next attempt. Non-retryable errors propagate
    immediately. ``sleep`` and ``jitter`` are injectable for tests.
    """
    config = RetryConfig(max_retries=max_retries, base_delay=base_delay, max_delay=max_delay)
    actual_sleep = sleep if sleep is not None else time.sleep
    actual_jitter = jitter if jitter is not None else random.random

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            for retry_index in range(config.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except BaseException as exc:
                    if retry_index >= config.max_retries or not is_transient_error(exc):
                        raise
                    delay = _backoff_delay(config, retry_index, actual_jitter)
                    if on_retry is not None:
                        on_retry(retry_index + 1, exc, delay)
                    actual_sleep(delay)
            raise RuntimeError("retry loop exited without return or raise")  # pragma: no cover

        return wrapper

    return decorator


__all__ = [
    "RETRY_EVENT_TYPE",
    "RetryConfig",
    "is_transient_error",
    "with_transient_retry",
]
