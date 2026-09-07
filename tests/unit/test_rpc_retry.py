"""Transient-retry decorator unit tests (SPEC retry dispatch layer).

Covers: 502-then-success, 4x502-then-fail, 4xx-no-retry, non-transient
500-no-retry, total time bounded, on_retry callback fired per retry.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from longtask.rpc.dispatch import (
    RETRY_EVENT_TYPE,
    RetryConfig,
    is_transient_error,
    with_transient_retry,
)

pytestmark = pytest.mark.unit


class _FakeHTTPStatusError(Exception):
    """Stand-in for httpx.HTTPStatusError (httpx is not a runtime dep)."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class _FakeConnectError(Exception):
    """Stand-in for httpx.ConnectError."""


class _FakeReadTimeoutError(Exception):
    """Stand-in for httpx.ReadTimeout."""


class _FakeRemoteProtocolError(Exception):
    """Stand-in for httpx.RemoteProtocolError."""


def test_is_transient_502() -> None:
    assert is_transient_error(_FakeHTTPStatusError(502)) is True


def test_is_transient_429() -> None:
    assert is_transient_error(_FakeHTTPStatusError(429)) is True


def test_is_transient_4xx_no_retry() -> None:
    assert is_transient_error(_FakeHTTPStatusError(400)) is False
    assert is_transient_error(_FakeHTTPStatusError(404)) is False
    assert is_transient_error(_FakeHTTPStatusError(422)) is False


def test_is_transient_500_no_retry() -> None:
    # Non-listed 5xx is surfaced (model-side or server bug, not transient).
    assert is_transient_error(_FakeHTTPStatusError(500)) is False
    assert is_transient_error(_FakeHTTPStatusError(501)) is False


def test_is_transient_connect_timeout_protocol() -> None:
    assert is_transient_error(_FakeConnectError()) is True
    assert is_transient_error(_FakeReadTimeoutError()) is True
    assert is_transient_error(_FakeRemoteProtocolError()) is True


def test_is_transient_stdlib() -> None:
    assert is_transient_error(ConnectionError("conn refused")) is True
    assert is_transient_error(TimeoutError("read timeout")) is True


def test_is_transient_value_error() -> None:
    # Plain application errors are never transient.
    assert is_transient_error(ValueError("bad input")) is False
    assert is_transient_error(RuntimeError("boom")) is False


def test_502_then_success_one_retry() -> None:
    calls: list[int] = []
    sleep_calls: list[float] = []
    retry_events: list[tuple[int, str, float]] = []

    def flaky() -> str:
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise _FakeHTTPStatusError(502)
        return "ok"

    @with_transient_retry(
        on_retry=lambda attempt, exc, delay: retry_events.append(
            (attempt, type(exc).__name__, delay)
        ),
        sleep=sleep_calls.append,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return flaky()

    assert run() == "ok"
    assert calls == [1, 2]
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(0.5, rel=0.0)
    assert len(retry_events) == 1
    assert retry_events[0][0] == 1
    assert retry_events[0][1] == "_FakeHTTPStatusError"


def test_502_four_times_then_fail() -> None:
    calls: list[int] = []
    sleep_calls: list[float] = []
    retry_events: list[int] = []

    def always_fails() -> str:
        calls.append(len(calls) + 1)
        raise _FakeHTTPStatusError(502)

    @with_transient_retry(
        max_retries=3,
        on_retry=lambda attempt, exc, delay: retry_events.append(attempt),
        sleep=sleep_calls.append,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return always_fails()

    with pytest.raises(_FakeHTTPStatusError):
        run()
    # 1 initial + 3 retries = 4 calls
    assert calls == [1, 2, 3, 4]
    assert len(sleep_calls) == 3
    assert len(retry_events) == 3
    assert retry_events == [1, 2, 3]


def test_4xx_no_retry() -> None:
    calls: list[int] = []
    sleep_calls: list[float] = []
    retry_events: list[Any] = []

    def bad_request() -> str:
        calls.append(len(calls) + 1)
        raise _FakeHTTPStatusError(404)

    @with_transient_retry(
        on_retry=lambda *args: retry_events.append(args),
        sleep=sleep_calls.append,
    )
    def run() -> str:
        return bad_request()

    with pytest.raises(_FakeHTTPStatusError):
        run()
    assert calls == [1]
    assert sleep_calls == []
    assert retry_events == []


@pytest.mark.parametrize(
    ("status", "expected_calls"),
    [(400, 1), (404, 1), (422, 1), (429, 4)],
)
def test_400_does_not_retry_429_does(status: int, expected_calls: int) -> None:
    """429 (rate limit) is retried; other 4xx are surfaced immediately."""
    calls: list[int] = []

    def raises() -> str:
        calls.append(1)
        raise _FakeHTTPStatusError(status)

    @with_transient_retry(
        sleep=lambda _: None,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return raises()

    with pytest.raises(_FakeHTTPStatusError):
        run()
    assert len(calls) == expected_calls, f"status={status}"


def test_non_transient_500_no_retry() -> None:
    """5xx outside {502, 503, 504} is surfaced immediately (model bug)."""
    calls: list[int] = []
    sleep_calls: list[float] = []

    def server_bug() -> str:
        calls.append(len(calls) + 1)
        raise _FakeHTTPStatusError(500)

    @with_transient_retry(
        sleep=sleep_calls.append,
    )
    def run() -> str:
        return server_bug()

    with pytest.raises(_FakeHTTPStatusError):
        run()
    assert calls == [1]
    assert sleep_calls == []


def test_connect_error_retries() -> None:
    calls: list[int] = []
    sleep_calls: list[float] = []

    def flaky() -> str:
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise _FakeConnectError()
        return "ok"

    @with_transient_retry(
        sleep=sleep_calls.append,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return flaky()

    assert run() == "ok"
    assert calls == [1, 2, 3]
    # Backoff: 0.5, 1.0 (capped at max_delay=8.0, no jitter with jitter_fn=0)
    assert sleep_calls == pytest.approx([0.5, 1.0])


def test_total_time_bounded() -> None:
    """Sum of backoff delays must be <= sum(2^i * base_delay) + jitter."""
    sleep_calls: list[float] = []

    def always_fails() -> str:
        raise _FakeHTTPStatusError(503)

    @with_transient_retry(
        max_retries=3,
        base_delay=0.5,
        max_delay=8.0,
        sleep=sleep_calls.append,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return always_fails()

    with pytest.raises(_FakeHTTPStatusError):
        run()
    # 0.5 + 1.0 + 2.0 = 3.5s; plus max possible jitter 0.5/2 * 3 = 0.75 -> 4.25s
    assert sum(sleep_calls) == pytest.approx(3.5, abs=0.0)
    # And it actually completes in well under a real second.
    start = time.monotonic()
    with pytest.raises(_FakeHTTPStatusError):

        @with_transient_retry(
            max_retries=3,
            base_delay=0.01,
            max_delay=0.05,
            sleep=lambda _: None,
            jitter=lambda: 0.0,
        )
        def run2() -> str:
            return always_fails()

        run2()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"retry loop took {elapsed:.3f}s, expected << 1s"


def test_event_emitted_on_each_retry() -> None:
    """The on_retry hook fires once per retry with attempt+exc+delay."""
    attempts: list[int] = []
    errors: list[str] = []
    delays: list[float] = []

    def flaky() -> str:
        raise _FakeConnectError("connection refused")

    def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        attempts.append(attempt)
        errors.append(type(exc).__name__)
        delays.append(delay)

    @with_transient_retry(
        max_retries=3,
        on_retry=on_retry,
        sleep=lambda _: None,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return flaky()

    with pytest.raises(_FakeConnectError):
        run()
    assert attempts == [1, 2, 3]
    assert errors == ["_FakeConnectError", "_FakeConnectError", "_FakeConnectError"]
    assert delays == pytest.approx([0.5, 1.0, 2.0])


def test_default_on_retry_is_noop() -> None:
    """No on_rety callback → decorator still retries and re-raises cleanly."""
    calls: list[int] = []

    def flaky() -> str:
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise _FakeHTTPStatusError(502)
        return "ok"

    @with_transient_retry(
        sleep=lambda _: None,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return flaky()

    assert run() == "ok"
    assert calls == [1, 2]


def test_retry_config_defaults() -> None:
    cfg = RetryConfig()
    assert cfg.max_retries == 3
    assert cfg.base_delay == 0.5
    assert cfg.max_delay == 8.0


def test_retry_event_type_wire_string() -> None:
    """Hardcoded wire string; EventType.RETRY_ATTEMPTED is a follow-up."""
    assert RETRY_EVENT_TYPE == "retry/attempted"


def test_max_delay_cap() -> None:
    """Backoff never exceeds max_delay even with many retries."""
    sleep_calls: list[float] = []

    def always_fails() -> str:
        raise _FakeHTTPStatusError(502)

    @with_transient_retry(
        max_retries=5,
        base_delay=0.5,
        max_delay=2.0,
        sleep=sleep_calls.append,
        jitter=lambda: 0.0,
    )
    def run() -> str:
        return always_fails()

    with pytest.raises(_FakeHTTPStatusError):
        run()
    # 0.5, 1.0, 2.0, 2.0, 2.0 (capped at 2.0 starting at retry_index=2)
    assert sleep_calls == pytest.approx([0.5, 1.0, 2.0, 2.0, 2.0])


def test_non_retryable_exception_propagates() -> None:
    calls: list[int] = []

    def value_error() -> str:
        calls.append(len(calls) + 1)
        raise ValueError("not transient")

    @with_transient_retry(
        sleep=lambda _: None,
    )
    def run() -> str:
        return value_error()

    with pytest.raises(ValueError, match="not transient"):
        run()
    assert calls == [1]
