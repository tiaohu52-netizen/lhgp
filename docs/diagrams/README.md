# Diagrams

Architecture and flow diagrams for the resilient-execution layer. Generated with [`tt-a1i/archify`](https://github.com/tt-a1i/archify) (MIT, v2.17) and validated at showcase quality.

## Index

| # | File | Diagram | Audience |
|---|------|---------|----------|
| 01 | [`01-resilient-execution.png`](./01-resilient-execution.png) | End-to-end view: where plan gate, retry, auto-handover, and resume live in the system. | Anyone evaluating the project at a glance. |
| 02 | [`02-plan-gate.png`](./02-plan-gate.png) | Sequence: agent submits a Plan, validator approves or rejects, runner checks before dispatch. | Anyone reading the plan_mode code. |
| 03 | [`03-auto-handover-dataflow.png`](./03-auto-handover-dataflow.png) | Data flow: how the daemon tick detects, writes HANDOVER_DUE, and the runner reacts. | Anyone reading the auto-handover code. |
| 04 | [`04-handover-lifecycle.png`](./04-handover-lifecycle.png) | Lifecycle: warm warning (60s debounce) vs. overdue (immediate). | Anyone tuning the threshold. |

## Self-contained HTML versions

Every PNG has a sibling interactive HTML in `output/` that supports theme switching, pan/zoom, search, and tracing. Open in a browser for the full experience.

## Source specs

The source JSON for each diagram lives in `specs/`. To regenerate:

```bash
# from C:/Users/17464/.codex/skills/archify_tmp
node archify/bin/archify.mjs render architecture ../../工作台/远期任务协议/docs/diagrams/specs/01-resilient-execution.architecture.json ../../工作台/远期任务协议/docs/diagrams/output/01-resilient-execution.html --quality showcase
node archify/bin/archify.mjs visual-check ../../工作台/远期任务协议/docs/diagrams/output/01-resilient-execution.html
```

`validate` runs the 9-check showcase artifact contract before delivery. `visual-check` collects browser-side containment evidence at 1440×900 and 2048×1320 viewports.

## Reuse

These diagrams are generated from real code, not invented. The `archify` skill enforces "no invention": every node label, edge, and classification must trace back to a wire string or function. To re-author them after a code change, edit the spec, validate, render, and re-export — the loop takes minutes.
