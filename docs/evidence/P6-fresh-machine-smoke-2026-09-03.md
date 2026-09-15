# P6 fresh-machine smoke evidence

Date: 2026-09-03  
Artifact: `longtask_protocol-0.1.0a0-py3-none-any.whl`  
Environment: new CPython 3.13 virtual environment created with `uv venv`

## Checks performed

1. Installed the wheel with `uv pip install --no-deps` in an isolated environment.
2. Ran `lhgp --version` → `lhgp 0.1.0a0 (protocol v1)`.
3. Ran `longtask --version` → `longtask 0.1.0a0 (protocol v1)` (compatibility alias).
4. Sent a `tools/list` JSON-RPC request to `lhgp-mcp`; the response contained the
   LHGP aliases, notification audit, interrupt, and write-back tools.
5. In a clean data directory, ran `prepare`, `get`, `approve`, `status`, and
   `notifications --goal-id` for `lt-20300101-qs-smoke`. The contract moved from
   `drafted` to `active` and the read-only commands returned successfully.
6. Verified both wheel and sdist with `scripts/check_artifacts.py`; all companion
   plugin and Skill resources were present.
7. Ran the registry probe twice on Windows. Both runs
   selected only the headless CLI for the executor role and only the code CLI for the
   verifier role, rejecting the enabled `codex-cli` interference entry.
8. Ran the complete `tests/integration/test_reconcile.py` suite: 20 tests passed,
   covering real subprocess handles, restart/reattach, orphan grace, fencing,
   redispatch, and post-reap settlement.
9. Ran `tests/integration/test_p5_repair_loop.py`: 8 tests passed, covering
   verifier failure, structured repair, re-verification, and budget exhaustion.
10. In a clean temporary data directory, started the daemon with a one-second
    interval, confirmed `status` reported the live PID and token, stopped it, and
    confirmed a second `status` reported `running: false`.
11. Ran the Codex plugin creator validator against the repository; validation
    passed after the plugin manifest was changed to strict SemVer `0.1.0`.
12. Rebuilt the wheel and sdist, verified both with `scripts/check_artifacts.py`,
    reran the plugin validator, and inspected the wheel to confirm its embedded
    `.codex-plugin/plugin.json` also reports version `0.1.0`.
13. Ran `lhgp doctor` against an empty data directory; Python runtime,
    storage, database integrity, executor registry, and kill-switch checks all
    reported `PASS` (`ALL SYSTEMS GO`).
14. Ran the official Skill structure validator for both
    `skills/long-horizon-goals` and `skills/longtask-contract` in Python UTF-8
    mode; both reported `Skill is valid!`.
15. Ran `lhgp migrate` with its default dry-run mode. It reported
    `mode: dry-run (nothing changed)` and the checked temporary directory
    remained empty; an already-populated target was correctly refused rather
    than overwritten.
16. Ran `lhgp --version` and a one-cycle `lhgp.cli.daemon_loop` scan against the
    real default data directory; both canonical entrypoint checks succeeded.
17. Opened a database with `user_version=2` but missing P3 additive columns;
    `ensure_schema` repaired it in place, and the daemon completed one scan
    without an `OperationalError`.
18. Rebuilt the wheel and inspected its `entry_points.txt`: canonical scripts
    point to `lhgp.*`, legacy scripts remain on `longtask.*`, and the wheel
    contains the complete `lhgp/` package tree.
19. Rebuilt the source distribution and inspected its tar members; it also
    contains the complete `src/lhgp/` tree and `src/lhgp/py.typed` marker.
20. Walked and imported all 65 modules under `lhgp` in a clean `uv run`
    process; every canonical facade imported successfully with zero failures.
21. Ran the release artifact gate after adding canonical entry-point assertions;
    rebuilt wheel and sdist both passed, and the full quality gate completed with
    493 tests passing at 80.45% coverage.

## Scope and remaining work

This proves package installation and the basic local quickstart path. It does not
prove daemon execution of a non-trivial external runner, cross-agent relay, or the
three Alpha dogfood goals; those remain release gates in `docs/LHGP-ROADMAP.md`.
