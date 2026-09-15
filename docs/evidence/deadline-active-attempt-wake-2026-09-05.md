# Deadline wake evidence (2026-09-05)

## Finding

The daemon's adaptive sleep path only consulted `next_decision_at` when the
runner was idle.  A live executor therefore forced the default heartbeat
interval even when the next deadline decision was earlier, allowing the daemon
to sleep past the decision boundary.

## Fix

Commit `7b6550c` makes the sleep duration the minimum of the heartbeat interval
and the earliest persisted decision point for both idle and active runners.
When a decision is already due, the next loop runs immediately.  Heartbeat
renewal and subprocess observation still occur at the next loop.

## Regression evidence

`tests/integration/test_p4_next_decision.py::test_active_attempt_still_wakes_for_earlier_deadline_decision`
forces the runner into the active state with a 30-second deadline and verifies
that a 60-second configured interval becomes a 29-second sleep.  The complete
quality gate then passed:

- 631 tests passed
- coverage 82.13%
- 7/7 quality gates passed
- mypy/typecheck: 162 source files, no issues
