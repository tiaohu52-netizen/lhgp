"""P6 deadline level threshold tests (lhgp.enforcement.levels).

The hardcoded WARNING_THRESHOLD=0.30 and URGENT_THRESHOLD=0.05 govern
the user-visible escalation behavior (T-30%/T-5% warnings, breach lock).
These tests pin the boundary so a future tuning does not silently change
the protocol.

Boundary math (1h window from now):
  - elapsed_ratio = 1 - remaining / total
  - NORMAL   :  elapsed_ratio < 0.70  (>= 30% remaining)
  - WARNING  :  0.70 <= elapsed_ratio < 0.95
  - URGENT   :  elapsed_ratio >= 0.95 OR remaining < 60s
  - BREACHED :  now > deadline_at
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lhgp.enforcement.levels import (
    MIN_ESCALATION_WINDOW,
    URGENT_THRESHOLD,
    WARNING_THRESHOLD,
    DeadlineLevel,
    compute_deadline_level,
    contract_window_seconds,
)

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
TOTAL = 3600.0  # 1h window


def _level_at(remaining: float) -> DeadlineLevel:
    """Return the level for a contract that has ``remaining`` seconds left."""
    decision = compute_deadline_level(
        NOW + timedelta(seconds=remaining),
        now=NOW,
        total_seconds=TOTAL,
    )
    return decision.level


class TestNormalBand:
    def test_freshly_started(self) -> None:
        assert _level_at(TOTAL) is DeadlineLevel.NORMAL

    def test_just_below_warning_threshold(self) -> None:
        # Just past the WARNING boundary on the safe side.
        remaining = TOTAL * WARNING_THRESHOLD + 1.0
        assert _level_at(remaining) is DeadlineLevel.NORMAL

    def test_at_warning_threshold(self) -> None:
        remaining = TOTAL * WARNING_THRESHOLD
        assert _level_at(remaining) is DeadlineLevel.WARNING


class TestWarningBand:
    def test_mid_warning(self) -> None:
        # 30% < remaining/total < 5% → WARNING
        remaining = TOTAL * (WARNING_THRESHOLD - 0.10)  # ~36% elapsed
        assert _level_at(remaining) is DeadlineLevel.WARNING

    def test_top_of_warning_just_below_urgent(self) -> None:
        remaining = TOTAL * URGENT_THRESHOLD + 1.0
        assert _level_at(remaining) is DeadlineLevel.WARNING

    def test_urgent_threshold_breach(self) -> None:
        # elapsed_ratio >= 0.95 → URGENT, not WARNING
        remaining = TOTAL * URGENT_THRESHOLD
        assert _level_at(remaining) is DeadlineLevel.URGENT


class TestUrgentBand:
    def test_mid_urgent(self) -> None:
        # 2% remaining → URGENT
        remaining = TOTAL * 0.02
        assert _level_at(remaining) is DeadlineLevel.URGENT

    def test_minute_floor_overrides(self) -> None:
        # remaining < 60s but elapsed_ratio still below URGENT_THRESHOLD:
        # MIN_ESCALATION_WINDOW (60s) forces URGENT via the "small absolute
        # window" branch. Choose a 60s window with 58s remaining so
        # elapsed_ratio = 1 - 58/60 ≈ 0.033 (below 0.05) — only the floor
        # branch can fire.
        decision = compute_deadline_level(
            NOW + timedelta(seconds=58),
            now=NOW,
            total_seconds=60.0,
        )
        assert decision.level is DeadlineLevel.URGENT
        assert "small absolute window" in decision.rationale

    def test_floor_threshold_constant(self) -> None:
        # Sanity: the documented floor is 60s.
        assert MIN_ESCALATION_WINDOW == 60.0


class TestBreached:
    def test_just_past_deadline(self) -> None:
        assert _level_at(-1.0) is DeadlineLevel.BREACHED

    def test_far_past_deadline(self) -> None:
        assert _level_at(-3600.0) is DeadlineLevel.BREACHED

    def test_breach_includes_negative_remaining_in_payload(self) -> None:
        decision = compute_deadline_level(
            NOW - timedelta(seconds=120),
            now=NOW,
            total_seconds=TOTAL,
        )
        assert decision.remaining_seconds < 0
        assert decision.elapsed_ratio == 1.0


class TestThresholdsContract:
    def test_thresholds_constant(self) -> None:
        # Pin the user-visible tuning knobs.
        assert WARNING_THRESHOLD == 0.30
        assert URGENT_THRESHOLD == 0.05

    @pytest.mark.parametrize(
        "remaining_seconds,expected",
        [
            (TOTAL, DeadlineLevel.NORMAL),
            (TOTAL * 0.5, DeadlineLevel.NORMAL),
            (TOTAL * 0.31, DeadlineLevel.NORMAL),
            (TOTAL * 0.30, DeadlineLevel.WARNING),
            (TOTAL * 0.20, DeadlineLevel.WARNING),
            (TOTAL * 0.10, DeadlineLevel.WARNING),
            (TOTAL * 0.06, DeadlineLevel.WARNING),
            (TOTAL * 0.05, DeadlineLevel.URGENT),
            (TOTAL * 0.01, DeadlineLevel.URGENT),
            (61.0, DeadlineLevel.URGENT),  # URGENT due to floor
            (60.0, DeadlineLevel.URGENT),
            (0.0, DeadlineLevel.BREACHED),  # remaining<=0 → BREACHED
            (-1.0, DeadlineLevel.BREACHED),
        ],
    )
    def test_band_table(self, remaining_seconds: float, expected: DeadlineLevel) -> None:
        assert _level_at(remaining_seconds) is expected


class TestTotalSecondsIsMandatory:
    """``total_seconds`` 不是可选调优项，是判定能否成立的必要条件。

    回归：省略时 ``compute_deadline_level`` 回落成 ``remaining * 2``，于是
    elapsed_ratio 恒等于 0.5——任何未逾期合同都只能报 NORMAL。daemon 路径
    传了合同的真实窗口，而 CLI 的 ``deadline-report`` 与 MCP
    ``deadline_report`` 工具没传，同一个函数对同一份合同给出两种结论。
    """

    def test_omitting_total_seconds_caps_at_normal(self) -> None:
        """只剩 1% 的合同，不传 total_seconds 也报 NORMAL（错误行为）。"""
        decision = compute_deadline_level(NOW + timedelta(seconds=TOTAL * 0.01), now=NOW)
        assert decision.level is DeadlineLevel.NORMAL
        assert decision.elapsed_ratio == pytest.approx(0.5)

    def test_passing_the_window_reports_the_real_risk(self) -> None:
        """同一时刻，传入真实窗口后必须报 URGENT。"""
        assert _level_at(TOTAL * 0.01) is DeadlineLevel.URGENT

    def test_window_helper_computes_contract_window(self) -> None:
        created = NOW
        deadline = NOW + timedelta(seconds=TOTAL)
        assert contract_window_seconds(deadline, created) == pytest.approx(TOTAL)

    def test_window_helper_rejects_non_positive_windows(self) -> None:
        """deadline <= created_at 是脏数据，返回 None 让调用方走降级分支。"""
        assert contract_window_seconds(NOW, NOW) is None
        assert contract_window_seconds(NOW - timedelta(hours=1), NOW) is None


class TestWindowIsWhatCallersActuallyPass:
    """钉住「调用点必须传窗口」这个契约本身。"""

    def test_daemon_and_reports_share_one_helper(self) -> None:
        import inspect

        from longtask import mcp_server
        from longtask.cli import daemon_loop
        from longtask.cli import main as cli_main

        for module in (daemon_loop, cli_main, mcp_server):
            src = inspect.getsource(module)
            assert "contract_window_seconds" in src, (
                f"{module.__name__} 必须经 contract_window_seconds 传 total_seconds，"
                "否则该路径的 deadline 等级恒为 NORMAL"
            )
