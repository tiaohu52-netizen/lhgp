---
name: Pull request
about: Submit changes to the reference implementation
title: ""
labels: []
assignees: []
---

## What & why

One-paragraph summary. Reference the design section or issue this addresses.

- DESIGN §:
- Issue: #

## Scope of change

- [ ] Wire protocol (new method / event type / error code)
- [ ] Internal module
- [ ] Tests only
- [ ] Documentation only
- [ ] Skill (`skills/longtask-contract/`)
- [ ] CI / tooling

**Reminder**: this repo's design source of truth is `DESIGN.md`. Any change to
the protocol surface (methods, events, error codes) **must update DESIGN.md
first** and bump `schema_version` per the contract; code follows.

## Quality gate (all must be ✅)

- [ ] `uv sync --extra dev` clean
- [ ] `uv run python scripts/quality_gate.py` passes locally
  (`format / lint / arch / deps / claims / typecheck / test+coverage` — 7/7)
- [ ] If protocol surface changed: `quality/claims.json` updated with new
      evidence or new claim
- [ ] If `examples/` artifacts touched: `git diff` shows the change is the
      minimum necessary (archive integrity)
- [ ] Commit message follows `CONTRIBUTING.md` convention
      (`type: 中文摘要`, type ∈ feat/fix/chore/docs/gate/refactor)

## Test plan

- New tests added? (paths / what they cover)
- Commands to run for manual verification:

```bash
# reproducible verification
```

## Risk / migration

- Breaking change? (note: which existing callers affected)
- Migration notes for downstream users (if any)

## Checklist

- [ ] I read `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`
- [ ] I added or updated tests for any behavior change
- [ ] I did not touch `runtime*/` or generated artifacts
- [ ] I did not change the protocol surface without updating `DESIGN.md`
