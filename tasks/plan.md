# 当前工作计划

当前目标：统一“限期合同中枢”产品定位，修复已承诺能力的发布阻断项，优先发布 Developer Preview。

- 定位权威：[ADR-004](../docs/decisions/0004-contract-runtime-and-release-scope.md) 与 [SPEC](../docs/LHGP-SPEC.md)。
- 执行顺序、任务卡、验收条件：[RELEASE-PLAN.md](../docs/RELEASE-PLAN.md)。
- 当前结论与复现：[发布检查](../docs/evidence/release-readiness-2026-09-05.md)。
- 下一项：R1 超时取消失败的控制权保护；随后 R2 执行／验证预算隔离。
- 本轮已实施 R1/R2 运行时修复，未提交、打 tag、推送或对外发布。以下计划作为历史记录保留。

---

# 历史：Deadline Decision Reliability v1

## Objective

在单机、离线可用的范围内，把 Deadline 从“定时扫描提示”提升为可解释、可审计、可恢复的决策控制面。
范围澄清：跨主机、跨网络和云端中继不在当前计划；严格墙钟结果担保不作为待实现能力。

## Design assumptions

- `due_at` 仍是决策边界，不承诺不可控世界中的绝对完成时间。
- 现有合同、租约、验收和 `next_decision_at` 数据模型保持兼容。
- 每次风险重算都产生可去重的 Deadline snapshot；状态变化必须可审计。
- 低样本或数据不完整时必须降级为 `low/coarse`，不得伪装成精确概率。

## Ordered work

1. Update the authoritative spec and roadmap with the single-machine scope and concrete Deadline Reliability invariants.
2. Add a canonical immutable Deadline snapshot calculator: six-component forecast, p50/p90 slack, confidence, risk tier, and next decision point.
3. Integrate snapshot persistence and risk-change events into the daemon tick without event spam; preserve existing dispatch and miss semantics.
4. Add unit/integration/conformance coverage for stale/low-confidence estimates, threshold transitions, deadline boundary, idempotency, and recovery after restart.
5. Run format, lint, typecheck, tests, coverage, claims and architecture gates; update evidence only after the gates pass.

## Acceptance criteria

- No cross-host/network implementation is introduced.
- Every active contract has an explainable latest Deadline snapshot after a tick.
- Snapshot risk changes and confidence degradation are observable exactly once per unchanged revision/state.
- `due_at` equality is not a miss; `now > due_at` remains an atomic miss.
- Existing test suite remains green and new tests cover the invariants above.
