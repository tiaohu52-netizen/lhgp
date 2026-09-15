"""状态机合法迁移表纯函数测试（DESIGN §5）。

覆盖：各状态出边、终态无出边、非法迁移判定。
"""

from __future__ import annotations

import pytest

from longtask.contracts.schema import ContractState
from longtask.contracts.state_machine import (
    LEGAL_TRANSITIONS,
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    is_terminal_state,
    is_valid_transition,
)

pytestmark = pytest.mark.unit


class TestStateMachine:
    def test_terminal_states_set(self) -> None:
        assert set(TERMINAL_STATES) == {
            ContractState.COMPLETE,  # legacy alias（§7 命名迁移）
            ContractState.SATISFIED,
            ContractState.CANCELLED,
            ContractState.ARCHIVED,
        }
        for s in TERMINAL_STATES:
            assert is_terminal_state(s) is True
            assert LEGAL_TRANSITIONS[s] == frozenset()

        for s in NON_TERMINAL_STATES:
            assert is_terminal_state(s) is False

    def test_drafted_transitions(self) -> None:
        assert is_valid_transition(ContractState.DRAFTED, ContractState.ACTIVE) is True
        assert is_valid_transition(ContractState.DRAFTED, ContractState.CANCELLED) is True
        assert is_valid_transition(ContractState.DRAFTED, ContractState.PAUSED) is False
        assert is_valid_transition(ContractState.DRAFTED, ContractState.COMPLETE) is False
        assert is_valid_transition(ContractState.DRAFTED, ContractState.EXPIRED) is False

    def test_active_transitions(self) -> None:
        assert is_valid_transition(ContractState.ACTIVE, ContractState.PAUSED) is True
        assert is_valid_transition(ContractState.ACTIVE, ContractState.BLOCKED) is True
        assert is_valid_transition(ContractState.ACTIVE, ContractState.COMPLETE) is True
        assert is_valid_transition(ContractState.ACTIVE, ContractState.EXPIRED) is True
        assert is_valid_transition(ContractState.ACTIVE, ContractState.CANCELLED) is True
        assert is_valid_transition(ContractState.ACTIVE, ContractState.DRAFTED) is False
        assert is_valid_transition(ContractState.ACTIVE, ContractState.ARCHIVED) is False

    def test_paused_transitions(self) -> None:
        assert is_valid_transition(ContractState.PAUSED, ContractState.ACTIVE) is True
        assert is_valid_transition(ContractState.PAUSED, ContractState.CANCELLED) is True
        assert is_valid_transition(ContractState.PAUSED, ContractState.COMPLETE) is False
        assert is_valid_transition(ContractState.PAUSED, ContractState.BLOCKED) is False

    def test_blocked_transitions(self) -> None:
        assert is_valid_transition(ContractState.BLOCKED, ContractState.ACTIVE) is True
        assert is_valid_transition(ContractState.BLOCKED, ContractState.COMPLETE) is True
        assert is_valid_transition(ContractState.BLOCKED, ContractState.ARCHIVED) is True
        assert is_valid_transition(ContractState.BLOCKED, ContractState.EXPIRED) is True
        assert is_valid_transition(ContractState.BLOCKED, ContractState.CANCELLED) is True

    def test_expired_transitions(self) -> None:
        assert is_valid_transition(ContractState.EXPIRED, ContractState.COMPLETE) is True
        assert is_valid_transition(ContractState.EXPIRED, ContractState.ARCHIVED) is True
        assert is_valid_transition(ContractState.EXPIRED, ContractState.ACTIVE) is True
        assert is_valid_transition(ContractState.EXPIRED, ContractState.CANCELLED) is True
        assert is_valid_transition(ContractState.EXPIRED, ContractState.DRAFTED) is False

    def test_terminal_states_have_no_transitions(self) -> None:
        for term in (ContractState.COMPLETE, ContractState.CANCELLED, ContractState.ARCHIVED):
            for target in ContractState:
                assert is_valid_transition(term, target) is False
