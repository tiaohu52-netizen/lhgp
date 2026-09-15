# LHGP 实施目标与计划

> 版本：1.0
> 日期：2026-09-01
> 上游：[LHGP-SPEC.md](./LHGP-SPEC.md)（语义权威）、[ADR-003](./decisions/0003-long-horizon-goal-protocol.md)（命名权威）
> 状态：**长期里程碑与历史基线**；2026-09-05 起，本轮首发及完善执行顺序委托给
> [RELEASE-PLAN.md](RELEASE-PLAN.md)，产品定位依据 [ADR-004](decisions/0004-contract-runtime-and-release-scope.md)。

本文档只回答两个问题：**做到什么算完成**，以及**按什么顺序做**。
语义冲突以 LHGP-SPEC.md 为准；长期里程碑以本文档为准，本轮执行顺序与范围以 RELEASE-PLAN.md 为准。
当前能力只能由代码、测试和 `quality/claims.json` 证明，本文档写了目标不代表已经实现。

---

## 一、北极星目标

> **一个目标在原会话、原 Agent、原模型全部消失之后，仍然能在用户批准的边界内被接力、被推进、被验收。**

这句话可证伪。它的对立面是三种失败：目标随会话一起死掉；接力时丢掉已证实的进度；模型说"完成了"就算完成。

### 1.1 Alpha 完成判据（全部满足才算达成）

1. 三个真实目标分别跨越三种断裂：关闭原会话、切换 Agent CLI、重启守护进程。
2. 全部已提交 checkpoint 可恢复；重复执行与旧写回污染均为零。
3. 至少一个目标完整经历 `verifier fail → repair → reverify pass`。
4. 风险历史能解释每一次提醒、换人和用户升级。
5. 每份合同的 model/CLI 授权在日志和行为上完全一致。
6. 全新用户能在 10 分钟内完成安装、立约、批准、查看状态与取消。
7. README 的每一项能力声明都能指到当前 commit 的证据。

判据 1–3 是硬门槛：做不到就不发 Alpha，只能继续标 Developer Preview。

### 1.2 分层目标

| 层 | 目标 | 对应 SPEC |
|---|---|---|
| L0 诚实 | 对外叙事不比实现证据更强 | §21、§23 |
| L1 身份 | 合同成为 default-deny 的授权主体，会话只持有 attempt | §5、§6、§7 |
| L2 连续 | 守护进程重启后仍能观察、续接或安全重派 | §11 |
| L3 风险 | Deadline 是决策变量，驱动何时做、谁来做 | §10、§9 |
| L4 裁决 | 完成只由证据化验收推导，失败可修复可重验 | §12 |
| L5 分发 | 作为可安装、可发现的协议产品交付 | §3、§14、§15 |

---

## 二、起点：当前实现的位置

2026-09-01 的只读盘点结论（历史基线，详见 `.workbuddy/memory/2026-09-01.md`）：

- 六个里程碑整体完成度约 **15%**，M0 尚未完成，M5 为 0。
- 存储 3 张表（contracts / leases / events），SPEC §13.1 要求 11 个最小实体。
- 状态 2 轴，SPEC §7 要求 4 轴；`deadline_status` 与 `acceptance_status` 整轴缺失。
- 合同 8 组字段中 `authority`、`attention`、`continuity` 三组零命中。
- `external_run_id` / `session_locator` / `recovery_strategy` / `reconcile` / `checkpoint` 全部不存在。

### 2.1 当前实现快照（2026-09-03）

历史盘点不再代表当前代码。现已落地：四轴合同状态与不可变 revision、typed
checks 的确定性执行、attempt 外部句柄与 reconcile、模型/CLI 授权与证明、
daemon + 本机认证 Unix-socket RPC、Goal Capsule/handover、L0/L1 唤醒、
通知 outbox（幂等/重试/安静时间/风险红线）。P6 的 wheel 安装、双轨 CLI、MCP
发现和基础 quickstart 已在 [`docs/evidence/P6-fresh-machine-smoke-2026-09-03.md`](evidence/P6-fresh-machine-smoke-2026-09-03.md)
留证。完整证据以 README、测试和
`quality/claims.json` 为准；仍未宣称跨主机 relay、严格墙钟交付保证或外部通知渠道。

### 2.2 发布审计快照（2026-09-05）

此前复验记录为 634 个测试、82.13% 覆盖率；本轮在 `b4b301d` 上再次运行七道质量门，
结果为 634 个测试、82.17% 覆盖率、7/7 通过。附加故障注入仍复现了取消失败后释放租约、
验收挤占执行预算两个问题。**全绿基线不能据此标为已具备发布条件。**

本轮范围、复现及检查缺口见[发布检查](evidence/release-readiness-2026-09-05.md)。
历史 [P6 安装证据](evidence/P6-fresh-machine-smoke-2026-09-05.md) 和 claims 的
`pinned_sha` 各自只证明其锚定版本，最终候选必须重新构建、安装及验收。

Developer Preview 与 §1.1 的 Alpha 判据分别管理。跨主机／跨网络、L2/L3 中继明确
不进入本轮发布及完善计划；严格墙钟结果担保不是待补功能。兼容命名空间迁移不阻塞首发。

### 2.3 四个必须先纠正的冲突项（历史基线）

它们不是"还没做"，而是当前行为与规范相反，会持续产出错误的权威数据：

| # | 位置 | 冲突 |
|---|---|---|
| C1 | `cli/runner.py:370-375` | 见到历史 verifier 就 `return False`，主动违反 §12.3「历史 verifier 不得阻止新的 verifier」 |
| C2 | `cli/daemon.py:329` | escalate 预算传初始值且从不扣减，并发事实上无限，违反不变式 #5 |
| C3 | `cli/daemon.py:330` | `estimate_stalled=False` 硬编码，`escalation.py:105` PARALLEL 分支成死代码 |
| C4 | `rpc/handlers.py:302` | approve 的 actor 直接采信入参，模型可自称 `user`，不变式 #2 不可强制 |

附带：`daemon.py:382-390` 每 60 秒无条件 append 提醒事件，无冷却、无跨档判定，违反 §10.5。

### 2.4 明确保留、不得推倒的部分

租约 generation + holder 双重 fencing（含崩溃恢复集成测试）、fail-closed 拒接、SQLite/WAL 单事务与 request_id 幂等、七道质量门、分层唤醒 L0/L1、结构化 argv 与环境白名单。这些是 SPEC §19.1 点名的保留项，是本项目的地基。

---

## 三、执行原则

1. **规范先行。** 每个阶段先改 SPEC/schema，再改行为（SPEC §22 Always）。
2. **改名最后。** 完成 P0–P5 前不做任何全仓机械 rename（SPEC §19.3）。
3. **每阶段留证据。** 收工必须同时具备：conformance 场景、claims 条目、`pinned_sha` 锚定真实提交。
4. **不靠放宽失败定义来通过。** fail-closed 路径只允许收紧，不允许为了测试变绿而松绑。
5. **先问再做。** 扩大默认权限、改数据目录、新增运行时依赖、改 miss/预算/验收默认策略，一律先确认。
6. **不覆盖并发工作。** 动 `cli/runner.py`、`rpc/executor_api.py` 前先确认没有其他 Agent 正在编辑。

---

## 四、阶段计划

规模标记：S ≈ 单日可完成；M ≈ 数日；L ≈ 一周以上。

### P0 · 诚实性对齐（S，无依赖）

**目标**：让对外叙事与实现证据的强度一致。

**范围**
- README 按 SPEC §23 重写：首屏改为"目标不该随聊天一起死"，删掉 `It finishes` 类结果担保，写明机器离线时不工作、Deadline 不是担保、插件与协议不同层。
- `quality/claims.json` 拆为 `design_claims`（规范级声明）与 `implementation_claims`（绑定测试与 pinned_sha），并锚定真实 commit。
- 补命名迁移说明：LHGP / `lhgp` / `lhgpd` / `~/.lhgp` 的目标态与 `longtask` 别名窗口。

**验收**：README 无超出证据的承诺；每条能力声明可指到测试或归档；`uv run python scripts/quality_gate.py` 全绿。

**不做**：任何代码行为改动。

---

### P1 · 权威数据地基（M，无依赖，可与 P0 并行）

**目标**：让权威存储能表达四轴状态、不可变修订和可审计角色。

**范围**
- 状态机拆为四轴（§7）：commitment lifecycle、deadline_status、acceptance_status、attempt state；`complete` → `satisfied`，`expired` 语义并入 deadline 轴。
- 新增 `contract_revisions` 不可变表，替换 `store.py:737-779` 的 CAS 就地 UPDATE。
- 事件补齐 `contract_revision`、`role`、`payload_schema_version`；`contract_id` 语义迁移到 `goal_id`。
- 补 `attempts`、`decisions`、`idempotency` 三张实体表。
- 纠正 C1–C4，并为提醒事件加冷却与跨档判定。

**验收**：现有 15 条 claims 全绿不回退；新增四轴迁移测试与修订不可变测试；C1–C4 各有回归测试。

**不做**：授权矩阵、连续性、forecast——留给后续阶段。

---

### P2 · 合同授权与 schema v2（M，依赖 P1）

**目标**：让"这个目标允许谁来做"成为合同的一等字段，default-deny 真正成立。

**铁律（执行原则强化）**
1. **拆巨石**：当前 `contracts/schema.py`（~270 行）+ `cli/main.py`（~500 行）+ `rpc/handlers.py`（~860 行）+ `persistence/store.py`（~1700 行）已现单文件膨胀迹象。P2 起按 SPEC §22「Always」原则**先改 SPEC 后改实现**；按"一个 spec 对应一个 schema 切片 + 一个 validator 模块"切。新文件清单见下。
2. **防虚假引用**：禁止在 SPEC / docs / 代码注释里出现"§X.Y"却未在 LHGP-SPEC.md 实际找到对应章节。每次新增字段都从 SPEC 摘出原文段作为 contract test 的 evidence；claims.json 的 design_claim 不再新增"§X"而无 evidence.path 的项。
3. **去累赘**：P2 不引入与 §6.1/§6.3/§10.4 无关的额外字段；旧的 Acceptance.verifier / hard_constraints 不重复表达 authority；budget.max_escalations 与 authority.allow_parallel 解耦即停。

**范围**
- 合同补 `authority`、`attention`、`continuity` 三组字段（SPEC §6.1）—— 拆 `contracts/schema.py`：
  - `contracts/contract_draft.py`（ContractDraft 顶层容器）
  - `contracts/contract_view.py`（ContractView + ContractState/DeadlineStatus/AcceptanceStatus/BlockReason）
  - `contracts/acceptance.py`（Acceptance + 7 类 typed check 框架 P5 才完整填充）
  - `contracts/authority.py`（Authority 三维 allowlist + §6.3 7 条件判定）
  - `contracts/attention.py`（Attention 通知偏好 + 安静时间 + bypass 列表）
  - `contracts/continuity.py`（Continuity checkpoint / recovery / capsule 容量；P3 才完整填充字段）
  - `contracts/budget.py`（Budget 5 项上限，独立模块便于 P5 扩展 verification_budget）
- 单一 runtime validator —— 新文件 `contracts/validation.py`：消除 JSON Schema（schemas/contract.schema.json）、dataclass 默认值、CLI 默认值三处漂移。JSON Schema 仅作设计参考，runtime 校验由 `validation.validate_draft()` 单一入口负责。
- executor × model × role 三维 allowlist —— `contracts/authority.py` 实现 §6.3 的 7 个条件。
- `goal/prepare` 返回 admission offer（§10.4 七类信息）—— 新文件 `admission/offer.py` + `admission/refuse.py`。Offer dataclass 7 字段与 SPEC 原文一一对应，不增不减。
- 旧合同 dry-run 迁移 —— 新 CLI 子命令 `goal/migrate`（dry-run 必跑）+ `scripts/migrate_v1_to_v2.py`。

**验收**：
- conformance 场景 #1（未授权候选被拒，给原因）、#2（合同 A 限 Codex、合同 B 限 Claude，互不干扰）、#11（admission offer 7 类信息齐全）；
- 单一 validator：同一 draft 输入 JSON Schema / dataclass / CLI 三条路径输出一致；
- migration dry-run 在 examples/ 真实归档上跑通 0 报错。

**不做**：并发分区、跨机器、改名（仍 P6）、forecast（P4）、typed checks 实体（P5）。

---

### P3 · 连续性闭环（L，依赖 P1）

**目标**：守护进程重启后仍能观察、续接或安全重派，接力不丢已证实进度。

**范围**
- spawn 持久化返回 `external_run_id`、`session_locator`、`recovery_strategy`、`capability_snapshot`（§11.3）。
- daemon 启动 reconciliation 四分支：reattach / collect / orphan grace / fence 后重派。
- 结构化 checkpoint：`completed_claims`、`remaining_work`（p50/p90）、`next_action`、`requested_decisions`（§11.2）。
- Goal Capsule v1：9 元组结构 + provenance + fact/decision/hypothesis/untrusted 标记（§11.1）。
- adapter API 补齐 `control`、`checkpoint_request`、`recover`。

**验收**：conformance 场景 #4、#6；E2E——杀死原会话、重启 daemon、由不同授权 CLI 继续，无重复 attempt、无丢失进度、无旧写回污染。

**风险**：本阶段会触碰 `cli/runner.py` 与 `rpc/executor_api.py`，必须先确认并发编辑状态。

---

### P4 · Deadline Decision Reliability v1（单机，依赖 P3）

本阶段重新收敛目标：在不引入跨主机/跨网络基础设施的前提下，让 Deadline 成为可解释的决策控制面，而不是 cron 式空转。验收以 snapshot、风险事件、临界唤醒和恢复证据为准；不承诺绝对墙钟完成。

- 每次有效 tick 产生可审计 Deadline snapshot（六项 forecast、p50/p90、slack、置信度、风险档、next decision）。
- 低样本/过期估计自动降级 `low/coarse`，风险跨档和 miss 事件去重。
- `due_at` 边界、重启恢复、决策点不晚于安全边界均有测试证据。
- 跨主机 relay、网络控制面、L2/L3 唤醒、外部通知送达保证和严格墙钟 SLA 保持非目标。

**目标**：让唤醒来自"下一个有意义的决策时刻"，而不是每 60 秒问一次。

**范围**
- 六分量 forecast：queue、startup、remaining work、verification、retry reserve、safety margin；p50/p90 与 P(finish)（§10.2）。
- 风险六档与阈值（§10.3），产出 `next_decision_at`。
- 事件驱动唤醒替代固定轮询；无动作窗口模型调用数为零。
- remind / steer 产生真实 adapter 动作与回执，不再只记事件；升级动作通过 authority、constraints、budget 与 cooldown 校验。
- miss 仲裁与预测校准记录；通知 outbox + 幂等 + 安静时间例外。

**验收**：conformance 场景 #7、#8；模拟时钟可证明阈值动作；无动作窗口 LLM 调用数 = 0。

---

### P5 · 验收修复闭环（M，依赖 P2 + P3）

**目标**：完成只由证据推导，失败可修复、可重验。

**范围**
- 7 种 typed checks（§12.1）与 `evidence` 实体；mandatory / optional 分类。
- verifier 独立性强化：不同 attempt、不同 session、优先不同 model family，不继承 executor 对话上下文。
- repair brief 结构化产出；多轮 reverify 受验证预算限制（解除 P1 之后的剩余限制）。
- `satisfied` 只由当前 revision 的 mandatory checks 全通过推导。
- 验证预算独立记账；预算不足时 blocked 而非跳过验收。

**验收**：conformance 场景 #9、#10；E2E `executor success → verifier fail → repair → verifier pass → satisfied`。

---

### P6 · 分发与公开 Alpha（M，依赖 P4 + P5）

**目标**：让别人能在自己机器上装起来用。

**范围**
- 建立 `.codex-plugin/plugin.json`、`.mcp.json`、`skills/long-horizon-goals/SKILL.md`、`assets/`。
- MCP 工具已提供 `lhgp_*` 双轨命名、审计/控制扩展，并补齐
  read-only / destructive / open-world annotations；剩余工作是 fresh-machine 验证。
- `lhgp` / `lhgpd` 命令与 `~/.lhgp` 数据目录迁移工具（dry-run + 备份）；`longtask` 别名保留一个次版本。
- Python 内部模块路径最后迁移（`src/longtask` → `src/lhgp`），并同步 `scripts/arch_check.py` 的架构约束。
- fresh-machine 安装测试与三个非玩具 dogfood 目标。

当前新增可复现证据（内部运行记录）：stage-2 的 default-deny 授权预演（仅选出被授权 executor 与
独立 verifier，未启动外部进程）。这只证明授权预检，不替代真实
CLI 接力与验收闭环证据。

**验收**：conformance 场景 #14；官方插件 validator 通过；全新用户 quickstart ≤ 10 分钟；发布标签只能是 Alpha / Developer Preview。

---

## 五、依赖与关键路径

```text
P0 ──────────────────────────────────────────┐
                                             ├→ P6
P1 → P2 ────────────┬──→ P4 ─────────────────┤
    └→ P3 ──────────┴──→ P5 ─────────────────┘
```

关键路径是 **P1 → P3 → P4**。P3 工程量最大、外部依赖最多，应当最早开始技术预研。
P0 不阻塞任何阶段，可以立即做。P2 与 P3 在 P1 之后可并行。

---

## 六、进度度量

| 指标 | 起点 | Alpha 目标 |
|---|---:|---:|
| conformance 场景覆盖（§21.2 共 14 个） | 4 | 14 |
| claims 中 pinned 的实现声明 | 0 | 全部 |
| 四轴状态落地 | 2 / 4 | 4 / 4 |
| 存储最小实体（§13.1 共 11 个） | 3 | 11 |
| 无动作窗口 LLM 调用数 | 未度量 → **已度量（2026-09-11）**：安静窗口（活租约/低紧迫/blocked 三类）5 轮 tick 零派工，见 tests/integration/test_quiet_window_zero_dispatch.py | 0 |

覆盖率是辅助指标，不替代 conformance 场景。每个公开能力声明必须绑定一个可重复证据；`pinned_sha: unpinned` 的证据不能作为发布证明。

---

## 七、本轮明确不做

- 跨机器、多租户部署。
- 任意目标的自动并行拆分（MVP 只做串行接力，§11.5）。
- L2 云侧定时器与 L3 常在线中继（已在 claims 中登记为 `accepted_debt`）。
- 通用聊天记忆数据库。
- 取代 Temporal / LangGraph 等开发者工作流引擎。

---

## 七·补充：命名迁移窗口（longtask → lhgp）

P0 期间明确：协议语义正式名为 **LHGP**（`Long-Horizon Goal Protocol`，见 [ADR-0003](./decisions/0003-long-horizon-goal-protocol.md)），但实现与分发形态要按 SPEC §19.3「改名最后」原则分阶段迁移，避免破坏现有安装。

| 维度 | 现状（截至 280d788，P0） | P6 目标态 | 过渡期策略 |
|---|---|---|---|
| 协议名 | LHGP | LHGP | 不变 |
| Python 命令 / CLI | `longtask` / `longtaskd` | `lhgp` / `lhgpd` | P6 同期发布 `lhgp` / `lhgpd`，旧名作为 shim 至少保留一个次版本 |
| 数据目录 | `~/.longtask/` | `~/.lhgp/` | P6 发布迁移工具（dry-run + 备份 + 可回滚）；旧路径在迁移完成前继续生效 |
| Python 模块路径 | `src/longtask/` | `src/lhgp/` | P6 末段迁移；先双写后切读 |
| MCP server 入口 | `longtask-mcp`（核心工具与兼容入口） | `lhgp-mcp`（核心工具、LHGP 别名、审计/控制扩展，含安全语义标注） | 双轨已发布；旧入口继续可发现，至少一个次版本 |
| skill 名 | `skills/longtask-contract/` | `skills/long-horizon-goals/` | P6 同步迁移；旧名继续作为 alias |
| README / 文档 | `README.md` + `README.zh-CN.md`（P0 已双语）；文档主体口径用「LHGP / 远期目标协议」 | 同上 | 不变 |

不在 P0–P5 范围：

- 全仓 `longtask` → `lhgp` 字符串替换（违反 §19.3，会污染 git blame、破坏 `examples/` 归档路径）。
- MCP 工具名 1:1 迁移（属于 P6 的「插件包」工作）。
- 文档/代码注释统一口径（P0 已更新 README、CLAIMS、ROADMAP；代码注释与 docstring 留到 P6 末段同步）。

迁移硬约束（生效于 P6 实施时）：

1. **dry-run 必跑**：默认只打印计划，不动数据。
2. **备份必做**：迁移前对 `~/.longtask/` 做完整归档（`tar`/`cp -a`），向用户报告路径。
3. **可回滚**：迁移完成后保留旧路径 N 个版本窗口；旧名 CLI 持续可调用，自动识别新旧路径。
4. **claim 同步**：迁移 PR 必须更新 `implementation_claims` 中相关声明的 evidence 路径与 pinned_sha，并新增/更新 `lhgp-*-distribution` 系列 design_claim 描述目标态。

P0 之后 `quality/claims.json` 已有 8 条 `design_claims` 锚定 SPEC 章节（`spec-is-authoritative-source`、`lhgp-spec-four-axis-model`、`lhgp-spec-authorization-tri-axis`、`lhgp-spec-external-handles`、`lhgp-spec-goal-capsule-v1`、`lhgp-spec-forecast-six-component`、`lhgp-spec-typed-acceptance`、`lhgp-spec-distribution-and-rename`），每条 claim 显式标注所处阶段（当前实现状态 + 属 P1–P6 哪一阶段）。实现声明的当前精确锚点以 `quality/claims.json` 的 `pinned_sha` 为准，并由 claims 门禁校验。

---

## 八、变更规则

修改本文档的门槛低于修改 SPEC：调整阶段顺序、拆分或合并阶段，直接更新本文档并在提交信息里说明理由。
若某个阶段的验收标准与 SPEC 冲突，先改 SPEC，再回来同步本文档——不得靠降低验收标准来让阶段通过。
