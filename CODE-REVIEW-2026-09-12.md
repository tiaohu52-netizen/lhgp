# LHGP（远期任务协议 / 限期合同中枢）代码审查报告

> 审查对象：`D:\工作台\远期任务协议`  
> 审查日期：2026-09-12  
> 代码基线：`251e225`（fix: schemas/*.json 与运行时的漂移，审计 B10）  
> 审查方式：文档对读 + 三个方向（调度/持久化/RPC）代码走查 + 关键结论实测复现  
> 置信标注：**[实测]** = 我本人复现或逐行读过；**[走查]** = 探索代理发现、未逐行复核
> 第二轮复核（同日）：对 12 条高影响条目逐条读源码验证，见文末「附录 B 第二轮复核记录」



---

## 一、设计目标与核心机制

### 1.1 定位

LHGP 是一个**外置 harness**：不隶属任何 Agent 应用，反过来把 Codex / Claude Code / agent-cli / Hermes 等抽象成可替换的执行资源池，自己只做一件事——**把一份持久合同推进到底**。

> 会话持有一次尝试，LHGP 持有长期承诺。  
> A session owns an attempt. LHGP owns the commitment.

与 cron / CI / LangGraph 的根本差别在「完成定义」：自动化是退出码 0，LHGP 是**合同验收条款通过 + 证据**。

### 1.2 五条公理（DESIGN §2）

1. **合同外置**——状态活在文件系统/SQLite，不依附会话。
2. **会话即燃料**——任何会话都是临时雇来的执行者，用完即弃。
3. **不假设执行器能力**——只要求「命令行可拉起 + 读得懂入场提示词」。
4. **约束要么兑现要么拒接**——翻译不了硬约束就 `CONSTRAINT_UNTRANSLATABLE`，禁止降级执行。
5. **不靠模型但必暴露给模型**——调度内核零 LLM；模型侧只暴露工具面。

### 1.3 四条独立状态轴（SPEC §7）

```
Commitment: drafted → active ↔ paused / blocked → satisfied | cancelled | expired → archived
Deadline:   not_due → at_risk → met | missed | waived
Acceptance: pending → candidate → verifying → passed | failed | undetermined
Attempt:    admitted → starting → running ↔ waiting → succeeded|failed|cancelled|stale|orphaned
```

把四轴塞进单一状态机会制造歧义——这是本协议相对同类系统最实质的设计选择。

### 1.4 关键机制

| 机制              | 实现位置                                                                         | 作用                                                   |
| --------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------- |
| 冻结区 / 可修订区      | `lhgp/contracts/contract_draft.py`、`FROZEN_FIELDS`                           | Deadline/禁令/沙箱立约钉死，紧迫度永远不能升级它们                       |
| 租约 + fencing    | `longtask/persistence/leases.py`（CAS `expected_generation`）                  | 防旧会话苏醒覆盖新 attempt；过期写回 `LEASE_FENCED`                |
| 独立 verifier     | `longtask/cli/tick.py::_judge_verifier_outcomes`、`runner._dispatch_verifier` | 执行者自报完成不算数，交叉核对                                      |
| 升级阶梯            | `lhgp/promoter/urgency.py` + `escalation.py`                                 | 紧迫度 `u = 剩余工作量 ÷ 剩余时间` 驱动 0–5 档                      |
| 上下文投影           | `longtask/persistence/context.py::compile_context_snapshot`                  | `active.md`（只读锚点）/`scratch.md`（可编辑）分离，容量 fail-closed |
| 事件溯源            | `events` 表 + `log.jsonl` 投影                                                  | 投影「可落后可重建，不可超前」                                      |
| 不可信文本边界         | `lhgp/untrusted/{sanitize,boundary}.py`                                      | 输出结构性清洗（幂等）+ 摘要派生围栏 token                            |
| 进程树终止           | `lhgp/adapters/processes.py::terminate_tree`                                 | POSIX `killpg` / Windows `taskkill /T`               |
| MCP 工具面 profile | `longtask/mcp_profiles.py`                                                   | 按角色收窄 + 越 profile fail-closed 拒调                     |

### 1.5 双命名空间（迁移中间态）

```
src/lhgp/      规范命名空间（协议概念：contracts/acceptance/admission/forecast/scheduler/templates/...）
src/longtask/  遗留命名空间（运行时机械：cli/store/adapters/daemon/mcp_server）
```

规则：新代码写 `lhgp/`；`longtask/` 暂不搬；同名文件中一侧是真身、另一侧是 ≤5 行门面。**但这条规则有例外且已被实测证实**：`rpc/handlers/contract.py`（1382 行）与 `goal.py`（591 行）的**真身在 longtask 侧**——与 ARCHITECTURE.md「概念在 lhgp」的简记相反。

---

## 二、已完成的功能模块及工作方式

### 2.1 持久层 `persistence/`

- **权威存储**：SQLite/WAL，`STORE_SCHEMA_VERSION = 6`（`schema.py:29`），迁移链 v1→v6 由 `ensure_schema` 幂等补齐。
- **核心表**：`contracts`、`contract_revisions`（不可变修订快照）、`events`（追加式）、`attempts`、`leases`、`decisions`、`idempotency`、`evidence`（v6 独立表）、`memories`（v4）、`user_evaluations`/`acceptance_diffs`（v3）。
- **投影**：`contract.yaml` / `lease.json` / `log.jsonl` / `handover.md` / `task_plan.md` / `progress.md` / `context/attempts/<id>/{active,scratch}.md`。
- **上下文编译**：`compile_context_snapshot` 按 `ContextPolicy` 裁剪、限额、标注来源；超容量抛 `CapacityRefusedError`（fail-closed）。

### 2.2 调度与推动层 `cli/` + `promoter/`

- **tick 主循环**（`tick.py`）：kill switch → `_judge_verifier_outcomes` → 扫非终态合同 → 过期转 EXPIRED → 仅 ACTIVE 合同算紧迫度/预算 → `decide()` → 落 `next_decision_at`/forecast → 按档分派。
- **档位**（`urgency.py::classify`）：实际只产出档 0–3（QUEUED/REMIND/STEER/RESPAWN）。
- **decision**（`escalation.py::decide`）：租约活 → 封顶 REMIND；无租约 → RESPAWN；成本耗尽 → HAND_TO_USER。
- **attempt 生命周期**（`runner.py`）：prepare 复验 → spawn → 心跳续租 → 终态 collect → stale 标记。
- **重启恢复**（`promoter/reconcile.py`）：四分支——reattach / collect / orphan grace / fence 后重派。
- **deadline 强制**（`enforcement/`）：NORMAL/WARNING(30%)/URGENT(5%)/BREACHED 四级。

### 2.3 验收层 `acceptance/`

- **7 种 typed check**：`file-exists` / `file-content-matches` / `command-exit-zero` / `artifact-present` / `structure-valid` / `observable` / `user-assertion`。
- **两条证据通道**：RPC `attempt/write-back`（会话型）与 stdout `lhgp-verdict` 判定块（一次性 CLI）。
- **裁决合成**（SPEC §12.4）：确定性结果优先，`undetermined` 时模型观察填补；双方皆无 → 人工仲裁。
- **内容指纹绑定**：`Acceptance.content_fingerprint` 由 runtime 计算，核验证据绑到 attempt 准入修订（a13 修复的误完成路径）。

### 2.4 暴露面

- **CLI** `lhgp` / `lhgpd`，legacy 别名 `longtask` / `longtaskd`。
- **MCP** `lhgp-mcp`：52 个工具（35 正名 + 17 兼容别名），六档 profile（executor 11 / verifier 7 / planner 12 / operator 36 / full 35 / legacy 52）。
- **JSON-RPC**：`protocol/hello`、`contract/*`、`attempt/*`、`executor/*`、`lease/*`、`goal/*` 等。
- **插件**：`.codex-plugin/` + `.mcp.json` + skills。

### 2.5 附加子系统（a7–a9 引入）

`feedback/`（用户评价 + 验收 diff）、`learning/`（模板自动演化）、`portfolio/`（多合同看板）、`memory/`（协议级长期记忆 + auto-mine）、`wiki/`（双向 Obsidian vault）、`flow/`（代码/合同流程图 AST 走查）。

### 2.6 当前健康度（实测）

- 全量 `pytest tests/`：**1 failed**（环境性，见 3.14）、其余通过；`last-gate.log` 记录七道门 ALL PASS。
- 注意：`quality/last-gate.log` 与 `quality/pytest-run.log` 是**过期产物**（前者记 1114 passed / 75.62%，与 CHANGELOG 宣称的 1644 passed / 79.9% 不一致），日志本身也是漂移源。

---

## 三、缺陷清单（按模块）

> 严重度：**P0** 影响正确性/安全；**P1** 功能静默失效；**P2** 一致性/健壮性。

### 3.1 适配器注册表（影响授权语义）— **P0 [实测]**

**`RegistryEntry.set_enabled` 重建对象时丢失 `models` 字段** — `src/longtask/adapters/registry.py:385-393`

```python
updated = RegistryEntry(
    id=entry.id, kind=entry.kind, launch=entry.launch,
    capabilities=entry.capabilities, limits=entry.limits,
    cost_hint=entry.cost_hint, enabled=enabled,
)   # ← 漏了 models
```

`RegistryEntry.models` 定义在 `registry.py:181`（默认 `("*",)`）。后果：**任何一次 executor enable/disable 都会把该执行器的模型白名单重置为 `["*"]`**，而 `match_candidates` 用 `models` 做 authority 绑定校验（`registry.py:416` 注释明确「`"*"` 通配仅 Principal 显式选择才合法」）。生产路径 `lhgp/rpc/handlers/executor.py:64,82` 直接调用它。

附带：全仓无 `save_to_file` 调用点，注册表变更**不持久化**，重启即丢。

### 3.2 容量与预算记账 — **P1 [实测]**

1. **`TickCapacityLedger.can_dispatch` 从未被生产代码调用** — 定义于 `src/lhgp/promoter/fairness.py:95`，`grep` 全仓仅 `tests/unit/test_fairness_and_proposals.py` 引用。`tick.py:152` 构造台账、`:516` 只调 `record_dispatch`。**`max_dispatches_per_tick` 永不生效**——而 `fairness.py:16-18` 的注释正声称「规则 2 已接线」。
2. **`max_escalations` 从不 gate 决策** — `escalation.py` 全部 6 个返回分支里 `consumes_escalation` **恒为 False**（`_free` 返回 `(False, False)`，RESPAWN 返回 `(True, False)`）；`budget_escalations_left` 只出现在 reason 字符串。tick 侧 `escalations_used = verifier_count + steer_count`（`tick.py:192-196`），而 `steer_count` 统计的 `ESCALATION_STEERED` 事件只在 `tick.py:618` 发出，该分支属 **STEER 档——而 STEER 档不可达**（见 3.3.1）。净效果：`max_escalations` 退化成「verifier 次数上限」，与 `verification_attempts_reserved` 语义重复计量。
3. **`budget_dispatches_left` 手写状态列表漏 `starting`/`waiting`** — `tick.py:325`，与审计 B4 修过的 `attempts.py:170-186` 是同一类漂移，只修了一处。
4. **合同级 `max_concurrent_attempts` 从不生效** — 只有 registry 的 `entry.limits` 被读（`registry.py:444`）。


### 3.3 调度与升级阶梯 — **P1 [实测]**

1. **档 2 STEER 是死代码** — `decide()` 在租约活时 `min(tier, REMIND)`（`escalation.py:36-40`）、无租约时折成 RESPAWN（`:62-67`），故 `tick.py:610-626` 的 `case UrgencyTier.STEER` 永不可达。
2. **档 1/2 的文档动作根本没实现** — tick 对 REMIND 只 `append_event`（`tick.py:597-626`），`ESCALATION_REMINDED`/`ESCALATION_STEERED` **无任何消费者**。DESIGN §6.2 承诺的「向活跃会话注入带倒计时的提醒轮次」「steer() 扳话题」从未发生。
3. **`UrgencyThresholds.hand_to_user = 1.5` 是死配置** — `urgency.py:30`，`classify` 从不使用；档 5 只能由预算触发，「u ≥ 1.5 → 档 5」未实现。
4. **`estimate_stalled` 是空操作** — `escalation.py:74-97` 的 stalled 分支与兜底分支**返回完全相同的 `RESPAWN`**，只有 reason 文本不同。
5. **`_estimate_stalled_from_attempts` 按 `goal_id` 查询** — `promoter/records.py:118`，同一 Goal 下的多份阶段合同会互相串扰停滞判定。
6. **`_workspace_holder_other_than` 是 O(N²) 全事件扫描** — `tick.py:673` 每合同 `list_contracts` + `get_events`。
7. **`list_contracts(conn, limit=1000)` 遍布调度路径** — `tick.py:125/673/728`、`daemon_loop.py:512/618/648/708`、`wakeup.py:398/480`。超过 1000 份合同后**静默不调度**，无任何告警。
8. **`BLOCKED(NO_EXECUTOR)` 无自动唤醒** — 只有 `CAPACITY_FULL` 有 `wake_blocked_capacity_full`；事后注册新执行器不会触发重试。

### 3.4 租约与 verifier 派工 — **P0 [实测]**

1. **`_dispatch_verifier` 顺序违反 prepare→CAS→spawn** — `runner.py:1218-1253`：先 `adapter.prepare()` + `adapter.spawn()`，**之后**才 `acquire_lease()`。且 `_build_verifier_input`（`:1217`）在 acquire 之前读租约 → verifier 拿到的 `lease_generation` **少 1**。若 `acquire_lease` 抛 `LeaseCASError`（未捕获），异常击穿 daemon 主循环，**已 spawn 的子进程无人跟踪**（资源泄漏 + 孤儿写工作区）。
2. **`_dispatch_attempt` 回收租约时不校验存活** — `dispatch.py:533-547` 只要 `active_lease is not None` 就 `reclaim_lease`，从不调 `is_alive()`；docstring 写的是「心跳已断的旧租约」。上游 tick 确实用 `is_alive` 判过（`tick.py:314`），所以主路径安全，但**RPC 线程可并发 `lease/renew`**，存在抢活租约的 TOCTOU 窗口。


### 3.5 RPC / MCP 权限面 — **P0 [实测]**

1. **`handle_executor_enable` / `handle_executor_disable` 无任何 actor 校验** — `src/lhgp/rpc/handlers/executor.py:54-87`。对比 `contract/pause` 等要求 `require_principal`，这里**任何持 token 的 RPC 客户端都能改执行器开关**（含 MCP 模型客户端），等于绕过 §8.2「用户框定池子」这个控制面。
2. **`session_token` 只对 `client_id == "executor"` 强制** — `src/longtask/rpc/executor_api.py:262`。MCP 客户端 `client_id == "mcp"` 被直接豁免；且 `_get_session_token()` 读的是 MCP 服务器进程 env（`mcp_server.py:1151-1155`），拿不到 spawn 时注入的值。后果：**模型可对 `role=verifier` 的 attempt 走 `attempt/write-back` 伪造 evidence**。
3. **`handle_control_interrupt` 无 Principal 门** — `executor_api.py:182-217`；`lhgp_interrupt_attempt` 在 executor/operator profile 内。代码注释把它当有意设计（actor 如实派生），但与 SPEC §16.1「干预需授权」的矩阵不一致。
4. **`handle_protocol_events` 无保护** — `src/lhgp/rpc/handlers/protocol.py:43-58`：`json.loads(e.payload_json)` 无 try、`limit` 无上限。
5. **`transport.process_lines` 不校验 token 非空** — `src/lhgp/rpc/transport.py:43`，`{"token": ""}` 可通过（`daemon_loop.py:125` 的 `if token:` 恰好规避，属防御缺失而非漏洞）。
6. **`handle_contract_prepare` 用 `str()` 静默接受非字符串 contract_id** — `contract.py:79`，违反 `require_contract_id` 的类型 fail-closed 策略。


### 3.6 幂等与状态写入 — **P0 [实测/走查]**

1. **幂等 `request_id` 不绑定合同、不比对语义** — `store.py:1542-1547`（`update_contract_state`）、`:1778-1783`（`patch_contract`）、`:1958-1968`（`write_back`）：只要 `get_events_by_request_id` 非空就早退，**既不校验事件属于本 contract，也不比对 payload**。跨合同复用同一 `request_id` 会被静默当成功，真实状态变更被丢弃。仅 `save_contract`（`:1236`）做了 draft 比对。**[实测]** 根因在查询本身：`lhgp/persistence/events_query.py:234` 的 SQL 是 `WHERE request_id = ?`——**全库按 request_id 检索，无 contract_id 过滤**。`write_back` 更严重：早退时返回的 `event_ids` 取自**他合同**的事件（信息泄漏 + 写入被静默跳过）。
2. **`handle_goal_prepare` 自带重放逻辑、不复用归属校验** — `goal.py:209-236`：先按**本次请求的** `contract_id` 查库，查到就返回其视图，**完全不校验该 request_id 原本属于哪份合同**；兜底分支用 `get_events(conn)` 全表扫描反查。**[实测]**
3. **canonical `parse_contract_draft` 与 legacy 不一致且无测试** — `src/lhgp/rpc/handlers/_common.py:128-195` 全文**零次出现 `auto_approve`**（grep 确认），即静默走默认禁用；而 legacy 版（`longtask/rpc/handlers/_common.py:191-215`）会构造 `AutoApprove` 并按 `client_id == "mcp"` 剥离。`tests/unit/test_handler_common_parity.py` 只覆盖 `idempotent_replay`/`__all__`/errors，**不覆盖这个函数**。**[实测]**
4. **`attempts` 写入无状态机校验** — `attempts.py:259-301` 的 docstring 明写「不在此处校验迁移合法性……本模块只做忠实落库」；实现上 `terminal_at = now.isoformat() if terminal else None`，**非终态写入会把既有 `terminal_at` 清成 NULL**；`error_class`/`return_code` 用 `COALESCE(?, ...)` 永不清空。合同侧有 `assert_legal_contract_transition`，attempt 侧无对称兜底。**[实测]**
5. **`prune_terminal_events` 破坏幂等锚点** — `maintenance.py:100-136` 直接 `DELETE FROM events`，连带删掉 `request_id` 记录；被裁剪的终态合同若重放旧 `request_id` 会**重新执行**。且未用 `transaction()`，靠裸 `conn.commit()`。**[实测]**
6. **`write_back` 静默忽略参数** — `store.py:2009-2022`：仅当 `contract_state is not None` 时才使用 `expected_revision`/`blocked_reason`/`next_wakeup_at`，传了也无声丢弃（CAS 缺失）。**[走查]**

### 3.7 投影与文件写入 — **P1 [实测]**

1. **人类编辑门（dirty/tampered）是死代码** — `check_projection_dirty`（`projections.py:183`）全仓仅被定义、无生产调用；`PROJECTION_DIRTY` / `PROJECTION_REBUILT`（`events.py:55-56`）**从未被 append**。DESIGN §3.1 承诺的「手改 `contract.yaml` → 标 dirty → 暂停分发」**未接线**。
2. **`_atomic_write` 名不副实** — `projections.py:200-204`：无 `fsync`（与「crash 不会留下截断文件」的 docstring 不符）；tmp 名固定为 `.tmp` 不唯一，**并发重建互相覆盖**；Windows 下目标被占用时 `os.replace` 抛 `PermissionError` 未捕获。另 `context.py:799-802` 的 `active.md`/`scratch.md` 用裸 `write_text`，非原子。
3. **`timeline.py` 存在注入面** — `src/lhgp/persistence/timeline.py`：只有 `summary` 转义（`:92`），`actor`/`attempt_id` 原样进 `innerHTML`（`:138-140`）；`data_json` 内联进 `<script>`（`:128`），任何含 `</script>` 的字符串可破出。虽为本地单用户产物，仍属未加固面。
4. **`contract_dir` 不做路径校验** — `projections.py:150-152`，与 `resume.py:58` 的严格白名单不对称。

### 3.8 上下文投影 — **P1/P2 [走查]**

1. **容量下限与上限自相矛盾** — `context.py:616-618`：`memory_budget = min(int(policy.max_bytes * 0.4), 4000)` 之后又 `max(memory_budget, 1500)`，而下限来源 `context.py:117` 是 `max_bytes = max(1, max_bytes)`。**合同声明 `max_bytes < 3750` 时，memory 段单独就超过整个容量合同** → 必然抛 `CapacityRefusedError`，attempt 永远起不来。**[实测]**
2. **截断只覆盖 directive 与 handover-prompt** — `hard_constraints` / memory / handover 正文均未截断，超大输入直接变成**永久 fail-closed 拒绝**，而非降级。
3. **`check_handover_due` 用 `datetime.now(UTC)`** — `context.py:1056`，同文件其余路径全走注入的 `now`，导致无法重放测试。
4. **`build_brief` 通知未按合同过滤** — `insights.py:137-140` 取全局最近 3 条 outbox，会把**别的合同**的通知混进本合同 brief。

### 3.9 韧性四链（plan gate / retry / auto-handover / resume）— **P1 [实测]**

1. **auto-handover 只检测、不动作** — `daemon_loop.py:374-462` 的 `_check_handover_due` 只写 `HANDOVER_DUE` 事件，**从不写 `handover.md`、不终止/换会话**，无消费者；`daemon_loop.py:224-226` 注释声称「迫使下一轮不再派发」不成立。
2. **resume 未接进 daemon** — 仅 CLI（`main.py:1803`）与 MCP（`mcp_server.py:396`）手动入口。
3. **`RepairBrief.retry_strategy` 是死字段** — 计算了但从不被读取。

### 3.10 重启恢复与 deadline — **P1 [走查]**

1. **`reconcile._collect` 不跑 typed-check evaluate** — **[实测，且比初判更严重]** `evaluate_check` 在整个 `cli/` 层**只有一处调用点**：`runner.py:803`（全仓 grep 仅此一行 + `runner.py:24` 的 import）。`reconcile.py:380-447` 的 `_collect` 只调 `adapter.collect()`、填 `returncode`、给 verifier 盖证据指纹，然后落事件——**从不评估 typed checks**，且 `succeeded` 直接取自外部观测状态，未经「确定性结果优先」过滤。后果：daemon 重启后由 reconcile 结算的 verifier，事件 payload 里**没有 `evidence` 键**；`tick._judge_verifier_outcomes`（`tick.py:766`）对带 spec 的合同调 `_evaluate_contract_spec` 时拿不到证据 → 落到 `_record_spec_pending`，**合同永远卡在 ACTIVE**。SPEC §12.4「确定性结果优先」实际只在一条结算路径上成立。
2. **`EnforcementAction.actions` 计算后被丢弃** — `daemon_loop.py:723-746` 只把 `actions` 塞进事件 payload；`lock_new_attempts` 全仓无消费者（`enforcer.py:48` 只是元组里的字符串）。
3. **docstring 声称的门不存在** — `daemon_loop.py:702` 写「breached 等级的 `lock_new_attempts` 由后端调度器在 attempt 派发时按 `contract.deadline_status == MISSED` 拒绝」。实测 `dispatch.py`/`tick.py` 中**无此门**（实际保护来自 tick 把过期合同转 EXPIRED 后 `if c.state != ACTIVE: continue`，效果相近但描述不实）。
4. **`_judge_verifier_outcomes` 失败分支事件类型与状态不符** — `tick.py:842-861` 写 `CONTRACT_BLOCKED` 事件却把状态转 ACTIVE。

### 3.11 MCP 工具面 — **P2 [走查]**

1. **`_validate_arguments` 不查 `enum`/`minimum`/`maximum`/`minItems`** — `mcp_server.py:2379-2415`（只查 required/类型/未知键，函数体实测仅含这三类检查）。`tool_attach_to_executor` 对非法 `report_state` 两个分支都跳过 → **静默 no-op**（`mcp_server.py:1201,1228`）。**[实测]**
2. **`errors.RETRYABLE` 与重试逻辑无关联** — `lhgp/rpc/errors.py:33-54` 的表无人读；`longtask/rpc/dispatch.py:61-87` 只看异常类名/status_code。`RETRYABLE[INTERNAL] = True` 是死配置。**[走查]**
3. **`_lifecycle.py:136-158` 硬编码 `protocol_version = 2`**，并有两处 `except Exception: return` 静默吞异常（`handle_goal_prepare` 失败与 `auto_approve_drafted_contract` 失败都**不留事件、不留日志**，自动阶段合同会静默不创建）。**[实测]**

### 3.12 适配器与进程树 — **P1 [走查]**

1. **`cancel()` 提前 return，永不杀树** — `subprocess_adapter.py:641-646`：`if proc.poll() is not None: return`（注释「已自行退出：无可取消对象」）——直接子进程已退出时立即返回；而紧随其后 `:653-656` 的强杀分支注释写着「**即使直接子进程已经退出也照杀——此时残留的正是要清理的孙进程**」。**两处注释直接互斥，实现选择了前者**，于是「子进程已退出、孙进程仍存活」这一正是 DESIGN §14.3 要防的场景，恰好被提前 return 跳过。**[实测]**（诚实边界：进程已退出后 `taskkill /T /PID` 对已回收 pid 本就可能失败，所以这更像是「保证被弱化且自述不实」，而非纯实现失误。）
2. **`processes.py` 两份实现（canonical 397 行 vs legacy 25 行）** 需确认 legacy 是门面而非第二份逻辑。


### 3.13 持久化细节 — **P2 [走查]**

1. **`diff_revisions` 手写字段清单漏项** — `maintenance.py:22-35` 的 `_DIFF_FIELDS` 只有 12 项；而 `schema.py:172-198` 的 `contract_revisions` 实际有 `context_json`/`execution_json`/`client_meta_json`/`authority_json`/`attention_json`/`continuity_json`/`auto_approve_json` **7 个语义列**（逐列核对确认），全部不在 diff 清单里 → **改授权、改执行策略、改自动批准都不会出现在修订差异中**。**[实测]**
2. **`connect` 缺 `busy_timeout`** — `schema.py:41-45` 只设 WAL/foreign_keys，多写者 `BEGIN IMMEDIATE` 会立即 "database is locked"。
3. **位置解包 + 手写列清单漂移温床** — `store.py:246-280` 按位置解包 27 列，必须与 `:437-451`、`:1168-1177` 两处手写 SELECT 逐字对齐；`schema.py:565-599` 迁移列亦手写枚举。
4. **`schema.py` 列默认值漂移** — `:143` fresh 表 `payload_schema_version` 默认 2，`:585` 迁移默认 1。
5. **`ensure_schema` 每次连接跑全量回填 UPDATE** — `schema.py:602,617`，且 DDL 不在事务内。
6. **`acceptance_at_revision` 吞掉损坏行返回 None** — `store.py:178-179`，与 B5 的类型化 fail-closed 策略不一致。
7. **`calibration.py:113-116`** payload/check 非 dict 时抛未捕获 `AttributeError`。
8. **历史事件被改写** — `store.py:1635-1645` 与 `:1904-1919` 反复改写历史 `PLAN_APPROVED` 事件的 `contract_revision`，**`events` 并非 append-only**。
9. **`store.py:1079-1083` 裸 `except Exception: return` 静默吞错**；`:1485` ARCHIVED→CONTRACT_ARBITRATED 语义错配。

### 3.14 测试套件当前状态 — **P2 [实测]**

**1 个集成测试失败**：`tests/integration/test_contract_visibility.py::TestWorkspaceExclusivity::test_workspace_junction_like_alias_normalizes_to_target`

- 根因：该用例用 `os.symlink()`，其 `try/except OSError: pytest.skip` 守卫**不足以发现「调用不抛异常但链接并未创建」**。本机实测：`os.symlink` 返回成功，但 `os.path.islink()` 为 `False`、`nt._getfinalpathname` 报 `WinError 2`（沙箱拦截符号链接创建）。
- **生产代码无罪**：`_norm_workspace`（`tick.py:702-716`）用 `os.path.realpath`。我用真实 junction（`mklink /J`）实测：`realpath(link) == realpath(real)` → `True`，归一化正确。
- 建议：测试应加 `if not link.is_symlink(): pytest.skip(...)`，或改用 junction。

---

## 四、尚未实现 / 待完善

### 4.1 已登记在案的缺口（文档已标注，实测确认）

| 项                                                                                                                                      | 状态                                                                                  | 证据位置                                                                 |
| -------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 7 个 RPC 方法只有名字：`context/refresh`、`context/promote`、`control/notify`、`control/followup`、`control/steer`、`control/spawn`、`lease/release` | 调用返回 `STATE_FORBIDDEN: method not implemented`                                      | `tests/unit/test_rpc_dispatch_tables.py:49-59` `KNOWN_UNIMPLEMENTED` |
| 档 4 并行加派 + 分区租约                                                                                                                        | 未接线；`decide()` 已停止产出 PARALLEL；`partition_id` 全为默认空值；`lease/partition-conflict` 从未写出 | `escalation.py:74-91`、ADR-005                                        |
| E2 饥饿保护                                                                                                                                | `fairness_states` 恒为空，`observe_tick` 只在测试里出现                                        | `docs/RELEASE-PLAN.md:186-193`                                       |
| L2 云侧通知 / L3 常在线中继                                                                                                                     | 未部署，`accepted_debt`                                                                 | `quality/claims.json`                                                |
| 能力自动探测（v2）                                                                                                                             | 未做，靠人工声明 + 拒接兜底                                                                     | DESIGN §8.4                                                          |
| 合同模板市场 / 复杂 UI                                                                                                                         | 未做                                                                                  | DESIGN §16                                                           |
| 跨机器分发 / 跨网络中继                                                                                                                          | 明确非目标                                                                               | README「Boundaries」                                                   |
| R3b/R4b/R5/R6                                                                                                                          | 未完成（最终候选、隔离安装、安全扫描、发布决策）                                                            | `docs/RELEASE-PLAN.md:41`                                            |

### 4.2 文档**未**标注、但代码实测未接线的能力（本报告新增）

1. **上下文投影的「刷新 / 提升」闭环缺失** — `context/refresh` 与 `context/promote` 未实现，意味着 DESIGN §4.1「来源版本变化 → 请求刷新」「scratch → progress/阶段摘要的显式提升」**没有协议入口**；手工改过的 `handover.md`/`progress.md` 也无处提交。
2. **提醒与转向是「只记账不执行」** — 档 1/2 的 `ESCALATION_REMINDED`/`ESCALATION_STEERED` 事件无消费者；DESIGN §6.2「向活跃会话注入提醒轮次 / steer 扳话题」在运行时不存在。
3. **人类编辑门（dirty/tampered 检测）未接线** — 见 3.7.1。
4. **auto-handover 只有探测器** — 见 3.9.1；README 首屏把「auto-handover watches `active.md` size and fires before the context window dies」列为已交付的韧性链路之一，**实际只发事件**。
5. **resume 未接入 daemon** — 「resume reads `active.md` + `handover.md` to spin up a fresh attempt」目前只是人工 CLI/MCP 入口。
6. **`lock_new_attempts` 强制面未接线** — 见 3.10.2。
7. **多合同公平性的容量半** — `can_dispatch` 未调用（见 3.2.1），soft cap 默认 0 且无消费者。
8. **`max_escalations` / 合同级 `max_concurrent_attempts` 实际不生效** — 见 3.2.2/3.2.4。
9. **`enabled` 开关不持久化** — 注册表无 `save_to_file` 调用点。

### 4.3 待裁决的架构问题

- **ADR-005（工作区隔离）** 的 4 个待裁决问题：档 4 实现（分区分配 + 工作区隔离）与 `git worktree` 隔离是同一件事的两面。
- **档 4 去留**：实现分区并行，或正式取消该档（当前是「诚实化的串行重派」中间态）。
- **legacy 命名空间退场**：17 个 `longtask_*` 别名的删除时机（SPEC §19.3 要求保留至少一个次版本）。

---

## 五、优先级建议

| 优先级     | 项                                       | 理由                                  |
| ------- | --------------------------------------- | ----------------------------------- |
| **立即修** | 3.1 `set_enabled` 丢 `models`            | 静默破坏 authority 模型绑定，且无测试保护          |
| **立即修** | 3.5.1 executor enable/disable 无权限门      | 模型客户端可改执行器池                         |
| **立即修** | 3.5.2 session_token 豁免 MCP              | 可伪造 verifier evidence               |
| **立即修** | 3.4.1 verifier 派工顺序                     | 异常击穿 daemon + 孤儿进程 + generation 少 1 |
| **尽快修** | 3.6.1/3.6.2 幂等不绑合同                      | 跨合同假成功 + 信息泄漏                       |
| **尽快修** | 3.2.1 `can_dispatch` 未接线                | 并发帽形同虚设                             |
| **尽快修** | 3.12.1 `cancel()` 不杀树                   | DESIGN §14.3 取消保证不成立                |
| **尽快修** | 3.7.2 `_atomic_write` 无 fsync / tmp 不唯一 | 并发投影损坏                              |
| **应修**  | 3.3.1–3.3.5 升级阶梯空档                      | 文档承诺的动作不存在                          |
| **应修**  | 3.7.1 dirty 门未接线                        | DESIGN §3.1 人类编辑门不存在                |
| **应修**  | 3.10.1 reconcile 不跑 typed check         | 重启后 verifier 卡 CANDIDATE            |
| **应修**  | 3.8.1–3.8.2 上下文容量自相矛盾                   | 永久 fail-closed 拒绝                   |
| **收尾**  | 3.14 测试守卫 + 过期日志清理                      | 假失败掩盖真信号                            |

---

## 附录：审查方法说明

- 文档层：通读 `README.md`、`ARCHITECTURE.md`、`DESIGN.md`（v0.13）、`CHANGELOG.md`、`docs/LHGP-SPEC.md`（1.0）、`docs/RELEASE-PLAN.md`。
- 代码层：三个方向并行走查（调度/推动、持久化、RPC/MCP/适配器），覆盖 `src/` 全部 448 个文件中的关键实现。
- 实测层：对 18 条结论亲自读源码或复现（第一轮 6 条 + 第二轮 12 条，见附录 B），并运行全量测试套件定位当前失败项。
- **诚实边界**：标注 `[走查]` 的条目来自探索代理的阅读，未逐行复核；标注 `[实测]` 的条目我本人验证过。本报告不构成「已发现全部缺陷」的声明——项目自身的 README 也明确「Passing the existing test suite does not establish the absence of defects」。

---

## 附录 B：第二轮复核记录（2026-09-12）

逐条读源码验证，结果如下。**12 条全部成立**，其中 2 条在复核中被加强。

| # | 条目 | 复核方法 | 结果 |
|---|---|---|---|
| 1 | 幂等不绑合同 | 读 `store.py:1535-1555/1770-1790/1950-1975` + `events_query.py:233-235` | 成立。**加强**：根因是 SQL `WHERE request_id = ?` 无 `contract_id` 过滤，全库检索 |
| 2 | `goal_prepare` 重放 | 读 `goal.py:209-236` | 成立。先按本次 `contract_id` 查库即返回，完全不校验 request_id 归属 |
| 3 | canonical 丢 `auto_approve` | `grep auto_approve` 两文件对比 | 成立。canonical 侧零次出现，legacy 侧 `:191-215` 构造 |
| 4 | attempt 无状态机校验 | 读 `attempts.py:259-301` | 成立。docstring 自陈不校验；非终态写入把 `terminal_at` 置 NULL |
| 5 | `prune_terminal_events` 删幂等锚 | 读 `maintenance.py:98-140` | 成立。裸 `DELETE FROM events` + 裸 `conn.commit()` |
| 6 | `_DIFF_FIELDS` 漏列 | 读 `maintenance.py:22-35` + `schema.py:172-198` | 成立。修订表确有那 7 个语义列，逐列核对 |
| 7 | 上下文容量自相矛盾 | 读 `context.py:117` 与 `:605-620` | 成立。`max_bytes<3750` 时 memory 段单独超限 |
| 8 | reconcile 不跑 typed check | `grep evaluate_check` 全仓 + 读 `reconcile.py:380-447` | 成立。**加强**：全 `cli/` 层仅 `runner.py:803` 一处调用点；reconcile 结算的 verifier 无 `evidence` → 带 spec 合同卡 ACTIVE |
| 9 | `_validate_arguments` 不查 enum | 读 `mcp_server.py:2379-2415` | 成立。函数体只有 required/类型/未知键三类 |
| 10 | `_lifecycle.py` 硬编码 + 吞异常 | 读 `_lifecycle.py:130-163` | 成立。`protocol_version=2` 硬编码；两处 `except Exception: return` 不留痕 |
| 11 | `cancel()` 提前 return | 读 `subprocess_adapter.py:625-660` | 成立，措辞已修正。两处注释互斥，实现取前者 |
| 12 | 投影 dirty 门死代码 | `grep check_projection_dirty` 全仓 | 成立。仅定义、零生产调用；`PROJECTION_DIRTY` 零 append |

**仍未复核（保持 `[走查]`）**：`store.py:2009-2022` write_back 忽略参数、`projections.py:150-152` `contract_dir` 无路径校验、`insights.py:137-140` brief 混入他合同通知、`schema.py:41-45` 缺 `busy_timeout`、`errors.RETRYABLE` 死配置、`store.py:1635-1645/1904-1919` 改写历史事件、`store.py:1079-1083` 裸 except、`registry.py:444` 合同级并发不生效。

---

# 第四部分：修复记录（第三轮，2026-09-12）

> 基线：`251e225`。验证：`uv run python scripts/quality_gate.py` → **ALL PASS (7 gates)**。
>
> - 第一批（A–B，16 项 + 门禁加固）：`1701 passed, 10 skipped`，覆盖率 79.92%
> - 第二批（D2，8 项）：`1726 passed, 10 skipped`，覆盖率 79.98%
> - 第三批（D3，5 项）：`1744 passed, 10 skipped`，覆盖率 80.13%
> - 第四批（D4，2 项）：`1758 passed, 10 skipped`，覆盖率 80.49%
> - 第五批（D5，3 项 + 1 项诚实标注）：`1761 passed, 10 skipped`，覆盖率 80.53%
> - 第六批（D6，2 项）：`1770 passed, 10 skipped, 0 failed`，覆盖率 **80.59%**（地板 78%）
>
> 六批合计 **36 项修复**，改动 49 个文件。mypy strict：227 个源文件无问题；arch 门 0 违规。
> 测试从基线 1644 → 1770（**+126**），覆盖率 79.9% → **80.59%**。

## A. 已修复并配回归测试（16 项）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 1 | `longtask/adapters/registry.py:378-396` | `set_enabled` 手工重建漏 `models` → 模型白名单被重置为 `("*",)` | 改 `dataclasses.replace`（结构上不可能再漏字段） |
| 2 | `lhgp/portfolio/types.py:47` | `to_dict` 用 `c.__dict__`，而 `ContractSummary` 是 `slots=True` → **非空 portfolio 必崩**（CLI `portfolio` + MCP `tool_portfolio`） | 改 `dataclasses.asdict` |
| 3 | `lhgp/rpc/handlers/executor.py:66,89` | executor enable/disable 无 actor 校验，任何客户端可改授权池 | 加 `require_principal`；模型客户端现被拒（`AUTH_FAILED`） |
| 4 | `longtask/persistence/store.py` 三处写入口 + 新 `_replayed_events` | 幂等 `request_id` 不绑合同（根因：`events_query.py:234` SQL 无 `contract_id` 过滤）→ 跨合同复用被静默当成功 | 归属校验，跨合同抛 `IdempotencyMismatchError`（RPC 侧映射 `IDEMPOTENCY_REPLAY_MISMATCH`） |
| 5 | `longtask/cli/runner.py:1207-1290` | `_dispatch_verifier` **先 spawn 后 acquire_lease**：generation 少 1、`LeaseCASError` 击穿 daemon、孤儿进程无人跟踪 | 改为 CAS 占租约 → 编译输入 → prepare/spawn；`LeaseCASError`/`CapacityRefusedError` 均走 `_fail_attempt`（负责释放租约） |
| 6 | `longtask/cli/main.py:993-1013` | `feedback diff` 未校验 `contract_id` 拼路径 → 目录穿越 + 任意建目录 | 新增 `lhgp/contracts/validation.is_safe_contract_id` 作为**单一事实来源**，RPC 与 CLI 共用（原正则在 `rpc/handlers/_common.py` 私有，CLI 无法复用） |
| 7 | `lhgp/persistence/maintenance.py:22-70` | `_DIFF_FIELDS` 漏 7 个语义列（authority/attention/continuity/auto_approve/context/execution/client_meta） | 补齐清单与 SELECT（`zip(strict=True)` 会拦住错位） |
| 8 | `longtask/persistence/attempts.py:259-301` | 非终态写入把 `terminal_at` 清成 NULL（终态不可变在存储层无兜底） | 改 `CASE WHEN ? THEN ? ELSE terminal_at END` |
| 9 | `longtask/persistence/context.py:614-620` | memory 预算只有下限 1500、无上限 → `max_bytes < 3750` 时恒 `CapacityRefusedError` | `min(max(budget, 1500), policy.max_bytes)` |
| 10 | `lhgp/persistence/timeline.py:83-103` | 只有 `summary` 转义，`actor`/`attempt_id` 原样进 `innerHTML`；`data_json` 内联 `<script>` 可被 `</script>` 破出 | 两个字段也 `html.escape`；`data_json` 转义 `</` → `<\/` |
| 11 | `lhgp/memory/index.py:210-243` | `_fit_to_budget` 按每项 `+1` 记账，而 `_render_section` 用 `\n\n`（2 字节）→ n 项溢出 n−1 字节 | 记账改为严格镜像渲染；docstring 补上「单条超限时故意截断、可能越界」的例外（既有测试钉住了该行为） |
| 12 | `lhgp/templates/research.json:11` | `"\[source\]"` 是非法 JSON 转义 → 整个模板 `json.loads` 抛异常 | `"\\[source\\]"` |
| 13 | `lhgp/templates/release-check.json:15` | `max_escalations: 0` 被自家校验判非法 | 改 1 |
| 14 | `lhgp/templates/validate.py:52-100` | fail-open：deadline 后缀白名单跳过解析、`acceptance` 非 dict 静默跳过 | 一律交 `fromisoformat`；`acceptance` 非 dict 显式报错 |
| 15 | `lhgp/enforcement/levels.py` + `main.py:1131` + `mcp_server.py:1124` | CLI/MCP 的 deadline report 不传 `total_seconds` → `elapsed_ratio` 恒 0.5 → **未逾期合同永不告警**；模块 docstring 的档位描述与代码相反 | 新增 `contract_window_seconds` 单一入口，三条路径共用；docstring 改为与代码一致 |
| 16 | `tests/integration/test_contract_visibility.py:401-409` | `os.symlink` 在本机静默 no-op，测试缺守卫 → **假失败** | 加 `link.is_symlink()` 守卫 |

## B. 门禁脚本加固（此前**零测试**）

| 脚本 | 假阴性 | 加固 |
|---|---|---|
| `scripts/arch_check.py` | `imported_modules` 看不见三种写法：① `from longtask import persistence`（别名丢失）；② 相对导入（`node.level != 0` 整体丢弃）；③ `from longtask.adapters import subprocess_adapter`（只报父包） | 解析相对导入到绝对路径 + 每个 alias 作为子模块候选；抽出可测的 `rule_violated` |
| `scripts/claims_check.py` | 未传 `format_checker` → 所有 `"format": "date"` 不生效；`is_repo_relative` 放行 Windows 盘符相对路径 `C:foo`（`REPO_ROOT / "C:foo"` 会**替换盘符**） | 加 `FORMAT_CHECKER`；拦盘符、前导 `/`、`\` |

**加固后重跑：两条门对当前代码仍报 0 违规 / OK** —— 说明代码本身干净，是门看不见。

新增测试：
- `tests/unit/test_arch_check.py`（14 条）：三种写法的正向能力（门必须能抓到）+ 无误报 + 当前树 0 违规 + 基线为 0。
- `tests/unit/test_claims_anchor_check.py`：`is_repo_relative` 正反例。

## C. 判定为「有意设计」、未改

- **`lhgp/admission/eligibility.py:48-55`**：无 `authority` 字段的草稿得到空 `Authority()`，导致 `explicit_allowed=True`、任意 executor 满足 7 条件。表面违反 default-deny，但 `registry.match_candidates` 明确注释「未声明任何绑定的**存量合同保持旧行为**——语义是『没有设防』而不是『全部拒绝』」。这是有意的向后兼容，改它会让所有存量合同失效。**属于设计张力，不是缺陷**：建议后续单独立项，并让 `prepare` 的 offer 明确告知用户该合同"未设防"。

## D2. 第二批修复（同日后续）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 17 | `longtask/cli/watch.py:238-270` | `_parse_event_kinds` 用 `suppress(ValueError)` 丢掉非法 kind，集合为空时返回 `None`——而 `None` 语义是**全放行**。于是 `--kinds typo` 显示所有事件而不是报错 | 非法值抛 `ValueError` 列出合法 kind；`main()` 捕获后 exit 2 |
| 18 | `longtask/cli/watch.py:180` | `json.loads(event.payload_json)` 无保护，一条坏记录让整个 tail 崩溃 | 新增 `_safe_payload`，坏值转 `_raw`（与 trace 约定一致） |
| 19 | `longtask/cli/doctor.py:92-124` | `connect`+`ensure_schema` 会**新建**空库，然后报 `state.db healthy` → 对拼错/全新的目录也报健康（README 推荐 doctor 作排障第一步，误导代价最高） | 连接前记 `db_existed`；新建时报 "did not exist; created an empty one" + "no data — wrong --data-dir, or a fresh install" |
| 20 | `longtask/cli/doctor.py:126-148` | `registry.json` 缺失 → 空注册表 → "0 enabled" ok=True，看不出是「文件没了」还是「都关着」（后果都是派不了工） | details 补 `registry.json not found` / `none enabled — no executor can be dispatched` |
| 21 | `lhgp/flow/contract_flow.py:100-110` | `_resolve_by_module_path` 用 `src_root / seed.replace('.','/')`，对 `../../x`、`/abs/x`、`C:x`、UNC 都不设防 | 新增 `_is_within` 包含性校验 + 拦前导分隔符/盘符/`..` |
| 22 | `lhgp/flow/contract_flow.py:234-247` | `walk_contract` **先** `read_text()` **后** `relative_to` 校验——越界文件已进内存才被拒 | 顺序对调：先算 `module_name`（含 `relative_to`），再读 |
| 23 | `longtask/rpc/handlers/_lifecycle.py:140` | 硬编码 `protocol_version=2`，而 `PROTOCOL_VERSION = 1`（`longtask/__init__.py:7`）——协议升版时这里静默滞后，版本不符在服务端是 fail-closed 拒收 | 改用 `PROTOCOL_VERSION` 常量 |
| 24 | `longtask/rpc/handlers/_lifecycle.py:148-158` | 两处 `except Exception: return`，自动阶段合同创建失败**零痕迹** | 保留「对调用方静默」的设计，但加 `logger.warning(..., exc_info=True)` |

**新增测试**：`tests/unit/test_doctor.py`（9 条，doctor 此前只有 `test_cli.py` 的间接覆盖）；
`test_watch.py` 翻正 `test_invalid_name_silently_ignored`（它把 fail-open 当预期行为钉住了）→ 改为断言 fail-closed，并补 `_safe_payload` 5 条；
`test_contract_flow.py` 补 seed 越界 9 条 + `_is_within`。

## D3. 第三批修复（同日后续）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 25 | `longtask/cli/daemon_proc.py:315-337` | `lhgpd_entrypoint()` **完全不解析 `sys.argv`**——`lhgpd --data-dir X` 静默跑在默认 `~/.lhgp` 上，用户以为在管这份合同，实际派工/事件/工作区改动发生在另一份数据里 | 加 argparse（`--data-dir` / `--interval`），并透传 `interval_seconds` |
| 26 | `longtask/cli/main.py:677-686` | ① `--data-dir` 对 `migrate` 无效却静默；② `root.mkdir()` 早于 migrate 分支 → **dry-run 也凭空建出数据目录** | migrate 分支移到 `root` 计算之前；带 `--data-dir` 时显式告警 |
| 27 | `longtask/cli/main.py:1676` | `prepare --file` 的 `read_text`/`json.loads` 未捕获 → 坏输入抛裸 traceback（同文件 `plan submit` 早就有友好错误，一个 CLI 两种待遇） | `OSError`/`JSONDecodeError`/非 object 三类都 `Error: ...` + exit 2 |
| 28 | `longtask/cli/main.py:1796` | `patch --guidance` 的 `json.loads` 未捕获 | 同上 |
| 29 | `longtask/mcp_server.py:2386-2422` | `_validate_arguments` 只查 required/类型/未知键，`enum`/`minimum`/`maximum`/`minLength`/`maxLength`/`minItems`/`maxItems`/`pattern` **全部不强制**——schema 里声明的约束形同注释 | 补齐八类约束。例：`lhgp_notifications.status` 早就声明 `{"enum": ["pending","leased","sent"]}` 却一路放行到 `notifications.py` 才被拒 |

**新增/翻正测试**：
- `test_p6_paths_migrate.py` 补 5 条（`--data-dir` 生效、无参回落默认、`--interval` 透传、`migrate` 不 mkdir + 告警）；
  同时把既有 `test_lhgpd_entrypoint_runs_loop_foreground` 改为显式传 `[]`——`argv=None` 时 argparse 会读到 pytest 自己的参数。
- `test_cli.py` 补 4 条 JSON 错误处理，并加 `@pytest.mark.real_entry`（项目有**未标记测试数棘轮** 114，不加会让整会话 `UsageError`）。
- 翻正 `test_mcp_server.py::test_unknown_notification_status_returns_invalid_params`：它断言的是**下游**错误信息，而现在边界就该拦下；改为断言合法值被一并告知。

## D4. 第四批修复（同日后续）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 30 | `longtask/cli/tick.py:152,492-501` | `TickCapacityLedger.can_dispatch` **从不调用**（只调 `record_dispatch`）→ `max_dispatches_per_tick` 无论配什么都无效；而注释与 `fairness.py` docstring 都声称"规则 2 已接线" | 派工前真的问一次 `can_dispatch(cid)`；`run_daemon_tick` 新增可选 `fairness_config` 参数以便配置与测试。默认 cap=0（不限）→ 不配置时行为完全不变 |
| 31 | `longtask/cli/main.py:612-636,697-704` | `--dry-run` 只被 `_dispatch_rpc` 拦住；`plan submit/signoff`、`contract user-confirm`、`feedback submit/diff`、`memory add/expire`、`proposal-apply`、`prune-events`、`start`/`stop`/`kill-switch` **全部绕过 RPC 直连 DB 或起停进程**，照写不误——`--help` 却写着"不写库" | 新增 `_dry_run_direct_write(args)` 单一出口，在 `dry_run` 下统一拦下并打印 `would <what>`；只读子命令返回 `None` 不被误伤 |

**新增测试**：
- `test_contract_visibility.py` 补 `TestPerTickCapacityCap` 3 条（cap=1 时单 tick 只派 1 个；默认不限仍派 2 个；帽是 per-tick 不会永久饿死）。
- `test_cli.py` 补 `TestDryRunCoversDirectWrites` 9 条（含"dry-run 下 plan submit 真的不落事件"、"只读子命令不被误伤"、"不 dry-run 时仍正常写"）。

## D5. 第五批修复（同日后续）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 32 | `lhgp/memory/store.py:70,256,266` | `record_memory`/`expire_due`/`bump_score` 用裸 `with conn:`——它会**提交**任何正在进行的外层事务。`feedback.record_evaluation` 正是在调用方事务里调 `record_memory`，于是评价行被提前落库，与随后的 `USER_EVALUATION_SUBMITTED` 事件不再原子（中途失败留下"有评价、没事件"的半截状态） | 改用项目自带的**可重入** `transaction(conn)`（已在事务中则复用外层，不提前提交） |
| 33 | `longtask/cli/main.py` 两处（`contract user-confirm` / `proposal-apply`） | `ensure_schema(conn)` 写在 `try:` **之外**，抛错（迁移失败/库损坏）时连接永不关闭 | 移入 `try` 内 |
| 34 | `longtask/cli/doctor.py` | 库存在但 **0 份合同**时仍只报 `healthy`。最常见的成因是 `--data-dir` 指错，或某个只读命令顺手建了空库 | details 追加 `— empty; is --data-dir pointing at the right place?` |

**判定为"改不动/不该改"、只做诚实标注**：

- **`_open_read_conn` 名不副实**（`main.py:56`）：`connect` 会在文件不存在时**新建** `state.db`，`ensure_schema` 还会建表迁移。所以 `brief`/`board`/`stats`/`trace`/`forecast`/`portfolio`/`diff` 这些只读命令都会在目标目录留下一个空库；`--data-dir` 拼错时不报错，只是"什么都没查到"。真正的 fail-closed 要改 14 处调用点，超出"最小修复"，故只补 docstring 说明，并由上一行的 doctor 提示兜住后果。

**新增测试**：`test_memory_index.py` 补 `TestRecordMemoryDoesNotCommitOuterTransaction` 3 条
（外层回滚后内层写入必须一起消失；正常提交仍要落库；外层抛异常要整体回滚——
后两条是防止把"提前提交"修成"永不提交"）。

## D6. 第六批修复（同日后续）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 35 | `lhgp/wiki/sync.py:168-186,219-220` | frontmatter 的 `id:`/`title:` **未加引号**。契约标题是用户自由文本，出现 `:`（"Phase 1: 收尾"）或 `#` 时真正的 YAML 解析器（Obsidian）会读错或读不到，且不报错。另 `_yaml_list` 只转义双引号、**没转义反斜杠**——`a\` 会吃掉后面的闭合引号 | 新增 `_yaml_scalar`（反斜杠→引号→CR→LF 顺序转义）并用于 id/title；`_yaml_list` 改走同一函数 |
| 36 | `longtask/cli/main.py:817-843` | **DESIGN §3.1「人类编辑门」终于接上**：`check_projection_dirty` 全仓无调用、`PROJECTION_DIRTY` 事件从未写出，而 `rebuild_projection` 会直接把盘上 contract.yaml 覆盖成权威库序列化结果——用户手改的内容**无声消失** | plain `rebuild` 先做 dirty 检测；脏则 exit 1 并提示 `--revert`。`--revert` 的语义就是"以库为准"，保持不变 |

**兼容性已验证**：`scripts/build_wiki_index.py` 的解析器对 scalar 走 `value.strip("\"'")`、
对列表走 `v.strip().strip("\"'")`，加引号后解析结果不变——实测
`{'type': 'contract-page', 'id': 'lt-fm-1', 'title': 'Phase 1: 收尾 #3', 'tags': ['topic/wiki']}`。

**新增测试**：
- `test_wiki_publish.py` 补 `TestFrontmatterIsValidYaml` 6 条（冒号、`#`、双引号、
  反斜杠转义顺序、换行、`_yaml_list`），并翻正既有断言为加引号形式。
- `test_cli.py` 补 `TestRebuildHonoursHandEdits` 3 条（干净时正常重建；脏时拒绝并提示
  `--revert`；`--revert` 按设计覆盖）。

## D. 排查发现但**本轮未修**（需单独任务卡）

`cancel()` 提前 return 与注释互斥（`subprocess_adapter.py:641-646`）；`--dry-run` 被 8 个命令静默忽略（`main.py`）；`doctor` 对空目录制造"健康库"（`doctor.py:96-148`）；`migrate`/`lhgpd` 忽略 `--data-dir`；`watch.py` 的 `--kinds` fail-open + `json.loads` 无保护；`feedback/store.py` 嵌套事务提前提交；`flow/contract_flow.py` 先读文件后 `relative_to` 校验；`_lifecycle.py` 两处 `except Exception: return`；`wiki/sync.py` frontmatter 未加引号；`_validate_arguments` 不查 enum/min/max。

这些要么涉及行为语义裁决（需先改 SPEC），要么改动面超出"最小修复"，建议按 `RELEASE-PLAN.md` 的规矩单独立卡：确认 SPEC → 回归测试 → 最小实现 → 全量门。
