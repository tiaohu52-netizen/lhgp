"""Shared wall-clock budgets for tests that spawn real subprocesses.

Executor and verifier attempts run as genuine ``python.exe`` children, so the
time a test spends waiting for one is dominated by interpreter startup and by
whatever the host happens to be doing (antivirus scan, a second pytest worker,
a laptop thermal-throttling) — not by the code under test.  Each such test used
to hard-code its own small number, so a loaded machine made the wait expire and
the test failed for reasons unrelated to the product.

Two rules when touching these tests:

- Size the budget for a loaded machine and let the polling loop exit early;
  a generous ceiling costs nothing while the child finishes fast, whereas a
  tight one turns scheduling noise into a red gate.
- If CI needs a further allowance, set ``LHGP_TEST_WAIT_SCALE`` rather than
  editing every call site.
"""

from __future__ import annotations

import os

#: Multiplier applied to every subprocess wait budget in the suite.
SCALE = float(os.environ.get("LHGP_TEST_WAIT_SCALE") or "1.0")


def budget(seconds: float) -> float:
    """Return a wait duration of ``seconds``, scaled for the current machine.

    Call site shape::

        deadline = time.monotonic() + budget(30.0)

    so the loop still exits the moment its predicate holds; the number is a
    ceiling for the worst case, not a sleep.
    """
    return seconds * SCALE
