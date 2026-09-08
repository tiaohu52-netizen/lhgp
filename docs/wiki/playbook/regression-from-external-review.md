---
title: Regression patterns from external review
type: playbook
status: permanent
tags: [topic/quality, type/lessons-learned, audience/ai]
audience: [human, ai]
related: [[write-test-first]], [[quality-gate]]
created: 2026-09-08
last_reviewed: 2026-09-08
---

# Regression patterns from external review

Four rounds of independent re-test (P1 / Phase 2+3 / 3rd /
4th / submit-and-leave) each found real bugs the unit tests
missed.  This file is the reverse playbook: for every pattern
external review caught, what the unit test should have looked
like, and what the next PR must do to keep the same bug from
recurring.

**If you are an AI/agent writing a new RPC handler, MCP
wrapper, CLI subcommand, or daemon tick path**: read this
file end-to-end before you start.  Every pattern below has a
test that already pins it (or a test that should exist after
this PR).

## 1. The mock-envelope bypass

**Symptom (3rd, 4th-round, submit-and-leave)**: a Principal
gate lives in the MCP wrapper, not the handler.  A test calls
the tool with a hand-crafted ``ctx`` that has no ``envelope``
key (the real MCP stdio path) and a synthetic envelope with
``client_id="mcp"``.  The wrapper's ``if caller_envelope is not
None`` branch silently skips the check, the synthetic envelope
is built with ``client_id="mcp"`` (model), and the recorded
``actor="model"``.  The model self-signs.

**Unit-test pattern that misses it**:
```python
tool_user_confirm_spec_verdict(
    envelope.params,
    ctx={"conn": conn, "now": NOW, "envelope": envelope},
)
```
The injected ``envelope`` is a real ``RequestEnvelope`` but
the wrapper's allowlist branch is the only thing tested.  The
real production ``_make_context`` returns
``{"root": ..., "conn": ..., "registry": ...}`` — no
``envelope`` key.

**Fix (mandatory for every new tool)**:
- Principal gate lives in the **handler**, not the wrapper.
  Defense in depth.
- Test the no-envelope path explicitly:
  ```python
  tool_x(args, ctx={"conn": conn, "now": NOW})  # no envelope
  ```
- Test the real MCP server over stdio (see pattern 5 below).

**Pinned in**:
- `tests/unit/test_user_confirm.py` — no-envelope test case
- `tests/unit/test_auto_approve.py` — `test_non_daemon_clients_rejected` covers
  mcp / executor / verifier / cli / longtask-cli
- `tests/integration/test_4th_round_regression.py::test_mcp_user_confirm_via_real_stdio` — real
  stdio subprocess

## 2. Handler transitions only the status, not the full state

**Symptom (4th-round)**: a handler updates ``acceptance_status``
to PASSED and leaves ``state=ACTIVE``.  The dispatcher
re-spun the contract forever; the bound Goal never advanced;
the next-stage contract was never created.

**Unit-test pattern that misses it**:
```python
view = get_contract(conn, cid)
assert view.acceptance_status == AcceptanceStatus.PASSED
```
The contract's state is never checked.

**Fix (mandatory)**:
- After flipping status, run the full completion path: emit
  ``CONTRACT_COMPLETED``, transition to ``COMPLETE`` with
  ``acceptance_status=PASSED``, advance the bound Goal stage
  via :func:`advance_goal_after_verified_contract`.
- Test asserts ``state=COMPLETE`` AND ``acceptance_status=PASSED``
  AND the goal advanced to the next stage.

**Pinned in**:
- `tests/unit/test_user_confirm.py::test_user_confirm_moves_candidate_to_passed`
  — asserts `state=COMPLETE`
- `tests/integration/test_3stage_submit_and_leave.py` — assert all
  three stages reach the goal's ``completed`` list

## 3. Two-namespace parser divergence

**Symptom (3rd, 4th-round)**: ``lhgp/rpc/handlers/_common.py``
was patched to pass ``spec`` and ``spec_hash``; the actual
contract RPC handler imports from
``longtask/rpc/handlers/_common.py`` (a parallel file).  One
namespace was fixed, the other silently dropped the fields.

**Unit-test pattern that misses it**: tests for
``handle_contract_prepare`` use the same import the handler
uses, so the divergence is invisible from the test side.

**Fix (mandatory for new draft-shape fields)**:
- Bump BOTH ``lhgp/rpc/handlers/_common.py`` AND
  ``longtask/rpc/handlers/_common.py`` in the same commit.
  Docstring at the top of each must name the other as the
  canonical twin.
- Or, **delete one**: make ``longtask.rpc.handlers._common``
  re-export from ``lhgp`` (or vice-versa), not duplicate the
  parser.

**Pinned in**:
- `tests/integration/test_4th_round_regression.py::test_contract_prepare_preserves_spec_via_real_rpc`
  — calls ``route(RequestEnvelope(method=Method.CONTRACT_PREPARE))`` directly and
  reads back ``draft.acceptance.spec`` from the persisted row

## 4. CLI branch conditionally imports

**Symptom (3rd, 4th-round)**: ``from X import Y`` only happens
inside one argparse branch (``plan submit``).  A later branch
(``plan signoff``) references ``Y`` and hits
``UnboundLocalError: cannot access local variable 'Y'``.

**Unit-test pattern that misses it**: each branch is tested
in isolation.  No test exercises the
``argparse → dispatch → branch → function`` sequence end to
end.

**Fix (mandatory for CLI subcommands)**:
- Move imports to the top of the file, OR duplicate the
  import at the top of each branch.
- Add a **subprocess-level** test that spawns the real CLI
  (see pattern 5).

**Pinned in**:
- `tests/integration/test_4th_round_regression.py::test_cli_plan_signoff_real_subprocess`
  — spawns `python -m lhgp.cli.main plan signoff` and asserts
  ``PLAN_APPROVED`` event landed in the DB

## 5. Spec-vs-execution shape mismatch

**Symptom (4th-round)**: a validator expects ``stage.spec`` to
be a full StageSpec envelope (``goal``/``scope``/``acceptance``/etc.);
a synthesizer reads it as a boolean body
(``{"all": [...]}``).  A plan that passes the validator
silently fails to generate the next-stage contract.

**Unit-test pattern that misses it**: tests for the
synthesizer pass ``raw_spec`` directly without ever going
through the validator.  The two paths drift.

**Fix (mandatory for any spec-envelope field)**:
- The validator and the synthesizer must read the same
  shape.  Pick one (full envelope, recommended) and update
  both at once.
- Tests must run a stage entry through
  ``validate_stage_entry`` AND ``_synthesize_stage_draft``
  with the same input.

**Pinned in**:
- `tests/unit/test_synthesize_auto_approve_inherit.py::test_synthesize_inherits_auto_approve_for_spec_only_stage`
  — drives a stage with a spec-only entry through the
  synthesizer and asserts ``auto_approve`` carries forward

## 6. docstring blow-up

**Symptom (5th-round, commit f4bb408)**: a new function
ships with a 27-line docstring explaining the design
philosophy, the user-facing impact, the historical context,
and the future roadmap.  The user has to read 27 lines to
find the 1-2 lines that actually say what the function does.

**Unit-test pattern that misses it**: the linter only checks
length on individual lines, not total docstring length.

**Fix (mandatory for new code)**:
- 1-2 lines for new code.  The "why" decision goes in the
  first sentence; the "what" is left to the code.
- The :doc:`code-simplification` skill rules apply: no
  "design philosophy" headers, no "test contract protection"
  paragraphs, no multi-paragraph justifications of obvious
  choices.

**Caught by**:
- `verifier_4` audit on commit f4bb408 (4th-round
  follow-up).  12 docstrings were > 5 lines; all condensed
  to 1-3 lines.

## 7. Concurrent multi-event handler without pre-read CAS

**Symptom (5th-round, follow-up to `71bcfc6`)**: a handler
appends multiple audit events and then calls
``update_contract_state`` to flip the state.  No
``expected_revision`` is passed; the state update is
unconditional.  Two concurrent calls both write their
events to the log; whichever loses the state-race still
leaves its events in the table, and the audit trail
shows two ``CONTRACT_COMPLETED`` / ``ACCEPTANCE_STATUS_CHANGED``
records for a single logical transition.

The failure mode is **silent on the happy path**: a single
caller sees a clean state flip plus the events it wrote.
Only the multi-threaded test (or two CLI tabs in the wild)
sees the duplicate events and a confused dispatcher.

**Unit-test pattern that misses it**:
```python
# Single-threaded: events + state update both commit,
# revision is consistent.  Test passes.
result = handle_contract_user_confirm(env, conn=conn, now=NOW)
assert len(get_events(conn, contract_id=cid)) == expected
```
No thread pool, no shared DB, no pre-read snapshot.

**Fix (mandatory for any handler that appends events then
mutates state)**:
- Pre-transaction snapshot of ``expected_revision`` (and
  any other CAS field, e.g. ``acceptance_status``).
- Wrap the event appends + state update + downstream side
  effects (goal advance) in one
  ``with transaction(conn):`` block.  ``transaction()`` uses
  ``BEGIN IMMEDIATE`` so concurrent threads serialize on
  the write lock.
- Pass ``expected_revision=expected_revision`` to
  ``update_contract_state``; the loser's state update
  raises ``RevisionConflictError`` and the outer
  transaction rolls its events back.
- Convert ``RevisionConflictError`` to ``RpcError``
  **after** the ``with`` block exits — Python 3.13's
  ``contextlib.__exit__`` cannot assign ``__traceback__``
  to a frozen/slotted ``RpcError`` raised from inside the
  block.  Use ``raised = RpcError(...)`` then
  ``if raised: raise raised`` outside the block.
- One ``CONTRACT_COMPLETED`` event per transition: let
  ``update_contract_state``'s ``_STATE_TO_EVENT`` mapping
  write it; do not append a manual one and then call
  ``update_contract_state`` again.  Pass the extra fields
  via ``event_payload=`` so the auto-generated event
  carries the verifier/user-confirm provenance.

**Pinned in**:
- `tests/integration/test_user_confirm_concurrent.py` —
  two threads, only one survives, exactly one
  ``ACCEPTANCE_STATUS_CHANGED`` and one ``CONTRACT_COMPLETED``
  event in the final log
- `tests/integration/test_auto_approve_concurrent.py` —
  the same CAS pattern applied to the daemon-driven
  ``auto_approve_drafted_contract`` path
- `tests/integration/test_a2a_concurrent.py` —
  ``mark_directives_consumed`` race fixed by atomic
  SQL ``json_set + MAX + COALESCE``

## 8. The lazy "no real entry, just mock" test

**Symptom (recurring)**: a test creates a hand-crafted
``envelope``, calls the handler in-process, asserts the
return value.  Never exercises the real dispatcher, the
HANDLERS map, the daemon tick, or the JSON-RPC stdio path.

**Fix (mandatory for new RPC/MCP/CLI entry points)**:
- The new entry point's first PR must include a real
  subprocess test (CLI), a real JSON-RPC stdio test (MCP),
  or a real ``RequestEnvelope`` + ``route()`` test (RPC).
  See :doc:`test_4th_round_regression` for the template.
- The unit test in the same PR is supplementary, not
  primary.

**Pinned in**:
- `tests/integration/test_4th_round_regression.py` (CLI +
  MCP stdio + RPC)
- `tests/integration/test_3stage_submit_and_leave.py`
  (real subprocess executor + verifier)
- `tests/unit/test_auto_approve.py` (real RequestEnvelope
  × 10 cases)

---

## New-PR checklist

Before opening a PR that touches an RPC handler, MCP
wrapper, CLI subcommand, or daemon tick path, run through
this list:

1. **No-mock coverage**: the PR adds a test that drives the
   real entry point.  Subprocess for CLI, stdio for MCP,
   ``route(RequestEnvelope)`` for RPC.
2. **Both namespaces**: if you touched one of
   ``lhgp/rpc/handlers/_common.py`` /
   ``longtask/rpc/handlers/_common.py``, the same change
   ships in the other.
3. **CLI import discipline**: every branch's
   ``from X import Y`` is at the top of the function or the
   top of the file.  No conditional imports inside an
   ``if args.cmd == ...`` block.
4. **State + status**: a state-flip test asserts BOTH
   ``state`` AND ``acceptance_status`` AND (when the
   contract is bound to a goal) the goal's ``progress`` advanced.
5. **Docstring length**: 1-2 lines for new code, 3 max for
   ``why``-heavy exceptions.
6. **Idempotency**: every new RPC method is in
   ``IDEMPOTENT_METHODS`` and the handler calls
   ``idempotent_replay`` before any state change.
7. **Clock-iterator plumbing**: if you add a new
   ``clock()`` call in the daemon main loop or the daemon
   startup, audit every test that injects a clock iterator
   and add one timestamp per new call site.
8. **Spec/validator/synthesizer shape parity**: any new
   ``stage.spec`` field is accepted by
   ``validate_stage_entry`` AND read by the synthesizer.
9. **Pre-read CAS on multi-event handlers**: any handler
   that appends events then calls
   ``update_contract_state`` must pre-read
   ``expected_revision`` outside the transaction, wrap
   events + state update + downstream side effects in one
   ``with transaction():`` block, and pass
   ``expected_revision=`` to ``update_contract_state``.
   Add a multi-threaded integration test that pins the
   single-survivor outcome (see pattern 7).
10. **One event per logical transition**: do not append a
    manual ``CONTRACT_COMPLETED`` and then call
    ``update_contract_state(COMPLETE, ...)`` (which writes
    a second one via ``_STATE_TO_EVENT``).  Pass extra
    fields via ``event_payload=``; let the state update
    write the single canonical event.

Skip a check, the 4th-round-style leak is one PR away.
