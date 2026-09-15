---
name: Feature or design proposal
about: Suggest a new method, behavior, or design clarification
title: "[proposal] "
labels: ["discussion"]
assignees: []
---

## Summary

One-paragraph description of the proposal.

## Motivation

- What problem does this solve? (reference a use case or DESIGN.md section)
- Who is the user (human / AI agent / daemon)?
- Why is the current design insufficient?

## Proposed change

- New method (wire protocol) / new event type / new CLI subcommand?
- New RPC handler? (which method name, which params)
- Breaking change? (if yes, list which claims/contracts change)

## Alternatives considered

What else you considered and rejected — helps reviewers see the design space.

## Test plan

- How will we know it works? (conformance / integration scenario)
- Which gate is the entry point? (format / lint / arch / deps / claims / typecheck / test+coverage)
- Will this add or change a `claims.json` claim?
