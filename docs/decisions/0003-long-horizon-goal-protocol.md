# ADR-003: 以远期目标承诺而非长任务作为协议中心

## 状态

已接受（作为本轮权威设计基线；后续变更须由新 ADR 取代）

2026-09-05 更新：产品叙事由 [ADR-004](0004-contract-runtime-and-release-scope.md)
细化并部分取代；本 ADR 的协议命名、标识及兼容迁移决策继续有效。

## 日期

2026-09-01

## 背景

项目当前使用“远期任务协议 / Long-Term Task Protocol / LongTask”。这个名字把读者引向长时间运行、延时任务和 cron 调度，而用户真正要解决的是：目标在原会话、原 Agent、原模型退出后仍由一个独立主体持有，并能在 Deadline 约束下被不同执行者接力和验收。

相邻系统已经覆盖 durable workflow、thread checkpoint、session memory 和长时间 Agent run。继续用 LongTask 会把项目放进已有类别，也无法解释“合同拥有目标、会话只拥有一次尝试”这一差异。

## 决定

1. 中文规范名使用 **远期目标协议**。
2. 推荐英文规范名使用 **Long-Horizon Goal Protocol**，缩写 **LHGP**。
3. 核心领域对象命名为 `Goal Commitment`；`Task` 只表示计划中的可执行工作单元。
4. 规范标识使用 `lhgp`；后续 CLI、守护进程和数据目录分别迁移为 `lhgp`、`lhgpd`、`~/.lhgp`。
5. `longtask`、`longtaskd`、`~/.longtask` 在迁移窗口内作为兼容别名，不再出现在新用户的首要叙事中。
6. 规范、参考运行时和平台插件分层命名。Codex 插件是 LHGP 的一个客户端，不代表协议本身。

## 考虑过且拒绝的方案

### Long-Term Task Protocol / LongTask

- 优点：现有代码和文档已经使用，迁移成本最低。
- 拒绝原因：中心词错误；无法与队列、cron、durable task runner 拉开认知边界。

### Durable Goal Protocol（DGP）

- 优点：直接表达跨重启持久性。
- 拒绝原因：弱化 Deadline 和远期时间跨度；“durable”更像基础设施属性。

### Goal Continuity Protocol（GCP）

- 优点：准确表达跨会话接力。
- 拒绝原因：GCP 与 Google Cloud Platform 强冲突，搜索与沟通成本过高。

### Persistent Goal Protocol（PGP）

- 优点：表达目标不随会话消失。
- 拒绝原因：PGP 与 Pretty Good Privacy 强冲突，且“persistent”容易被理解为普通存储。

## 后果

- README、包元数据、CLI、MCP 工具、Skill 和 schema 需要分阶段迁移。
- 当前 Python 包可暂时保留 `longtask` 模块路径，避免一次性破坏实现；公开标识先改，内部路径最后迁移。
- 所有新增设计必须用 Goal、Commitment、Attempt 三层词汇，不能再把它们合并成 task。
- 旧文档若与 [LHGP-SPEC.md](../LHGP-SPEC.md) 的产品语义冲突，以新规范为准；具体代码能力仍以测试和 claims 证据为准。
