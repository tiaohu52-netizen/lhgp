---
name: long-horizon-goals
description: 将需要跨会话、跨 Agent 推进的用户目标转成 LHGP 合同，明确授权、验收、Deadline、预算和交接边界。
---

# long-horizon-goals

> LHGP 协议面向 AI 的 skill 教学。配合 `lhgp-mcp` 使用；`longtask-mcp`
> 仅作为迁移窗口内的兼容别名。

## 何时触发

当用户**委托一个跨会话的任务**给你，且该任务：
- 超过单次对话上下文窗口
- 可能需要"歇一歇再继续"（即分散在多个时间点执行）
- 需要可被审计（用户事后要回看每一步）

→ 触发本 skill：把任务转成 LHGP 合同 + 提交给 `lhgp-mcp`，而非自己直接做。

## 核心公理

> **会话只持有一次尝试，LHGP 持有长期承诺。**

你的会话可能随时结束（上下文耗尽、用户关闭、模型服务重启）。
你起草的合同**不会**因为你的会话结束而消失——daemon 在本机持有它，
按 Deadline 风险推进或换另一个会话继续。

## 七步走通 MCP 工具链

1. `health` — 确认 MCP/daemon 在线
2. `doctor` — 做本机只读预检，确认数据库和已启用 CLI 可启动；发现缺失命令时先
   修环境或换候选，不要先批准合同消耗预算
3. `list_executors` — 查可用执行器池（Codex / Claude Code / 任何 CLI 适配器）
4. `lhgp_prepare_goal` — 起草合同：objective / acceptance / deadline / budget / authority
5. `lhgp_approve_goal` — **仅限用户**：模型调用返回 AUTH_FAILED。向用户说明合同内容与影响，请用户在 CLI 执行 `lhgp approve <id>`；`lhgp_update_goal` 同理（Goal 计划修订也是 Principal 决定权，ADR-004 规则 6）
6. `get_contract` — 看运行状态、当前 attempt、leasing，以及该合同隔离的
  `decision_history`（风险档、升级原因、预算余量和下一步依据）；可传
  `decision_limit` 控制决策历史条数，`attempt_limit` 控制 attempt 历史条数。
  响应中的 `deadline_snapshot` 是最新的风险快照（p50/p90、slack、risk、
  confidence、forecast_level、sample_count、p_finish_basis、reason、
  next_decision_at），应优先用于判断
  是否等待、请求验收、暂停或升级；它不是按时完成保证。
7. （等待 daemon 派 attempt；轮询 get_contract 或订阅 events）
8. `list_contracts` — 复盘历史 + 审计事件链

调用 `get_contract` 后，优先读取返回的 `verification_history`：
`verification/requested` 表示用户已请求验收，`verification/consumed` 表示
daemon 已接受并处理请求，`verification/started` 表示 verifier 已经启动。
只有看到后两者之一后才进入等待或读取 `attempt_history`；不要因为请求已写入
就臆测 verifier 已经运行。

如果执行预算已经耗尽、但工作区可能已经满足验收，不要重新起草或继续派
executor；调用 `lhgp_request_verification`（兼容名
`longtask_request_verification`）请求只验收当前交付物。它写入
`verification/requested`，由 daemon 下一次 tick 幂等派生独立 verifier；终态、
已有 verifier 运行中或验证预算耗尽时必须接受协议拒接并按提示升级。

## 场景 → 工具决策表（if...then）

| 你想做什么 | 调用 | 关键参数 | 响应里看什么 |
|---|---|---|---|
| 确认 MCP 在线 | `lhgp_health` | — | 工具名列表 |
| 派工前排查环境 | `lhgp_doctor` | — | FAIL 项 = 待处理问题 |
| 接手会话盘点工作 | `lhgp_list_contracts` → `lhgp_list_goals` | state 过滤 | deadline_snapshot 风险 |
| 看某合同现状/为什么 blocked | `lhgp_get_contract` | contract_id | decision_history + verification_history |
| 看多阶段流程走到哪 | `lhgp_get_goal` | goal_id | progress + 各阶段合同状态 |
| 不知道下一步合法动作 | `lhgp_next_goal_action` | goal_id | action 字段照做 |
| 阶段需要新合同 | `goal_contract_draft` → 给用户审阅 → `lhgp_prepare_goal` | goal_id + stage_id | 草案是只读预览 |
| 交付物疑似完成 | `lhgp_request_verification` | contract_id | 预算耗尽则停手交用户 |
| 阶段验收已通过 | `lhgp_advance_goal` | goal_id + stage_id + revision | STAGE_NOT_PASSED = 还没验收 |
| 执行者写进度/终态 | `lhgp_write_back` | fencing 三元组 | LEASE_FENCED = 重新取代次 |
| 用户要求停止 | `lhgp_interrupt_attempt` | attempt_id | 事件归属真实发起者 |
| 合同 blocked 想知道原因 | `lhgp_notifications` | goal_id/status | include_payload 默认 false |

工具名统一用 `lhgp_*` 正名。MCP 网关可按角色收窄工具面
（`LHGP_MCP_PROFILE=executor|verifier|planner|operator|full|legacy`，未设置 = 全量）：
执行者/核验者会话按角色启动后只看见自己需要的工具，越界调用会被直接拒绝——
如果某个 `lhgp_*` 工具不在你的工具列表里且报 "outside this server's profile"，
那是部署侧的收窄，不是协议错误，找操作者而不是反复重试。

## 运行中审计与控制（扩展工具）

- `lhgp_notifications` 是只读通知 outbox 视图；优先按 `goal_id` 或
  `status` 缩小范围，默认不请求 `include_payload`，避免把上下文内容带回对话。
- `lhgp_attempt_status` 用于查看某次 attempt 的事件、租约和当前状态。
- `lhgp_interrupt_attempt` 仅在用户明确要求停止时调用；它只写入中断请求，
  由 daemon 在安全仲裁点兑现。
- `lhgp_write_back` 只接受执行者持有的 generation，并必须携带真实进度或
  evidence；不要用它伪造完成状态。
- 所有会改变状态的 MCP 工具都支持 `request_id`。网络或模型重试时必须复用
  同一个 `request_id`，否则协议会把它视为新的变更请求；只读查询无需依赖它。

没有 MCP 时，可用等价的只读 CLI 审计：
`lhgp notifications --goal-id <id>`（payload 默认隐藏）。

## 起草合同时必须写清

- **objective** — 写验收，不是方法（"输出 result.txt 含 'hi'" 而非 "用 python 写"）
- **acceptance.checks** — 逐条可独立核对（不要 "看起来对就行"）
- **workload_initial_hours** — 如实填（决定紧迫度档位）
- **authority.executors** — 你知道哪些 CLI 可用；未列即拒（default-deny）
- **deadline** — 给时区，ISO-8601

`acceptance.checks[].kind` 必须使用协议线值：
`file-exists`、`file-content-matches`、`command-exit-zero`、
`artifact-present`、`structure-valid`、`observable` 或 `user-assertion`。
不要把设计文档里的 `artifact_exists`、`command`、`schema` 等概念名直接
写进合同；它们不是当前 wire 枚举。

对于 `command-exit-zero` 验收，若命令可能耗时较长，可在 check 的
`args.timeout_seconds` 中给出更短上限。运行时还会以合同剩余 Deadline
进一步收紧超时；超时只代表 `undetermined`，不要把它解释成通过或失败，
应交给 verifier 判定块或用户仲裁补证据。

## 不要做

- 不要把模型输出塞进 argv 或环境变量（注入防线 §14）
- 不要编造检查通过——failed 就是 failed
- 不要把"看起来对"算 pass——必须 evidence
- 不要试图执行合同——交给 daemon 派 attempt
