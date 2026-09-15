"""P4 forecast dataclass 单元测试。"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from longtask.cli.tick import _completed_attempt_durations
from longtask.forecast.model import (
    RISK_TIER_THRESHOLDS,
    Forecast,
    build_deadline_snapshot,
    risk_tier,
)

pytestmark = pytest.mark.unit


class TestForecastRoundTrip:
    def test_to_dict_all_keys(self) -> None:
        f = Forecast(
            queue_minutes=5.0,
            startup_minutes=1.0,
            remaining_minutes=10.0,
            verification_minutes=3.0,
            retry_reserve_minutes=2.0,
            safety_margin_minutes=1.0,
            forecast_p50_minutes=19.0,
            forecast_p90_minutes=22.0,
            p_finish=0.85,
        )
        d = f.to_dict()
        assert set(d.keys()) == {
            "queue_minutes",
            "startup_minutes",
            "remaining_minutes",
            "verification_minutes",
            "retry_reserve_minutes",
            "safety_margin_minutes",
            "forecast_p50_minutes",
            "forecast_p90_minutes",
            "p_finish",
        }
        f2 = Forecast.from_dict(d)
        assert f2 == f

    def test_from_dict_all_none_safe(self) -> None:
        f = Forecast.from_dict({})
        assert f.remaining_minutes is None
        assert f.p_finish is None

    def test_from_dict_handles_garbage_values(self) -> None:
        # 非法值不能炸；当作 None
        f = Forecast.from_dict(
            {
                "queue_minutes": "not-a-number",
                "p_finish": [1],
                "remaining_minutes": True,
                "forecast_p90_minutes": math.nan,
                "safety_margin_minutes": math.inf,
            }
        )
        assert f.queue_minutes is None
        assert f.p_finish is None
        assert f.remaining_minutes is None
        assert f.forecast_p90_minutes is None
        assert f.safety_margin_minutes is None


class TestRiskTier:
    def test_risk_tier_thresholds(self) -> None:
        assert len(RISK_TIER_THRESHOLDS) == 6

    @pytest.mark.parametrize(
        "u,expected_tier",
        [
            (0.0, 0),
            (0.24, 0),
            (0.25, 1),
            (0.5, 2),
            (0.99, 2),
            (1.0, 3),
            (1.49, 3),
            (1.5, 4),
            (1.99, 4),
            (2.0, 5),
            (2.99, 5),
            (3.0, 6),  # 越最高档
            (100.0, 6),
        ],
    )
    def test_risk_tier_mapping(self, u: float, expected_tier: int) -> None:
        assert risk_tier(u) == expected_tier

    def test_risk_tier_none_for_past_deadline(self) -> None:
        assert risk_tier(None) is None


def test_deadline_snapshot_is_conservative_for_low_sample_forecast() -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    forecast = Forecast(forecast_p50_minutes=20, forecast_p90_minutes=40, p_finish=0.95)
    snapshot = build_deadline_snapshot(
        forecast, computed_at=now, due_at=now + timedelta(minutes=30), sample_count=0
    )
    assert snapshot.confidence == "low"
    assert snapshot.forecast_level == "coarse"
    assert snapshot.slack_p90_minutes == -10
    assert snapshot.risk == "red"
    assert snapshot.p_finish_basis == "coarse-heuristic"


def test_deadline_snapshot_treats_due_at_equality_as_not_missed() -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    forecast = Forecast(forecast_p50_minutes=0, forecast_p90_minutes=0, p_finish=0.9)
    snapshot = build_deadline_snapshot(forecast, computed_at=now, due_at=now, sample_count=3)
    assert snapshot.risk != "missed"


def test_historical_samples_are_not_presented_as_calibrated() -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    forecast = Forecast(
        queue_minutes=1,
        startup_minutes=1,
        remaining_minutes=10,
        verification_minutes=1,
        retry_reserve_minutes=1,
        safety_margin_minutes=1,
        forecast_p50_minutes=15,
        forecast_p90_minutes=20,
        p_finish=0.8,
    )
    snapshot = build_deadline_snapshot(
        forecast, computed_at=now, due_at=now + timedelta(hours=1), sample_count=3
    )
    assert snapshot.confidence == "high"
    assert snapshot.forecast_level == "historical"


def test_historical_finish_probability_uses_empirical_deadline_cdf() -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    forecast = Forecast(
        queue_minutes=1,
        startup_minutes=1,
        remaining_minutes=10,
        verification_minutes=1,
        retry_reserve_minutes=1,
        safety_margin_minutes=1,
        forecast_p50_minutes=15,
        forecast_p90_minutes=25,
        p_finish=0.99,  # 显式样本应覆盖未经证实的先验值
    )
    snapshot = build_deadline_snapshot(
        forecast,
        computed_at=now,
        due_at=now + timedelta(minutes=30),
        sample_count=3,
        sample_durations_minutes=(10.0, 27.0, 40.0),
    )
    # 排队/启动/验收/安全开销共 4 分钟，只有 10 分钟样本能在
    # 合同剩余的执行窗口内完成；不能把 27 分钟样本误算为可交付。
    assert snapshot.forecast.p_finish == pytest.approx(1 / 3)
    assert snapshot.sample_count == 3
    assert snapshot.p_finish_basis == "empirical-success-cdf"


def test_finish_probability_samples_exclude_failed_attempts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE attempts (goal_id TEXT, role TEXT, state TEXT, "
        "started_at TEXT, terminal_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO attempts VALUES (?, 'executor', ?, ?, ?)",
        [
            ("goal-a", "succeeded", "2026-09-03T12:00:00+00:00", "2026-09-03T12:10:00+00:00"),
            ("goal-a", "failed", "2026-09-03T12:00:00+00:00", "2026-09-03T12:01:00+00:00"),
        ],
    )
    conn.commit()
    assert _completed_attempt_durations(conn, "goal-a") == [10.0, 1.0]
    assert _completed_attempt_durations(conn, "goal-a", successful_only=True) == [10.0]
    conn.close()
