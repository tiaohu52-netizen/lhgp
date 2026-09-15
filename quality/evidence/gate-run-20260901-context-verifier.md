# 质量门运行证据：§4.1 临时上下文 + 执行者侧 RPC + §5.2 verifier + e2e 复验

- 日期：2026-09-01
- 命令：`uv run python scripts/quality_gate.py`（本地，与 CI 同一命令）
- 环境：Windows 11（10.0.26200），uv 0.11.16 管理的 CPython 3.13（uv sync --extra dev）
- 结果：**ALL PASS (7 gates)**

## 各门结果

| # | 门 | 结果 | 备注 |
|---|----|------|------|
| 1 | format | PASS | 65 文件全部已格式化（ruff format --check） |
| 2 | lint | PASS | ruff check 零违规（含 examples/ 排除） |
| 3 | arch | PASS | 架构依赖方向违规 0 / 基线 0 |
| 4 | deps | PASS | 运行时依赖 0；dev 依赖 6 个全部 == 锁定且在白名单 |
| 5 | claims | PASS | 16 条声明（15 verified, 1 accepted_debt, 0 blocking） |
| 6 | typecheck | PASS | mypy --strict，34 个源文件零问题 |
| 7 | test+coverage | PASS | 245 passed，总覆盖率 86.65%（≥70% 基线） |

## 本次提交序列（911862d / 2b34348 / 84f0b46 + 本次收尾）

1. **§4.1 临时上下文**（DESIGN §4.1，persistence/context.py 新模块）：
   ContextPolicy 解析合同 context 字段；compile_context_snapshot 物化
   active.md（合同锚点 + 交接 + 最近 attempt 终态摘要）+ scratch.md 骨架；
   容量合同 fail-closed（context/capacity-refused + CapacityRefusedError）；
   task_prompt 自动注入交接附言——修复「再派 attempt 缺验收失败上下文」
   缺口（v1 第二轮浪费的根因）。probe 路径不物化快照（§10 时序）。
2. **执行者侧 RPC**（DESIGN §11.2，rpc/executor_api.py 新模块）：
   attempt/status、attempt/logs、attempt/write-back、lease/renew 四个
   handler；fencing（write_generation/holder 校验，LEASE_FENCED 拒绝
   旧代次写回事件不落库，§14.1）；request_id 幂等（重试同 event_ids，
   §11.3）；词汇表内事件（context/scratch-updated + attempt/*）。
3. **§5.2 verifier**（cli/runner.py + cli/daemon.py）：
   AttemptRunner._dispatch_verifier：执行者 succeeded 后派生独立
   verifier attempt（role=VERIFIER，候选 ≠ 执行者，无独立候选记
   ESCALATION_HANDED_TO_USER）；daemon._judge_verifier_outcomes
   tick 末钩子按 verifier 终态事件裁决：succeeded→合同 complete
   （actor=verifier，带证据），failed→退回 active。
4. **端到端真实复验**（内部归档）：headless CLI 执行者走通
   同任务：执行者 attempt 收 active.md 快照（context/snapshot-built），
   90 次 lease/renewed 心跳，succeeded 后派生 exec-b verifier，
   verifier succeeded → contract/completed 合同 complete。事件链
   完整可审计。如实记录已知设计缺口：headless harness 子进程生命
   与 Popen 句柄对齐（v2 README §"已知设计缺口"）。

## 门真实拦下的问题

- mypy：dict[str, Any] | {} 误写为类型注解；after 标注（forward ref）。
- lint：unused variable（RUF059）、field 误用、import 顺序、未启用 type
  ignore。
- claims：accepted_debt 的 debt_policy 必填字段（reentry_trigger /
  non_blocking）。
- format：跨提交有多轮重排。

## 治理增量

- `pyproject.toml` ruff `extend-exclude = ["examples"]`：归档交付物不
  格式化不 lint（改写即篡改验收证据）。
- claims 新增两条 verified（ephemeral-context-and-verifier、
  executor-session-rpc）+ 证据三类（focused_test / integration_real_store /
  integration_real_agent-cli 第三方归档）。
- README 发布分级同步：Developer Preview 已涵盖 §4.1 / §11.2 / §5.2；
  v1 补 headless harness 生命周期对齐（v2 README 已记）。
- examples 归档（预览期私有）：v1（无上下文/无 verifier）→ v2
  （含 §4.1 快照 + §5.2 自动裁决）的演化证据。