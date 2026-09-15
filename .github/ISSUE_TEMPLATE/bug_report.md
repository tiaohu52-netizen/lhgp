---
name: Bug report
about: Something is wrong (handler error, gate failure, unexpected output)
title: "[bug] "
labels: ["bug"]
assignees: []
---

## Reproduction

```bash
# minimal steps or input that triggers the bug
```

## Expected

What you expected to happen.

## Actual

What actually happened (full output, stack trace, screenshots).

## Environment

- OS + version:
- Python version (`python --version`):
- Repo commit / branch:
- Install method (`uv sync`? editable? pypi?):

## Quality gate

Did `uv run python scripts/quality_gate.py` pass on your tree?
(If no, paste the failing step's output — that is often the entire diagnosis.)

## Possible cause

Optional: if you already have a hypothesis or suspect a specific file/line,
note it here to speed triage.
