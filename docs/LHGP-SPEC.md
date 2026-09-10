# 远期目标协议（Long-Horizon Goal Protocol，LHGP）权威设计规范

> 文档版本：1.0  
> 目标协议版本：`lhgp/v1alpha1`  
> 日期：2026-09-01  
> 状态：**权威设计基线**；实现尚未全部符合本规范  
> 适用范围：单机、单用户参考运行时；协议语义不绑定 Python、MCP 或 Codex

本文件定义项目“应该是什么”和“完成意味着什么”。它是后续实现、README、schema、CLI、MCP、Skill 与插件封装的上游依据。旧 [DESIGN.md](../DESIGN.md) 仍是当前实现的历史设计输入；两者在产品语义上冲突时，以本文件为准。当前能力只能由代码、测试与 `quality/claims.json` 证明，不能因本文件写了 MUST 就宣称已经实现。

本文使用 MUST、MUST NOT、SHOULD、SHOULD NOT、MAY 表达规范强度。

---

## 0. 核心定义

**LHGP 的参考实现（产品名「限期合同中枢」，Deadline Contract Hub）是独立于会话和模型的多 Agent 任务合同运行时，支持远期目标的持续推进与证据化验收。**
Long-Horizon Goal Protocol（远期目标协议）继续作为协议名称；“任务合同运行时”描述系统类别，“限期合同中枢”是产品名称。
用户通过 CLI、程序或 Agent 客户端建立合同，声明期望结果、验收、Deadline、权限、预算及允许使用的执行者。
运行时持有多份合同，在后台保存证据化进度、选择或更换获授权的执行者、编译接力上下文、管理 Deadline 风险，
并根据验收证据推进合同及关联目标。该定义依据 [ADR-004](decisions/0004-contract-runtime-and-release-scope.md)。

调度内核 MUST 能在没有 LLM 参与决策时工作。LLM MAY 用于起草计划、执行任务或语义验收，
但模型会话 MUST NOT 成为合同存续的必要条件。未来可选规划器只能通过受校验的计划／修订入口提交建议，
MUST NOT 绕过合同授权、预算、审批或验收来直接改写权威状态。

当前应分别理解三个层次：运行时管理多份合同；Goal 消费外部提交的阶段计划并推进关联合同；
规划器自主提出或重写计划。前两者已有实现路径，第三者是后续可选增强，不得混称为已实现的自主目标规划。
多合同持久化也不构成全局最优调度或任意共享工作区并行写入安全的保证。

> **会话持有一次尝试，LHGP 持有长期承诺。**  
> **A session owns an attempt. LHGP owns the commitment.**

“远期”不等于“单次运行很久”。只要目标可能跨越任一边界，它就属于本协议的候选范围：

- 原聊天或任务被关闭；
- 原 Agent 进程退出或失联；
- 执行模型被更换；
- 机器或守护进程重启；
- 工作被暂停数小时或数日；
- 验收失败后需要另一执行者修复；
- Deadline 风险要求调整执行节奏。

协议承担的是**受约束的推进义务**，不是对不可控世界作结果担保。它必须尽力在 Deadline 前交付通过验收的结果；做不到时，必须提前暴露风险，并在到点后留下可审计的违约事实、部分成果与下一步选择，绝不能静默失败。

---

## 1. 要解决的用户问题

目标用户是已经会使用 Codex、Claude Code、agent-cli、Hermes 或其他 Agent CLI，但不愿持续盯住一个会话的人。

其核心任务可以写成：

> 当我有一个需要跨天或多轮推进的结果时，我希望把它交给一份独立合同，而不是交给某个聊天窗口；这样即使原会话、模型或应用消失，目标仍能在我批准的权限、预算和 Deadline 内被其他 Agent 接力，最后用我事先同意的标准验收。

用户购买的不是“后台进程”，而是四种确定性：

1. **目标不会随会话消失。**
2. **换人不会丢掉已证实的进度。**
3. **Deadline 越近，系统越主动，但永不突破权限和预算。**
4. **模型不能靠一句“完成了”自行结案。**

### 1.1 何时适用

- 结果可被描述并至少部分验收；
- 工作可能跨会话、跨模型或跨天；
- 用户愿意预先限定权限、预算与执行器；
- 中间成果值得保存，即使最终未按时完成；
- Deadline 会影响资源和决策，而不只是提醒时间。

### 1.2 何时不适用

- 只需当前对话立即回答的一次性问题；
- 完成标准完全不可表达，且没有人愿意仲裁；
- 必须由固定脚本按固定时间执行的确定性动作；
- 不能给任何 Agent 足够权限完成工作；
- 用户要求无条件、无限预算地“直到成功”。

---

## 2. 类别边界与差异化

LHGP 不以“别人完全没有持久化目标”为前提。这个绝对主张在 2026 年已经不能成立。它的可防守创新是把下列能力统一成**面向最终用户的轻量协议**：

- 用户级目标承诺合同；
- Deadline 可行性与风险调度；
- 每合同限定 CLI、模型和角色；
- 跨会话、跨 Agent 的证据化接力；
- 与执行者分离的验收；
- 独立、可审计、可恢复的本地运行时。

| 系统 | 所有权中心 | 持久化什么 | 时间语义 | 执行者替换 | 完成裁决 |
|---|---|---|---|---|---|
| 会话内 Goal / Spec | 当前聊天或 run | 会话状态、计划 | 运行期间持续推进 | 通常在同一 harness 内 | 当前 Agent/流程 |
| cron / 定时自动化 | 时间表与固定动作 | 调度配置 | 到点触发 | 固定脚本或固定任务 | 退出码/规则 |
| LangGraph / Temporal 等 runtime | 开发者定义的 workflow | checkpoint / workflow state | 等待、重试、恢复 | 由开发者编排 | 工作流代码 |
| Agent memory | 用户/Agent 记忆空间 | 事实、偏好、历史 | 被调用时检索 | 不负责调度 | 不负责验收 |
| **LHGP** | **独立 Goal Commitment** | **合同、证据、计划投影、attempt 与风险历史** | **Deadline 驱动承诺风险** | **合同授权池内可替换** | **合同验收 + 独立证据** |

已知相邻系统包括：OpenAI Goal 要求相关工作留在同一 chat；LangGraph 提供 thread checkpoint 与 durable execution；OpenAI Agents SDK 通过 Dapr、Temporal、Restate、DBOS 等集成承载长等待和重启恢复；Abject 已展示跨机器存续的 goal 与动态多 Agent 重规划。LHGP 的定位必须建立在这些事实之上，而不是靠否认它们存在。来源见 §24。

---

## 3. 命名与标识

- 中文规范名：**远期目标协议**。
- 英文规范名：**Long-Horizon Goal Protocol**。
- 缩写与命名空间：**LHGP** / `lhgp`。
- 核心对象：`Goal Commitment`，中文称“目标承诺”或“目标合同”。
- `Task` 只表示计划中的工作单元，不得指代整个目标。
- `Attempt` 表示某个执行器的一次承办尝试。

参考实现最终命令建议为 `lhgp`，守护进程为 `lhgpd`，数据目录为 `~/.lhgp`。当前 `longtask`、`longtaskd`、`~/.longtask` MUST 在迁移期作为兼容别名保留，并输出弃用提示；不得直接破坏已有合同数据。

协议名称不等于产品品牌。未来 MAY 采用更短的产品品牌，但线协议、schema 和一致性套件统一使用 `lhgp`。

---

## 4. 领域对象与角色

### 4.1 领域对象

| 对象 | 定义 | 是否权威 |
|---|---|---|
| Goal | 用户希望世界最终呈现的结果 | 合同中的身份核心权威 |
| Goal Commitment | 目标、验收、Deadline、权限、预算与服务策略的版本化合同 | 是 |
| Plan | 当前实现路线和工作分解，可随证据修订 | 否；是可重建投影 |
| Work Unit | 计划中的有限工作单元 | 否；由计划版本定义 |
| Attempt | 一个 executor/model/session 对一个角色的一次执行 | 是，身份与结果不可变 |
| Checkpoint | attempt 对进度、剩余工作和证据的结构化提交 | 是，追加式 |
| Handover | 为下一 attempt 编译的接力视图 | 否；由权威记录重建 |
| Goal Capsule | 注入新执行者的最小、带来源上下文包 | 否；由合同与事件重建 |
| Artifact | 目标产生的交付物 | 外部事实；由哈希/路径引用 |
| Evidence | 支撑进度声明或验收判断的可定位事实 | 是，追加式 |
| Decision | 用户、运行时或获授权 Agent 作出的可审计决定 | 是，追加式 |

### 4.2 角色

| 角色 | 权限与责任 |
|---|---|
| Principal | 用户；批准、修订、取消合同，扩大权限或预算，处理仲裁 |
| Broker | 帮用户把模糊愿望整理为合同草案；无权自行批准 |
| Runtime | 合同与事件的权威持有者；不依赖某个模型存活 |
| Risk Controller | 估算 Deadline 风险并提出/执行合同允许的升级动作 |
| Dispatcher | 从合同授权池中选择合格 executor/model |
| Executor | 推进工作、提交 checkpoint 与候选交付物 |
| Verifier | 按验收条款核对证据；默认只读且与执行 attempt 隔离 |
| Adapter | 把协议语义翻译到具体 Agent harness，并报告可证明的能力 |
| Arbiter | 对 undetermined、Deadline miss 或权限扩张作人类裁决；默认是 Principal |

同一个软件进程 MAY 承担多个系统角色，但事件中必须标明逻辑角色。Executor 与 Verifier 的独立性必须按合同执行，不能因为实现方便而合并。

### 4.3 Goal 计划与阶段（Plan stages）

Goal 的 `plan` MAY 包含有序 `stages` 序列（工作分解）。每个 stage 是有限工作单元，可携带：

- `id`：阶段标识，用于绑定与推进。
- `acceptance_checks`：该阶段被判定完成前，绑定合同 MUST 覆盖的验收要求引用；规范化形式为 `kind:target`（与 §12.1 的 typed check 对齐）或遗留自由文本。
- `contract_id`：当前绑定到该阶段的合同，由 goal/prepare 写回；它是阶段计划与合同验收保持单一事实来源的手段，不允许计划与合同脱节。
- `draft`：可选的阶段性合同草案模板，供 goal/contract-draft 复用。

**阶段绑定不变式。** 当 goal/prepare 指定 `stage_id` 时，运行时 MUST：

1. 确认目标 Goal 存在，且 `id` 存在于 `plan.stages` 中；
2. 拒绝该阶段已被其他合同占用（`contract_id` 非空且不等于本次合同）；
3. 拒绝合同 `acceptance.checks` 未覆盖阶段 `acceptance_checks` 中全部要求的请求，且错误信息 MUST 列出缺失项，供模型调用方修正草案；
4. 通过后把 `contract_id` 写回阶段，使计划与合同验收保持单一事实来源。

阶段推进（goal/advance）只有当绑定合同的 `acceptance_status` 为 `passed` 时才允许；阶段完成态由证据推导，而非由执行者声明。检查身份的比较 MUST 在规范化身份（typed check 折叠为 `kind:target`，遗留文本按原文）上进行，不得依赖对象身份——typed check 携带不可哈希的 `args` 映射，直接做集合比较会崩溃而非给出可处理的拒接。

---

## 5. 十条不可破坏的不变式

一个实现只有满足下列要求，才可声称符合 LHGP：

1. **承诺外置。** 合同与已提交进度 MUST 不依附任何聊天、模型上下文或子进程内存。
2. **用户批准。** 模型 MAY 起草合同，但任何自主执行前 MUST 有 Principal 的明确批准记录。
3. **执行者可替换。** Attempt 失败 MUST NOT 自动等于 Goal 失败；合格的新执行者可从已提交状态继续。
4. **默认拒绝。** 未被当前合同授权的 executor、model、控制动作、路径、网络和预算 MUST NOT 被使用。
5. **紧迫不扩权。** Deadline 风险只能改变“何时做、由谁做、做多少并发”；MUST NOT 放宽硬约束、授权或预算。
6. **证据优先。** 模型叙述、退出码 0 或 attempt succeeded MUST NOT 单独触发目标满足。
7. **独立验收。** 除非合同明确选择 `verifier: none`，Goal 只有在全部强制 checks 通过后才能 `satisfied`。
8. **不伪造在线。** 机器、守护进程或 Agent 离线期间 MUST NOT 记录虚假推进；恢复后必须按墙钟和持久状态仲裁。
9. **不静默违约。** 预测不可行或 Deadline 已错过 MUST 产生可观察事件、风险解释和下一步选择。
10. **重复安全。** 唤醒、通知、spawn 和写回 MAY 至少一次；所有副作用接口 MUST 具备幂等键，所有 attempt 写回 MUST 受 fencing 保护。

---

## 6. 目标合同

### 6.1 规范示例

```yaml
api_version: lhgp/v1alpha1
kind: GoalCommitment
goal_id: goal-20260901-001
title: 统一插件架构并达到可发布状态

goal:
  intent: >-
    把当前原型整理为可在 GitHub 公开、可由新用户安装验证的插件。
  outcomes:
    - id: release-candidate
      deliverables:
        - path: .codex-plugin/plugin.json
        - path: README.md
        - path: docs/LHGP-SPEC.md

acceptance:
  verifier: cross_check
  independence:
    different_attempt: true
    different_session: true
    different_model_family: preferred
  checks:
    - id: plugin-valid
      type: schema
      path: .codex-plugin/plugin.json
      schema_ref: codex-plugin-manifest
    - id: quality-gate
      type: command
      command: [uv, run, python, scripts/quality_gate.py]
      expect_exit_code: 0
    - id: claims-honest
      type: human_or_model_review
      rubric: README 不声明未被证据支持的能力

deadline:
  due_at: "2026-09-08T18:00:00+08:00"
  confidence_target: 0.80
  start_policy: risk_optimized
  not_before: null
  on_miss: pause_and_arbitrate
  wake_service: local_best_effort

attention:
  notify_on: [need_user, risk_red, satisfied, missed]
  quiet_hours: { start: "23:00", end: "08:00", timezone: Asia/Shanghai }
  bypass_quiet_hours_on: [missed]

authority:
  executor_policy: explicit_allow
  executors:
    - executor_id: codex-cli
      models: [gpt-5.6-sol, gpt-5.6-terra]
      roles: [executor, planner]
    - executor_id: claude-code
      models: [sonnet]
      roles: [executor, verifier]
  required_capabilities: [spawn, observe, checkpoint, recover]
  allowed_controls: [notify, followup, steer, spawn]
  allow_parallel: false

constraints:
  workspace_root: D:/workspace/project
  file_effects: workspace-write
  deny_paths: []
  network: deny
  process: restricted
  package_install: deny

budget:
  max_attempts: 8
  max_concurrent_attempts: 1
  max_wall_minutes_per_attempt: 120
  max_escalations: 3
  verification_attempts_reserved: 2
  max_output_bytes: 1048576

continuity:
  checkpoint_max_age_minutes: 20
  checkpoint_on_material_change: true
  recovery_grace_minutes: 5
  capsule_max_tokens: 12000

revision: 1
approved_by: null
```

### 6.2 字段分组

- `goal`：目标身份与结果定义。
- `acceptance`：完成条件、检查方法与 verifier 独立性。
- `deadline`：决策边界、风险目标、错过后的动作与唤醒等级。
- `attention`：何时打扰用户、通知渠道、安静时间和紧急例外。
- `authority`：当前合同可使用的 executor、model、角色和控制动作。
- `constraints`：文件、网络、进程、安装等不可突破的强制边界。
- `budget`：attempt、并发、时间、升级、输出与费用上限。
- `continuity`：checkpoint、新鲜度、恢复宽限和上下文容量。

### 6.3 授权语义

全局 registry 只回答“系统发现了谁”，合同 `authority` 才回答“这个目标允许调用谁”。候选必须同时满足：

```text
globally_enabled
∧ contract_explicitly_allows(executor, model, role)
∧ capability_satisfies
∧ constraint_enforcement_proven
∧ budget_available
∧ concurrency_available
∧ verifier_independence_satisfies
```

`models: ["*"]` 只有 Principal 明确选择时才合法。新注册的执行器默认不得自动加入任何既有合同。模型 MAY 建议扩大授权，但扩大 executor/model、预算、路径、网络或控制范围 MUST 由 Principal 修订批准。

### 6.4 修订与身份

- 草案批准前所有字段可改。
- 批准后，每次修改产生不可变 `ContractRevision` 与审计事件。
- 软指引、通知偏好、计划策略可普通修订。
- Deadline、验收、授权、预算和约束的修改必须经过 Principal 批准。
- 原 Deadline 已错过后延期，旧 revision 的 `deadline_status: missed` MUST 保留；不得通过覆盖时间抹去违约历史。
- 若 `goal.intent` 或结果集合发生实质变化，应创建派生 Goal 并用 `supersedes` 关联，而不是把原承诺改成另一件事。

---

## 7. 四条独立状态轴

把合同、Deadline、验收和 attempt 塞进单一状态机会制造歧义。LHGP 使用四条相关但独立的状态轴。

### 7.1 Commitment lifecycle

```text
draft → active ↔ paused
           │  ↕
           │ blocked
           ├────────→ satisfied
           ├────────→ cancelled
           └────────→ archived（仅从终态或人工归档进入）
```

- `blocked` 必须携带稳定 reason code、解释、所需权限和可选动作。
- Deadline miss 默认使 lifecycle 进入 `blocked(deadline-missed)`，但不等于清空成果。
- `satisfied` 只能由 acceptance axis 的 `passed` 推导。

### 7.2 Deadline status

`not_due → at_risk → met | missed | waived`

- `at_risk` 可恢复为 `not_due`，但历史风险事件不可删除。
- 在 `due_at` 前 acceptance passed，记录 `met`。
- `now > due_at` 且未 passed，记录 `missed`，即使守护进程刚从关机恢复。
- 用户放弃 Deadline 可记 `waived`，但不得伪装成 `met`。

### 7.3 Acceptance status

`pending → candidate → verifying → passed | failed | undetermined`

- `failed` 进入 repair round 后可回到 `pending`，然后必须创建新的 verifier attempt。
- `undetermined` 默认使合同 `blocked(need-arbitration)`。
- 每次验证都有独立 evidence set；不得复用旧 verdict 直接完成新修订。

### 7.4 Attempt state

`admitted → starting → running ↔ waiting → succeeded | failed | cancelled | stale | orphaned`

- `succeeded` 表示该 attempt 正常交回候选成果，不表示 Goal 满足。
- `orphaned` 表示运行时重启后无法确认外部运行状态；宽限期后才可 fence 并重新派发。
- 所有终态不可变；重试创建新 attempt id。
- Attempt MUST 持久化其所属的 `contract_id` 与稳定 `goal_id`。同一 Goal
  可以按阶段绑定多份合同；恢复、预算、租约和验收不得仅凭 `goal_id`
  猜测当前合同。旧数据若无法唯一解析合同，运行时 MUST fail-closed，
  不得对错误合同续租或写回。

---

## 8. 逻辑架构

```text
┌────────────────────────────────────────────────────────────┐
│ Client surfaces: CLI / MCP / Codex plugin / future UI      │
├────────────────────────────────────────────────────────────┤
│ Contract API │ Event API │ Principal approval / arbitration│
├────────────────────────────────────────────────────────────┤
│ Risk Controller │ Dispatcher │ Verifier Coordinator        │
├────────────────────────────────────────────────────────────┤
│ Goal Capsule Compiler │ Attempt Supervisor │ Reconciler     │
├────────────────────────────────────────────────────────────┤
│ Adapter SDK: Codex / Claude / agent-cli / Hermes / other CLI     │
├────────────────────────────────────────────────────────────┤
│ Authoritative Store: contracts, events, leases, evidence   │
└────────────────────────────────────────────────────────────┘
```

### 8.1 规范与实现分离

- **LHGP 规范**定义对象、状态、事件、不变式与一致性场景。
- **参考运行时**可使用 Python + SQLite，实现本机单用户版本。
- **适配器**把某个 harness 的 session、model、sandbox 与控制能力映射到协议。
- **插件**提供安装、MCP 工具和 Skill，不持有合同权威状态。

任何第三方实现只要通过同一 schema 与 conformance suite，MAY 不使用 Python、SQLite、MCP 或守护进程形态。

### 8.2 组件职责

- Contract Store：事务化保存版本、事件、幂等键和租约。
- Projector：生成可读文件；投影可落后、可重建、不可超前。
- Capsule Compiler：从权威记录编译本次 attempt 的最小上下文。
- Attempt Supervisor：spawn、observe、checkpoint、cancel、collect。
- Reconciler：运行时启动后恢复外部 attempt 观察关系。
- Risk Controller：计算 finish forecast、风险等级与下一检查点。
- Dispatcher：只从合同授权且可证明合格的候选中选择。
- Verifier Coordinator：管理 candidate → verify → repair → reverify。
- Wake Service：只负责唤醒/通知，不拥有合同或裁决权。

---

## 9. 事件驱动控制循环

参考运行时 MUST 以持久事件而不是“每 60 秒问一次模型”驱动工作。一次控制循环按以下顺序进行：

1. 读取新适配器事件、心跳、用户修订与墙钟。
2. Reconciler 核对所有非终态 attempt 的外部状态。
3. 在事务中投影合同、Deadline、验收、预算和租约状态。
4. Risk Controller 计算 forecast、置信度、风险档和 `next_decision_at`。
5. Policy Engine 选择零个或一个主要动作：等待、提醒、控制现有会话、checkpoint、换人、验证、请求用户。
6. 先提交带 `decision_id` 的决定事件，再幂等执行外部副作用。
7. 根据执行结果写回 accepted/rejected/unknown，并计算下一唤醒。

唤醒源包括：

- `next_decision_at` 到达；
- attempt 心跳、新 checkpoint 或终态；
- 租约即将过期；
- 用户批准、修订、暂停或取消；
- 适配器健康/容量变化；
- acceptance candidate 产生；
- Deadline 或风险阈值即将穿越；
- 系统启动后的全量 reconciliation。

当没有可执行动作时，运行时 MUST 等待下一事件，不得调用任何 LLM 进行“空转检查”。固定短周期扫描 MAY 作为进程健康保底，但扫描本身不得产生模型调用或重复通知。

---

## 10. Deadline：从定时器升级为风险控制

### 10.1 语义

`due_at` 是目标承诺的**决策边界**：在此之前，系统应争取获得 acceptance passed；在此之后，系统必须如实记录 miss 并执行 `on_miss`。它不是 spawn 时间，也不是杀死进程的时间。

LHGP 不使用“能保证任意 AI 工作准时完成”的表述。一个符合规范的实现应向用户承诺：

1. 立约时做 admission forecast，明显不可行时不静默接单；
2. 运行中持续用新证据修正预测；
3. 在合同权限和预算内提前升级；
4. Deadline 到达时给出已验收结果，或可审计的 miss、部分成果和下一步选项。

### 10.2 完成时间估计

每个计划版本至少维护下列分量的中位数与保守估计；推荐使用 p50/p90：

```text
T_required,p = T_queue,p
             + T_startup,p
             + T_remaining_work,p
             + T_verification,p
             + T_retry_reserve,p
             + T_safety_margin

slack_p = (due_at - now) - T_required,p
```

- `T_queue`：合格执行器容量等待。
- `T_startup`：进程、会话、模型和上下文启动成本。
- `T_remaining_work`：基于 work units 与 checkpoint 的剩余工作。
- `T_verification`：至少一次独立验收及证据收集。
- `T_retry_reserve`：失败、修复和重验的预算储备。
- `T_safety_margin`：时钟误差、唤醒延迟与未知风险。

有历史样本时 SHOULD 计算 `P(finish <= due_at)`；样本不足时 MUST 标记 `confidence: low`，可使用 p90 加总作为保守回退。只用 `initial_hours / time_left` 的实现只能声明 `forecast_level: coarse`，不得称为精确风险预测。

`forecast_level: historical` 表示估计主要来自本地历史 attempt 时长，尚未完成
回放校准与置信区间验证；只有经过独立校准流程的实现才能使用
`forecast_level: calibrated`。模型不得仅凭样本数量把 historical 解读为高精度保证。
Deadline snapshot MUST also expose `sample_count` and `p_finish_basis` so a model
can distinguish `empirical-success-cdf`, `coarse-heuristic`, and `unavailable`
probability evidence without guessing from the numeric value alone.

### 10.3 风险档与动作

默认档位可配置，但语义固定：

| 档位 | 默认条件 | 主要动作 |
|---|---|---|
| unknown | 无新鲜估计或执行器状态不明 | 请求 checkpoint/health；无法刷新则向用户暴露未知风险 |
| green | `P_finish ≥ 0.85` 且 `slack_p90 ≥ 0` | 安静等待下一有意义事件 |
| yellow | `0.65 ≤ P_finish < 0.85` | 提醒当前 attempt 更新估计；预留 verifier 容量 |
| orange | `0.40 ≤ P_finish < 0.65` 或停滞 | steer、缩短 checkpoint 周期、准备串行换人 |
| red | `P_finish < 0.40` 或 `slack_p90 < 0` | 在授权内使用更合适执行器/模型、安全分区并行，或立即请求用户缩范围/扩预算/延期 |
| missed | `now > due_at` 且未通过验收 | 原子记录 miss，按 `on_miss` 暂停仲裁或继续迟到执行 |

风险升级的候选动作还必须通过 authority、constraints、budget 与 cooldown。没有合法动作时，系统应立刻 `blocked(need-user)`，而不是不断重试同一不可行方案。

### 10.4 Admission control

`goal/prepare` MUST 返回一份 offer，而不是直接承诺执行。offer 至少包含：

- 可用和被拒绝的 executor/model 及原因；
- 当前验收可执行性；
- p50/p90 完成预测与置信度；
- verification reserve 是否足够；
- 预计最晚安全启动时刻；
- 已知不受运行时控制的风险；
- 可声明的 continuity / wake / sandbox 保证等级。

如果没有合格执行器、没有可执行验收、预算不含一次验证或 p90 已超过 Deadline，默认 MUST 拒绝进入 active。Principal MAY 选择“明知高风险仍批准”，但合同和 UI 必须标为 `accepted_with_risk`，保留当时 forecast。

### 10.5 用户节奏与通知

用户不是通过手写一串 cron 表达节奏，而是通过以下约束表达意图：`due_at`、`confidence_target`、`start_policy`、`not_before`、预算、允许的 executor/model、并发策略和 `attention`。Risk Controller 再根据实际进度决定何时推进。

### 10.6 Deadline Decision Reliability v1（单机范围）

Developer Preview 的下一阶段目标不是宣称“按时完成”，而是让每一次 Deadline 决策都可解释、可审计、可恢复。对每个非终态合同，守护进程在一次有效 tick 后 SHOULD 保留最新的 immutable Deadline snapshot，至少包含：`computed_at`、`due_at`、六项 forecast、`forecast_p50`、`forecast_p90`、`slack_p50`、`slack_p90`、`p_finish`、`confidence`、`forecast_level`、`risk_tier`、`next_decision_at` 及计算原因。

Snapshot 的硬不变量：

1. `due_at == now` 不算 miss；只有 `now > due_at` 且未验收通过才记录 `missed`。
2. 任一必需估计缺失、样本不足或估计过期时，置信度 MUST 降为 `low`，精度 MUST 标为 `coarse`；不得输出看似精确的概率。
3. `slack_p90 < 0` 或 `p_finish < 0.40` MUST 至少进入 `red` 风险解释；风险降级、跨档和 deadline miss 必须有去重事件。
4. 同一合同 revision 在 snapshot 内容未变化时不得刷写重复 forecast 事件；合同修订或事实变化后必须重新计算。
5. `next_decision_at` 是下一次必须重新审视的最早时刻；它不得晚于 `due_at - safety_margin`，且在过去的决策点必须立即重算。

本版本仅承诺本机守护进程、SQLite/WAL、L0/L1 唤醒和本地事件审计。跨主机、跨网络 RPC/relay、云端唤醒、外部通知送达保证以及严格墙钟结果保证均为明确非目标，不得作为实现完成度或 SLA 宣称。

- `start_policy: eager` 表示批准后尽快开始；`risk_optimized` 表示在满足置信目标的前提下选择启动与检查时刻；`not_before` 是绝对禁止提前执行的下界。
- 运行时 MUST 在 `need_user`、无法维持目标置信度、`satisfied`、`missed` 和预算耗尽时，根据合同发送通知。
- 通知采用 outbox + idempotency，语义为至少一次。`sent` 只代表渠道接受，`delivered`/`acknowledged` 必须由渠道证据支持。
- 安静时间可抑制普通提醒，但不得抑制合同明确列入 `bypass_quiet_hours_on` 的事件。
- 重复风险评估若未跨档、事实未变化，不得重复骚扰用户。

---

## 11. 跨会话与多 Agent 接力

### 11.1 Goal Capsule

新 attempt 不应读取整个聊天历史。运行时为每次 attempt 编译独立、不可变的 Goal Capsule，至少包含：

1. `contract_anchor`：当前合同 revision、目标、Deadline、验收与硬约束摘要；
2. `authority_digest`：本 attempt 的角色、允许动作、executor/model 与预算余额；
3. `plan_snapshot`：当前计划版本、依赖与本次 work unit；
4. `verified_progress`：已完成声明及 evidence 指针；
5. `open_work`：剩余工作、优先级和估计区间；
6. `decisions`：仍有效的用户/架构决定；
7. `risks_and_blockers`：风险、假设与需要用户的信息；
8. `handover`：上一 attempt 的最后 checkpoint 与建议下一步；
9. `provenance`：所有片段的 event id、revision、hash 与生成时间。

Capsule 中必须区分 `fact`、`decision`、`hypothesis`、`untrusted_content`。来自网页、待处理文档或旧模型的文本默认是数据，不得因被写入 handover 就升级为系统指令。Capsule 丢失或过期时可重建；它永远不是第二真相源。

### 11.2 Checkpoint 协议

Executor 在发生实质进展、收到 checkpoint 请求、即将退出或超过 `checkpoint_max_age` 时，必须提交：

```json
{
  "attempt_id": "att-...",
  "lease_generation": 4,
  "plan_revision": 7,
  "completed_claims": [
    {"work_unit": "wu-3", "claim": "...", "evidence_ids": ["ev-..."]}
  ],
  "remaining_work": [
    {"work_unit": "wu-4", "p50_minutes": 35, "p90_minutes": 80}
  ],
  "next_action": "...",
  "risks": [],
  "requested_decisions": []
}
```

自由文本 MAY 作为说明，但不替代结构化字段。估计变化必须引用来源 attempt；连续两次估计不下降不自动证明停滞，Risk Controller 还应检查 evidence、日志与外部状态。

### 11.3 持久外部句柄与重启恢复

任何可长期运行的 adapter 在 spawn 成功后 MUST 持久返回：

- `external_run_id`：目标 harness 的稳定运行标识；
- `session_locator`：重新观察或发送控制的定位信息；
- `recovery_strategy`：`reattach | poll | nonrecoverable`；
- `process_identity`：PID/启动时间等提示，不得单独作为身份真相；
- `capability_snapshot`：本次实际可用的 observe/cancel/checkpoint/control 能力。

运行时启动后必须先 reconcile：

1. 能确认同一外部 run 仍活着 → 重新绑定并续租；
2. 能确认已终止 → collect 结果并结算 attempt；
3. 状态未知 → 标记 `orphaned`，在 recovery grace 内不得重复 spawn；
4. 宽限后仍未知 → fence 旧 generation，记录风险，再决定重新派发；
5. 旧 attempt 后续写回 → `LEASE_FENCED`，但原始外部日志 MAY 作为非权威附件保留。

只把 `subprocess.Popen` 存在内存中的实现不符合跨守护进程重启连续性要求。

### 11.4 执行器选择

Dispatcher 不是“最便宜优先”。它应在硬过滤后，按当前角色优化：

```text
score = finish_probability
      + capability_fit
      + recovery_quality
      + historical_reliability
      + verifier_independence
      - expected_cost
      - queue_delay
      - context_transfer_loss
```

权重是运行时策略，不写进线协议；但每次选择必须记录候选、拒绝原因和可解释排序因子。Deadline 越紧可以改变权重，不能改变候选合法性。

### 11.5 并行

MVP 默认串行接力。只有计划显式给出互斥分区、写入范围、合并策略和分区验收时，才允许并行。不能安全分区的目标 MUST 串行换人。任何自动切分都只能提出计划修订，不能绕过租约与用户约束。

---

## 12. 验收、修复与完成

### 12.1 检查类型

验收 check SHOULD 尽量使用可重复验证的类型。线协议中的 `kind` 是以下七个
固定枚举值（概念上的 artifact/schema/command 等分类映射到这些值；模型和
客户端 MUST 使用线协议值，不得发送下划线别名）：

| `kind` | 语义 | 参考实现的自动判定 |
|---|---|---|
| `file-exists` | 交付文件存在 | 确定性 pass/fail |
| `file-content-matches` | 文件包含文本、正则或 SHA-256 | 确定性 pass/fail/undetermined |
| `command-exit-zero` | 在 workspace 中以结构化 argv 执行命令并要求退出码 0 | pass/fail/undetermined |
| `artifact-present` | 声明的产物已生成 | 确定性 pass/fail |
| `structure-valid` | 产物结构可解析（当前参考实现支持 JSON） | pass/fail/undetermined |
| `observable` | 需要 verifier 观察的外部事实 | 由 verifier/人工仲裁 |
| `user-assertion` | 需要用户确认的事实 | 由用户仲裁 |

`artifact_exists` / `artifact_hash`、`schema` / `structured_query`、
`diff_policy` / `path_policy`、`model_review`、`human_review` 和
`composite` 是设计层概念；它们尚未作为额外 wire `kind` 发布。需要这些
语义时，合同应使用上表中最接近的 typed check，或使用 `observable` 并由
verifier 提供结构化证据。组合 all/any/threshold 由合同的多个 checks 与
mandatory/optional 规则表达，而不是自行发明新的 kind。

自然语言 verifier 判断是可用证据，但置信等级低于确定性 check。合同应明确哪些 check 是 mandatory，哪些允许 undetermined。

**target 解析约定**：文件类与命令类 check 的 `target` 一律**相对
`workspace_root` 解析**（不是相对仓库根、不带 workspace 前缀重复）。
声明 `charfreq.py` 在 `workspace_root=ws/` 下即指向 `ws/charfreq.py`；
声明 `ws/charfreq.py` 会指向 `ws/ws/charfreq.py`。

**command check 的执行环境契约**：命令以结构化 argv、`shell=False`、
`cwd=workspace_root`、**守护进程环境**（继承 daemon 的 PATH）执行。
守护进程环境通常不含项目虚拟环境——声明者 SHOULD 使用可解析的
解释器绝对路径，或依赖 §12.4 的裁决合成（协议侧 undetermined 由
verifier 的判定块填补）。命令不可执行（解释器缺失等）产出
`undetermined` 而非 `fail`——「跑不了」与「跑挂了」是不同的事实。

命令类 check MAY 在 `args.timeout_seconds` 中声明更短的超时。参考实现对
每次命令执行施加不超过 60 秒的硬上限，并将剩余 Deadline 时间作为更严格的
运行时上限；超时结果 MUST 是 `undetermined`，由 verifier 判定块或人工仲裁
补充证据，不能把超时伪装成通过或失败。这样一个失控命令不会阻塞 daemon 的
Deadline 决策和用户控制面。

### 12.2 独立性

默认 verifier：

- 使用不同 attempt id 和 session；
- 只读交付物和证据；
- 不继承 executor 的自由对话上下文；
- 不使用同一外部 run；
- 预算允许时优先不同 model family 或 executor；
- 不能修改 acceptance 条款来让结果通过。

### 12.3 Repair loop

```text
executor candidate
  → verifier #1
    → passed → satisfied
    → failed → repair brief → executor/repair attempt
                            → verifier #2 → ...
    → undetermined → human arbitration
```

每次 verifier failure 必须生成结构化 repair brief：失败 check、证据、最小修复范围、是否影响既有通过项。历史 verifier 的存在不得阻止新的 verifier。每次验证消耗保留预算；该预算按 `contract_id` 隔离统计（同一 Goal 下不同阶段合同不得互相消耗）；当验证预算不足时，合同必须 blocked，而不是跳过验收。

`Goal satisfied` 的唯一合法推导是：当前 contract revision 的所有 mandatory checks 已有未过期 evidence，并由允许的验收路径产生 `acceptance.passed` 事件。

### 12.4 验收证据通道与裁决合成

verifier 报告验收结果的通道有两条，按 harness 能力选择：

1. **RPC `attempt/write-back`**（会话型 harness）：verifier 在会话内调用
   协议 RPC，终态必须携带 evidence 列表（每条 check_id/outcome/source）。
2. **stdout 判定块**（一次性 CLI harness）：headless CLI verifier 无法调
   RPC——task_prompt MUST 指示其在输出末尾写一个机器可读判定块：

   ````
   ```lhgp-verdict
   {"verdict": "succeeded", "checks": [{"check_id": "file-exists:x.py", "outcome": "pass", "source": "ws/x.py"}]}
   ```
   ````

   运行时解析最后一个 `lhgp-verdict` 块；缺失或非法 JSON 时该通道视为
   无证据（不猜、不静默兜底）。

**裁决合成规则**（确定性评估 × 模型观察，逐 check）：

- 协议确定性评估产出 `pass`/`fail` 时，**确定性结果优先**——模型观察
  不得覆盖机器可复现的证据（防 verifier 橡皮图章）；冲突如实记录进
  evidence 的 `model_outcome` 字段供审计。
- 协议确定性评估产出 `undetermined`（如命令在守护进程环境不可执行）
  时，模型观察的显式 `pass`/`fail` **填补**该 check 的裁决——模型
  verifier 真实执行过核验命令（observable 证据，§12.1），其结论强于
  「无法判定」。
- 双方均无显式结果 → `undetermined`，走人工仲裁（§12.3）。

**用户触发验收（`contract/request-verification`）**：Principal MAY 在
任意非终态合同上直接请求验收——典型场景是执行预算耗尽但交付物疑似
已就绪（`blocked(need-user)`）。运行时 MUST：

1. 校验合同非终态且无进行中的 verifier attempt；
2. 验证预算允许（§12.4 独立记账；预算耗尽时如实拒绝并说明升级路径）；
3. 把合同恢复为可验证状态（blocked → active，保留升级历史）后派生
   verifier attempt，`requested_by=user` 落事件供审计。

该通道只派 verifier、不派 executor——它表达「先看看现状算不算完成」，
不是「再干一轮」。

---

## 13. 权威存储与事件模型

### 13.1 最小持久实体

参考实现至少需要：

- `goals`：稳定 goal identity；
- `contract_revisions`：不可变合同版本与批准信息；
- `events`：追加式领域事件；
- `attempts`：所属 contract、Goal、角色、executor、model、外部句柄和终态；
- `leases`：contract/partition、generation、holder、expiry；
- `checkpoints`：结构化进度与估计；
- `artifacts`：位置、hash、media type 与产生 attempt；
- `evidence`：check、结论、来源与有效 revision；
- `decisions`：用户/运行时决定及其所属 contract、Goal 与 revision；
- `idempotency`：request id、payload hash、原结果；
- `wakeups`：计划、触发、降级与重复去重。

状态快照、事件和幂等记录必须在同一事务提交。文件投影只能在事务提交后生成；损坏或落后时从权威数据重建。

模型侧读取合同（`contract/get`，以及对应的 MCP 工具）MUST 在权威合同视图之外返回
`decision_history` 数组。数组按 `recorded_at` 倒序，仅包含该 `contract_id` 的决定，
并保留 `decision_id`、`contract_revision`、`tier`、`decision_type`、`reason`、预算余量、
`payload`、`recorded_at` 与 `actor`。旧库中无法证明合同归属的历史行 MUST 不得被猜测拼入，
以免跨合同污染模型上下文；调用方可用 `decision_limit` 限制返回条数。
同一响应 SHOULD 提供 `attempt_history`：仅列出该合同的 attempt，包含角色、执行器/模型、
状态、版本、时间、返回码、错误类别与恢复策略；不得用 Goal 级历史替代合同级归属。
调用方可用 `attempt_limit` 限制返回条数（参考实现默认 20，最大 100）。
参考实现还提供 `verification_history`（最近 20 条），明确呈现用户验收请求、
daemon 消费结果和 verifier 启动事件，模型无需扫描原始事件流即可判断验收请求是否已兑现。
`lhgp doctor` SHOULD also validate the launch executable of every enabled registry
entry and report missing commands before dispatch, without consuming contract budget.

唤醒层降级事件（`wakeup/degraded`）按状态边沿记录：同一层持续不可用时
MUST NOT 按 daemon tick 重复写入；该层恢复后再次失效时 SHOULD 重新记录，
以保留可审计的故障转变而不制造事件空转。
注销已登记的唤醒任务若失败，运行时 MUST 保留登记并在后续 tick 重试；只有
注销成功后才可移除本地登记，防止操作系统中的计划任务残留。
对 active 合同，L1 一次性唤醒的目标时刻 MUST 取
`min(next_wakeup_at, next_decision_at, due_at - safety_margin)`（忽略缺失值）。
较晚的 Deadline 安全边距不得遮蔽更早的决策点；否则实现不能声称对
`next_decision_at` 提供唤醒对齐。若该最早时刻已在过去，目标 MUST 钳制为
当前时刻，表示立即重算，不得把过去时间直接交给平台调度器。
参考实现的 Windows L1 端口使用一次性 Task Scheduler 任务；任务动作只能
调用本机认证的 `daemon/wake` RPC，唤醒信号写入 `wakeup/rtc-fired` 后由
daemon 下一轮消费。计划任务不得直接读取或修改合同状态。

### 13.2 事件族

事件名采用 `lower_snake_case` 载荷和 `domain/action` 类型，例如：

- `goal/prepared`, `goal/approved`, `goal/amended`, `goal/blocked`；
- `deadline/risk_changed`, `deadline/missed`, `deadline/waived`；
- `attempt/admitted`, `attempt/started`, `attempt/orphaned`, `attempt/succeeded`；
- `checkpoint/committed`, `capsule/built`, `capsule/rejected`；
- `dispatch/selected`, `dispatch/refused`；
- `verification/requested`, `verification/consumed`, `verification/started`,
  `verification/failed`, `verification/passed`；
- `lease/acquired`, `lease/renewed`, `lease/fenced`；
- `wakeup/armed`, `wakeup/fired`, `wakeup/degraded`。

事件至少包含 `event_id`、`goal_id`、`contract_revision`、`attempt_id?`、`actor`、`role`、`request_id`、`created_at`、`payload_schema_version` 和 `payload`。

### 13.3 文件投影

建议目录：

```text
~/.lhgp/
  state.db
  registry.yaml
  goals/<goal-id>/
    contract.yaml
    status.json
    plan.md
    progress.md
    evidence.jsonl
    events.jsonl
    capsules/<attempt-id>.md
    attempts/<attempt-id>.json
```

用户直接编辑投影只形成 draft。提交修订必须经过 API、CAS 和权限检查。用户 MAY 导出、版本控制或 grep 投影，但不得把直接改 SQLite 当成合法状态变更。

---

## 14. 协议暴露面

线协议使用版本化 request/response 与事件 cursor；本机参考实现 MAY 继续使用 JSON-RPC 2.0。协议 API 与模型工具必须分开，避免把 30 个底层 RPC 机械暴露给模型。

### 14.1 Principal / client API

```text
protocol/hello
goal/prepare          # 返回 admission offer
goal/approve
goal/get
goal/list
goal/amend
goal/pause
goal/resume
goal/cancel
goal/arbitrate
goal/events
executor/list
executor/health
executor/enable
executor/disable
runtime/status
runtime/kill_switch
```

### 14.2 Attempt API

```text
attempt/get_assignment
attempt/checkpoint
attempt/propose_plan
attempt/submit_candidate
attempt/report_failure
attempt/status
lease/renew
lease/release
artifact/register
evidence/register
```

Attempt 凭证必须限定到单个 `goal_id + attempt_id + lease_generation + role`，不得调用 Principal-only 的授权、预算或 Deadline 修订。

### 14.3 Adapter API

```text
describe
health
prepare
spawn
observe
control
checkpoint_request
cancel
collect
recover
```

`prepare` 返回本次实际 enforcement proof；manifest 只是声明。`spawn` 只接收结构化 argv、cwd 与环境白名单。任何模型输出都按不可信数据处理，不能直接拼成 shell 命令。

### 14.4 MCP 工具面

面向一般 Agent 的最小工具建议：

- `lhgp_prepare_goal`
- `lhgp_approve_goal`
- `lhgp_get_goal`
- `lhgp_list_goals`
- `lhgp_amend_goal`
- `lhgp_pause_goal`
- `lhgp_resume_goal`
- `lhgp_cancel_goal`
- `lhgp_submit_checkpoint`
- `lhgp_submit_candidate`

MCP 传输层 MAY 无会话；每次调用显式携带 `goal_id`、`attempt_id` 或 capability handle。工具必须带正确的 read-only/destructive/open-world annotations，并对需要 Principal 权限的操作要求可验证批准。

### 14.5 兼容迁移

旧 `contract/*` 方法和 `longtask_*` MCP 工具 MAY 在一个次版本周期内代理到新资源，并返回 deprecation metadata。持久数据升级必须提供 dry-run、备份、可重复迁移和 schema version 检查。

---

## 15. Codex 插件只是一个发行载体

一个完整 Codex 插件包至少包含：

```text
.codex-plugin/plugin.json
.mcp.json
skills/long-horizon-goals/SKILL.md
assets/                     # 可选
```

推荐 manifest 名为 `long-horizon-goals`，展示名为 `Long-Horizon Goals` 或 `远期目标协议`。插件应完成三件事：

1. 安装并连接本地 LHGP MCP server；
2. 用 Skill 教模型如何起草合同、解释风险、提交 checkpoint；
3. 提供少量面向用户任务流的工具，而不是暴露数据库或完整 RPC 隧道。

插件卸载不得删除 Goal 数据；MCP client 断开不得影响 `lhgpd` 持有的承诺。正式发布前必须通过官方插件 manifest validator，且 `.codex-plugin/plugin.json`、`.mcp.json` 与 Skill 必须真实进入分发包。

---

## 16. 安全与权限

### 16.1 权限矩阵

| 操作 | Principal | Broker | Executor | Verifier | Runtime |
|---|---:|---:|---:|---:|---:|
| 起草合同 | ✓ | ✓ | 建议 | — | 校验 |
| 批准/扩权/扩预算/延期 | ✓ | — | — | — | 执行批准结果 |
| 修改计划 | ✓ | 建议 | 提议 | — | 提交版本 |
| 写工作区 | 依环境 | — | 按合同 | 默认否 | — |
| 提交 checkpoint | — | — | ✓ | ✓ | 校验/fence |
| 通过验收 | 仲裁 | — | — | 按 checks | 推导状态 |
| 选择执行者 | 策略批准 | — | — | — | 在授权池内选择 |

### 16.2 必须执行的安全规则

- 合同硬约束必须由 adapter 或操作系统机制执行；提示词不是安全边界。
- 路径先 canonicalize，再检查 workspace root、deny paths、符号链接与 junction 逃逸。
- 网络、进程和 package install 能力无法证明时必须拒接。
- 环境变量默认不透传；凭证使用 scoped secret handle，不写入 Capsule 或事件正文。
- verifier 默认只读；需要写入测试产物时使用隔离临时目录。
- Goal Capsule 中的外部内容与历史模型文本必须标为 untrusted。
- 每个控制动作记录目标 session、actor、授权依据和 adapter 返回值；“已入队”不得记录成“已执行”。
- kill switch 必须立即阻止新 dispatch，并尽力停止或冻结已有 attempt；不得删除数据。
- 运行时不得自行扩大 executor/model allowlist、并发、预算、网络、路径或 Deadline。

---

## 17. 故障与恢复语义

| 故障 | 必须行为 |
|---|---|
| 原聊天关闭 | 不影响合同；现 attempt 由 adapter observe，不能观察则按 recovery strategy 处理 |
| Agent 子进程崩溃 | 保存最近 checkpoint，attempt failed，预算允许时换人 |
| 守护进程重启 | 先 reconcile 非终态 attempt，禁止直接重复 spawn |
| 机器关机跨 Deadline | 启动后按墙钟记录 deadline/missed；不得伪造期间进度 |
| 租约过期后旧 Agent 写回 | `LEASE_FENCED`；不改变权威状态 |
| 无合格 executor/model | blocked(no-authorized-executor)，列出每个候选拒绝原因 |
| 估计过期 | risk unknown；请求 checkpoint，不能继续展示旧“绿色”状态 |
| verifier fail | 生成 repair brief，回到 repair loop，不得永久禁止重验 |
| verifier undetermined | blocked(need-arbitration)，保留所有 evidence |
| 预算不足以验证 | blocked(verification-budget-exhausted)，不得跳过 verifier |
| 重复 wake/spawn 请求 | request id 幂等返回原结果；attempt id 不重复 |
| 投影损坏 | 从权威库重建并记录事件；权威库损坏则 fail closed |

---

## 18. 参考实现的目标模块边界

当前 Python 参考实现可演进为：

```text
src/lhgp/                    # 最终包名；迁移期可由 longtask 代理
  domain/                    # Goal、Contract、Attempt、Evidence、状态推导
  store/                     # SQLite 事务、事件、迁移与投影
  continuity/                # Capsule、checkpoint、handover、reconcile
  risk/                      # forecast、Deadline、策略与 next_decision_at
  dispatch/                  # registry、authorization、candidate scoring
  verification/             # checks、evidence、repair/reverify
  adapters/                  # 公共接口与具体 harness 适配器
  api/                       # JSON-RPC 与 capability tokens
  surfaces/cli/              # 人类 CLI
  surfaces/mcp/              # 模型工具面
```

依赖方向应保持领域层向外无依赖：

```text
surfaces → api → risk/dispatch/verification/continuity
         → adapters → store → domain
```

一个适合本项目的代码风格示例：

```python
@dataclass(frozen=True, slots=True)
class AttemptHandle:
    attempt_id: str
    external_run_id: str
    recovery_strategy: RecoveryStrategy


def may_dispatch(contract: GoalCommitment, candidate: Candidate) -> Refusal | None:
    """返回明确拒绝原因；合法时返回 None，绝不静默降级。"""
```

领域值对象使用 immutable dataclass/enum；副作用在端口层；所有错误使用稳定 code + details；所有时间必须带时区；所有外部 argv 都是字符串数组。

---

## 19. 当前实现到目标设计的迁移

### 19.1 保留

- SQLite/WAL 权威状态与追加事件方向；
- 文件投影与可重建上下文方向；
- lease generation fencing；
- 结构化 argv、环境白名单与 fail-closed 约束翻译；
- CLI、MCP、质量门与 conformance 测试基础；
- 独立 verifier 和分层 wakeup 的设计基础。

### 19.2 必须替换或补齐

1. `LongTask` 产品语言与公开标识迁移为 LHGP；
2. runtime 对 schema 做唯一、完整验证，消除 JSON Schema、dataclass 与 CLI 默认值漂移；
3. 合同加入 executor/model/role 授权矩阵，调度从全局 enabled 改为双重授权；
4. attempt 持久化外部 run handle，守护进程重启实现 reconcile/reattach；
5. checkpoint 提供滚动 p50/p90 估计，风险引擎不再永远使用 initial hours；
6. reminder/steer/spawn 必须产生真实 adapter 动作与回执，不只记事件；
7. verifier fail → repair → 新 verifier → pass 的多轮闭环；
8. verification attempts、escalations 与并发都计入真实剩余预算；
9. 事件驱动 `next_decision_at` 替代高频无意义扫描；
10. 补齐 `.codex-plugin/plugin.json`、`.mcp.json`、Skill metadata、MCP annotations 和打包清单；
11. README 从“后台跑任务”重写为“目标不属于会话”，并删掉超出证据的承诺。

### 19.3 迁移顺序

不得先做全仓机械 rename。正确顺序是：

1. 确立本规范和 ADR-003；
2. 新增当前仓库的 contract schema v2（承载 `lhgp/v1alpha1`）与读旧写新 migration；
3. 让行为闭环符合规范；
4. 引入 `lhgp` 公开命令和数据目录迁移工具；
5. 保留 `longtask` 兼容别名至少一个次版本；
6. 最后迁移 Python 内部模块路径与删除别名。

别名轨在删除前必须先补齐正名缺口（2026-09-10：`lhgp_user_confirm_spec_verdict` 已补——此前签字门只有 `longtask_user_confirm_spec_verdict` 一个 MCP 入口，直接清别名等于删掉 CANDIDATE→PASSED 的唯一程序化路径）。工具面按角色收窄的 profile 机制见 DESIGN §11.8。

---

## 20. 分阶段实施计划

### M0：语义收敛与诚实发布

- 本规范、ADR、术语表成为评审依据；
- README 只陈述已证实能力；
- `quality/claims.json` 把设计声明与实现声明分开；
- 新增命名迁移说明。

验收：任意新贡献者能准确说出 Goal、Contract、Task、Attempt 的区别；README 不再把项目描述为 cron 或普通 durable task runner。

### M1：合同授权与仓库 schema v2

- 单一 runtime validator；
- executor/model/role allowlist；
- admission offer 与 Principal approval；
- deadline/acceptance/lifecycle 三轴投影；
- 旧合同 dry-run migration。

验收：同一个全局 registry 中，合同 A 可只允许 Codex + 指定模型，合同 B 可只允许 Claude；任何未授权候选均被一致拒绝并给出原因。

### M2：真正的连续性闭环

- Goal Capsule v1；
- 结构化 checkpoint；
- 外部 run handle 与 recovery strategy；
- daemon startup reconciliation；
- orphan grace、fencing 与安全 redispatch。

验收：杀死原会话、重启 daemon，再由不同授权 CLI 继续；没有重复 attempt、丢失已提交进度或旧写回污染。

### M3：Deadline 风险控制

- p50/p90 工作量与验证储备；
- admission forecast；
- 风险档、next_decision_at、真实 remind/steer/swap；
- miss 仲裁与预测校准记录。

验收：在模拟时钟中可证明各阈值动作；无动作窗口模型调用数为零；风险预测和实际完成时间可回放比较。

### M4：验收修复闭环

- typed checks 与 evidence；
- verifier independence；
- repair brief；
- 任意多轮 reverify，受预算限制；
- completion 只由当前 revision 的 pass 推导。

验收：`executor success → verifier fail → repair → verifier pass → satisfied` 真实 E2E 通过；首个 verifier 的历史不会阻止第二个 verifier。

### M5：插件与公开 Alpha

- 正式 Codex plugin manifest、MCP config、Skill 与资产；
- `lhgp` 命令及 `longtask` 兼容别名；
- fresh-machine 安装测试；
- 三个非玩具 dogfood 目标和故障注入报告。

验收：官方插件 validator、七道质量门、schema conformance、多平台 CI、全新用户 quickstart 全部通过；发布标签只能是 Alpha/Developer Preview。

---

## 21. 测试与一致性策略

### 21.1 标准命令

迁移期继续使用当前工具链：

```bash
uv sync --extra dev
uv run python scripts/quality_gate.py
uv run python -m pytest tests/conformance -q
```

插件发布额外运行官方 validator；schema migration 必须有旧数据库副本上的 dry-run 与 rollback 测试。

### 21.2 必须存在的 conformance 场景

1. 未批准合同不能 dispatch。
2. 未授权 executor/model 即使全局 enabled 也不能 dispatch。
3. adapter 无法证明硬约束时拒接，不能降级。
4. daemon 在外部 attempt 运行中重启，能 reattach 或进入 orphan grace，不能立即重复 spawn。
5. 租约换代后旧写回被 fenced。
6. Goal Capsule 丢失后可重建，hash 与来源一致。
7. stale estimate 使风险变 unknown，不继续显示 green。
8. Deadline miss 在离线恢复后如实记录。
9. verifier fail 后可 repair 和重新验证。
10. attempt succeeded 但 checks 未通过时 Goal 不得 satisfied。
11. 预算只够执行、不够验证时 admission 拒绝或明确高风险批准。
12. 重复请求 id 不产生第二个副作用。
13. kill switch 阻止新 dispatch 且不删除合同。
14. 新插件包在干净目录通过 manifest、Skill 与 MCP discovery 验证。

覆盖率是辅助指标，不替代上述场景。每个公开能力声明必须绑定一个可重复证据；`pinned_sha: unpinned` 的证据不能作为正式发布证明。

---

## 22. 工程边界

### Always

- 先更新规范/schema，再改行为；
- 任何状态变更都写事件并具备幂等性；
- 新公开能力必须有 conformance test 和 claims 证据；
- 修改前检查并发工作区，不覆盖用户或其他 Agent 的改动；
- 保持 strict typing、结构化错误和 fail-closed。

### Ask first

- 扩大默认权限、网络或模型 allowlist；
- 修改用户数据目录或不可逆迁移；
- 新增运行时依赖；
- 改变 Deadline miss、预算或验收默认策略；
- 删除兼容别名或旧 schema 读取能力。

### Never

- 用提示词代替沙箱或权限执行；
- 用退出码或模型自报直接完成 Goal；
- 为赶 Deadline 自动扩权、扩预算或安装包；
- 抹去 miss、失败 verifier 或旧 revision；
- 在无法观察外部 attempt 时立即重复 spawn；
- 把 README 写得比实现证据更强。

---

## 23. README 的权威叙事顺序

README 首屏应说明当前产品类别、主要场景和边界，不承诺无人监管下必然交付。按 ADR-004 推荐：

```markdown
# LHGP — Deadline Contract Hub

A multi-agent task contract runtime, independent of sessions and models,
for sustained progress and evidence-based acceptance of long-horizon goals.

The scheduling core does not require an LLM. Models may draft plans,
perform work or supply semantic verification. LHGP holds multiple contracts;
long-horizon goals are the main use case.
```

随后按以下顺序展开：

1. 运行时职责、无需 LLM 的调度内核与模型可选参与的位置；
2. Goal / Contract / Attempt 的持久化关系及多合同边界；
3. 当前能力、证据入口和未实现内容；
4. 可复验的控制面 quickstart，以及真实执行所需的额外配置；
5. 安全、Deadline 与不保证事项；
6. 架构、开发入口、成熟度和已知发布阻断项。

不得用未经核实的“其他系统做不到”支持产品定位，也不得把仅建立合同的示例称为执行验收端到端成功。

README MUST 清楚写出：机器离线时不会工作；Deadline 不是结果担保；插件与协议不是同一层；Developer Preview 尚不代表生产可用。

---

## 24. 假设、成功标准与非目标

### 24.1 关键假设

- 用户愿意批准一份比普通 prompt 更结构化的合同。
- Agent 能以合理频率提交结构化 checkpoint，而不是只在结尾写总结。
- 至少部分主流 harness 能提供稳定外部 run id 或可安全判定 nonrecoverable。
- 验收标准可以被拆成足够可靠的 checks。
- Deadline 风险提示会改变用户决策，而不是被当作普通通知忽略。

这些假设必须用真实目标验证，不能只靠单元测试证明。

### 24.2 Alpha 成功标准

- 三个真实目标分别跨越：关闭原会话、切换 Agent CLI、重启 daemon；
- 全部已提交 checkpoint 可恢复，重复执行与旧写回为零；
- 至少一个目标经历 verifier fail → repair → reverify pass；
- 风险历史能解释每次提醒、换人和用户升级；
- 每合同 model/CLI 授权在日志和行为上完全一致；
- fresh-machine 用户能在 10 分钟内安装、立约、批准、查看状态与取消；
- README 每项能力均有当前 commit 的证据。

### 24.3 明确非目标

- 对任意任务提供绝对准时保证；
- 让 LLM 永久在线或无限循环；
- 成为通用聊天记忆数据库；
- 取代 Temporal/LangGraph 等开发者工作流引擎；
- 在 Alpha 自动完成跨机器、多租户、任意并行拆分；
- 让 Agent 自主增加权限、预算或可用模型。

---

## 25. 参考资料

- [OpenAI：Long-running work](https://learn.chatgpt.com/docs/long-running-work)——Goal 模式与同一 chat 上下文边界。
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)——durable execution、persistence 与 human-in-the-loop。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)——以 thread/checkpoint 组织持久状态。
- [OpenAI Agents SDK：Running agents](https://openai.github.io/openai-agents-python/running_agents/)——Dapr、Temporal、Restate、DBOS durable orchestration 集成。
- [Abject](https://www.abject.world/)——持久 Goal、动态多 Agent 轮次与跨机器先例。
- [MCP 2026-07-28 specification announcement](https://blog.modelcontextprotocol.io/posts/2026-07-28/)——显式 handle 与无隐藏传输会话的状态传递方向。
- [Codex plugin packaging](https://developers.openai.com/plugins/build/plugins)——插件 manifest、MCP 与 Skill 封装要求。

---

## 附录 A：一句话判定

如果一个系统只是“让同一个 Agent 继续跑”，它不是 LHGP。  
如果一个系统只是“到点启动一段自动化”，它不是 LHGP。  
如果目标在原会话消失后仍由独立合同持有，能在用户授权的 Agent/模型之间接力，按 Deadline 风险推进，并由证据化验收决定完成，它才符合 LHGP 的核心。
