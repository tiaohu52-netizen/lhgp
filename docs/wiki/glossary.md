---
title: LHGP Glossary
type: glossary
status: permanent
tags: [audience/ai, audience/human, type/reference]
audience: [human, ai]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Glossary

协议核心术语。新增概念必须在这里注册后才允许在其它 wiki 页或代码注释里使用。
跨页引用时**优先**用 `[[glossary#term-name]]` 而不是直接解释,这样术语表是单一真相源。

## 协议层

### Contract / 合同

不可变的意图声明 —— "做完什么算完成"。由 5 个字段组拼装:`objective` / `deadline_at` /
`hard_constraints` / `acceptance` / `workload_initial_hours`,加上 `budget` / `authority` /
`attention` / `continuity` / `soft_guidance` 等扩展字段。
**冻结区**:objective / deadline_at / hard_constraints / authority 不能经 patch 改。

### Attempt / 执行尝试

合同的一次具体执行。`executor` 出产物,`verifier` 交叉核验。attempt 不能跨合同复用。
**角色**(AttemptRole):`executor` / `verifier` / `planner`。
**状态机**:admitted → starting → running → (succeeded | failed | cancelled | stale | orphaned)。

### Lease / 租约

executor 持有合同某 attempt 的临时执行权。带 `generation`(CAS)和 `heartbeat_at`(超时回收)。
**fencing**:旧 generation 的 write_back 永远被拒,防止"僵尸 executor 覆盖新结果"。

### Acceptance / 验收

判断 attempt 是否满足合同的机器可执行清单。`standard`(人话) + `checks`(typed check 数组) +
`verifier`(cross_check / none)。每个 check 跑完产出 `CheckResult(passed, evidence)`。

### Context / 临时上下文

attempt 派发前编译的认知工作集(`active.md` + `scratch.md`)。**有版本 + 有过期 + 有容量合同**。
`active.md` 是只读快照,`scratch.md` 是模型可编辑区(标注为 untrusted,防止误读为指令)。

### Handover / 交接

跨 attempt 传递的工作现场。`current_stage` / `remaining` / `next_action` /
`estimate_remaining_hours` / `open_risks` / `source_attempt_id`。修复 attempt 启动时
自动注入 `task_prompt` 的 addendum(1200 字符内)。

## 数据层

### Event / 事件

不可变的事实记录。所有合同状态变化、attempt 状态、租约、context 快照、用户评价、
deadline 升级都落事件,事件名 `domain/verb` 格式(例 `contract/prepared` /
`deadline/breach-locked`)。
**幂等去重**:request_id 字段保证重放不产生第二个事件。

### Schema Version / 架构版本

`STORE_SCHEMA_VERSION` 决定 schema v 几。新增表 → bump + 写 `_migrate_vN_to_vN+1`。
**fail-closed**:`_check_schema_version` 看到 `current > expected` 直接 raise,要求只读。

### EventType / 事件类型

`Enum(StrEnum)`,值是事件字符串。**新事件必须先在 `lhgp.persistence.events.EventType`
里加枚举 + 同步事件**;SQL 里**不能写死字符串**,得用 `EventType.X.value` + 参数绑定
(防止改名时静默失配)。

## 调度层

### Deadline Level / 死线级别

`NORMAL` / `WARNING(剩余 30% 时间)` / `URGENT(剩余 5% 或 < 60s)` / `BREACHED`。
阈值常量 `WARNING_THRESHOLD=0.30` / `URGENT_THRESHOLD=0.05` 在 `lhgp.enforcement.levels`。
**bumped by integration test** —— `tests/unit/test_enforcement_levels.py` 把 0.7/0.05/0.30 全锁住。

### Daemon Tick / 调度心跳

`run_daemon_loop` 每轮执行的固定流程:reconcile → terminate 终态合同 attempt →
poll_attempts → 消费 interrupt/verification 请求 → **enforce_deadlines** →
run_daemon_tick → drain_notifications → start_attempts → 更新 L0/L1 唤醒。
`_enforce_deadlines` 必须在 `run_daemon_tick` **之前**调用,breached 锁住立刻生效。

## P6 概念(反馈回路 / 多合同 / 自动演化)

### User Evaluation / 用户评价

合同到终态后由用户/外部对结果打 1-5 分 + verdict(accept/partial/reject) + comments。
**是 human-anchored signal** —— 质量分数的第一权重(60%)。
落 `user_evaluations` 表 + `user/evaluation-submitted` 事件。

### Acceptance Diff / 验收态 diff

attempt 报告的产物 vs 当前 workspace 实际状态的文件级对比。输出 created/modified/deleted 列表。
落 `acceptance_diffs` 表 + `acceptance/diff-computed` 事件。
**作为 auto-evolve 的输入信号**(diff_efficiency 15% 权重)。

### Template Signal / 模板信号

从 user_evaluations + acceptance_diffs 矿出来的"这个合同值得复用"的模式抽象。
包含 quality(加权 0-1) + acceptance_vocabulary(check kinds 列表) + objective_keywords + title_hint。

### Auto-Evolve / 自动演化

`overall ≥ 0.7`(用户选的门槛)的 TemplateSignal 自动写 `templates/auto-<cid>-r<rev>.json`。
**无人工门控**,用户选 auto_evolve 模式。同 stem 碰撞自动 `-n2` / `-n3` 后缀,
**永不覆盖**。

### Portfolio Snapshot / 多合同聚合

`portfolio_summary` 一次拉所有合同 + by_state/by_deadline/by_acceptance 计数 + 最新评价 join。
**只读**,dashboard / health check 用途。

### Trace / 单合同时间轴

`trace_contract` 单合同完整事件流 + 最新评价 + 最新 diff。**只读**,调试"这份合同为什么这样关闭"用。
