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
11. **Trusted pre-authorization at the Goal layer**: the
    per-contract ``auto_approve`` field is the model's
    *request*; the trusted source is the bound Goal's
    ``plan.pre_authorized`` (user-pinned at
    ``goal/update`` time, Principal-gated).  A model
    caller (``client_id="mcp"``) must NOT carry an
    ``auto_approve`` claim — strip it at
    ``parse_contract_draft`` boundary.  A Goal-less
    contract, or a Goal without ``pre_authorized``, is
    never auto-approved.  See pattern 9 below.
12. **Goal-level execution config**: a spec-only stage
    synthesised contract must inherit
    ``Goal.plan.execution_config.workspace_root`` and
    ``Goal.plan.execution_config.executor_grant``;
    otherwise the synthesised contract has no eligible
    executor and ends up BLOCKED(NO_EXECUTOR).  Inline
    stage drafts that omit ``file_effects`` /
    ``authority`` also inherit from the Goal.  An
    explicit per-stage value always wins.  See
    pattern 10 below.
13. **MCP runtime → Principal-only tools**: when a
    Principal-only tool is invoked via the MCP runtime
    (the runtime builds a ``ctx`` without an
    ``envelope`` key), the rejection is ``AUTH_FAILED``
    with explicit guidance, **not** ``INTERNAL``.  The
    model should know to escalate to the user, not see
    a server fault.
14. **No fail-open guard on an optional field**: if a guard
    compares two values to decide "this evidence is still
    valid", it must reject when either side is missing, and
    the value it compares must be computed by the runtime —
    never a label the caller chose.  See pattern 11 below.

## 9. Trusted pre-authorization at the Goal layer (model self-sign fix)

**Symptom (5th-round review 2026-09-08)**: ``auto_approve_drafted_contract``
trusted the per-contract ``draft.auto_approve.enabled`` field — a
value the model itself sets when calling the MCP path.  An
MCP-issued ``contract/prepare`` with
``{"auto_approve": {"enabled": true, "actions": [...]}}`` was
auto-promoted to ACTIVE without any user sign-off, and the
synthesised next-stage contract inherited the grant.

**Unit-test pattern that misses it**: any test that
exercises the handler in-process with a hand-crafted
``ctx`` that has ``client_id="cli"`` and an
``auto_approve.enabled=True`` contract.  The
MCP-wrapped path was not exercised, so the bypass went
unnoticed.

**Fix (mandatory for any new auto-approve / pre-auth
field)**:
- Move the trusted source to the bound Goal's
  ``plan.pre_authorized`` (user-pinned at
  ``goal/update`` time, Principal-gated).  The
  per-contract ``auto_approve`` is the model's
  *request*; auto-approve only fires when the
  user-pinned grant covers that request (or the user
  pinned a generic grant with no specific actions).
- ``parse_contract_draft`` strips ``auto_approve``
  when the caller is ``client_id="mcp"`` so a model
  cannot even *claim* a scope.  Defense in depth.
- A contract with no Goal binding, or a Goal without
  ``pre_authorized``, is never auto-approved.
- ``synthesize_stage_draft`` initialises the new
  contract's ``auto_approve`` from
  ``Goal.plan.pre_authorized`` when set; the
  per-stage inline draft path is preserved as an
  override.
- ``tool_submit_plan`` augments the per-contract
  ``auto_approve`` check with the Goal's
  ``pre_authorized`` scope so a plan whose steps are
  inside the user's pre-grant is auto-approved even
  when the contract's own ``auto_approve`` field is
  empty (the MCP case after the parse-time strip).

**Pinned in**:
- ``tests/unit/test_trusted_pre_authorization.py`` —
  7 cases: MCP cannot self-authorize, CLI can still
  set, no Goal binding → no auto-approve,
  grant-covers-claim → fires, claim-outside-scope →
  rejected, generic-grant-without-claim → fires,
  ``enabled=False`` → blocks.
- ``tests/integration/test_3stage_submit_and_leave.py``
  — sets ``Goal.plan.pre_authorized`` to match the
  claimed action scope.

## 10. Goal-level execution config (workspace + executor_grant)

**Symptom (5th-round review 2026-09-08, follow-up)**: a
spec-only stage synth path dropped the user's
``workspace_root`` and ``executor_grant`` — the
synthesised contract had no eligible executor and ended
up ``BLOCKED(NO_EXECUTOR)``.  The same gap affected
inline stage drafts that omitted ``file_effects`` or
``authority``.

**Unit-test pattern that misses it**: tests that
build the inline draft by hand with the full
``file_effects`` + ``authority`` already populated, so
the inherit-from-Goal fallback path is never exercised.

**Fix (mandatory for any new stage-synth / auto-create
helper)**:
- Add ``plan.execution_config`` to the Goal plan: a
  dict with ``workspace_root`` (str) and
  ``executor_grant`` (list of {executor_id, models,
  roles}).
- ``synthesize_stage_draft`` reads
  ``Goal.plan.execution_config.workspace_root`` and
  copies it into
  ``hard_constraints.file_effects.workspace_root``
  when the spec-only stage doesn't pin one (mode
  defaults to ``workspace-write``).
- Same for ``execution_config.executor_grant`` →
  ``authority.executors`` (with
  ``executor_policy='explicit_allow'`` so the
  dispatcher can pick an eligible candidate).
- ``auto_create_next_stage_contract`` (the
  inline-draft branch) does the same fallback for
  inline stage drafts that omit ``file_effects`` or
  ``authority``.
- An explicit per-stage ``workspace_root`` /
  ``executor_grant`` always takes precedence over the
  Goal-level default.

**Pinned in**:
- ``tests/unit/test_goal_execution_config.py`` —
  5 cases: spec-only synth inherits workspace,
  spec-only synth inherits executor grant,
  inline-stage-draft inherits when missing, explicit
  per-stage wins, no-executor-grant regression pin.

## 11. The optional field with a fail-open comparison

**Symptom (8th-round review 2026-09-10)**: ``user_confirm``
refused to complete a contract on stale verifier evidence
only when *both* sides of a comparison were present and
differed.  The compared field — ``acceptance.spec_hash`` —
is optional and supplied by the caller, so the ordinary case
(nobody filled it in) fell through the guard and the contract
landed in COMPLETE/passed against a requirement nobody had
checked.  Worse: an honest caller who *had* used hashes was
less protected after editing, because the patch turned the
current value into ``None`` — which the same rule read as
"equal".

**Why the unit tests missed it**: every existing test seeded
the two-sided case (``hash-done`` vs ``hash-new``).  A guard
that fires on exactly one of four input states looks healthy
from that direction; only a caller who leaves the optional
field out discovers it.

**Fix (mandatory for any "is this evidence still valid?"
comparison)**:
- Compare an identity the **runtime computes** from the
  content (``Acceptance.content_fingerprint``), not a label
  the caller supplied.  A caller-chosen hash proves nothing
  and cannot be invalidated by an edit the caller did not
  announce.
- Make the comparison **fail closed**: missing identity is
  "does not match", never "matches".  Test all four states
  (absent/absent, present/absent, absent/present,
  present/present) — the two-sided pair is only one of them.
- Stamp the identity at **every** producer of the evidence,
  including the recovery paths: the runner that collects a
  finished attempt, the reconciler that settles one after a
  daemon restart, and the write-back handler.  One producer
  that forgets reintroduces the hole for that flow only,
  which is how this stayed hidden.
- Tighten only: keep the older veto alongside the new one, so
  no previously-rejected sequence starts passing.
- Refusals must name their reason ("edited" vs "no
  fingerprint"); the operator's next step differs.

**Pinned in**:
- ``tests/unit/test_acceptance_content_binding.py`` — the
  four spec_hash states, unbound legacy evidence, the
  identical-content relabel veto, the happy path that must
  still confirm, and a scan requiring every verifier-success
  producer to stamp the binding.
- ``tests/unit/test_plan_approval_migration.py`` — the
  7th-round two-sided case, kept green to prove the
  tightening did not loosen anything.

Skip a check, the 5th-round-style leak is one PR away.
