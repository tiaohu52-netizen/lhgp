# 架构指南（ARCHITECTURE）

> 本文件是代码库结构的权威地图。新贡献者读完这一页就能知道"哪个模块做什么、
> 该改哪里、不该改哪里"。SPEC 是语义权威，本文件是**物理布局**权威。

## 双命名空间（为什么有两棵树）

```
src/
├── lhgp/       ← 规范命名空间（canonical）：协议概念的正名
└── longtask/   ← 遗留命名空间（legacy）：历史包名 + 运行时大模块
```

SPEC §19.3 规定迁移顺序：**不得先做全仓机械 rename**。最终目标是在某个
次版本删除 `longtask` 别名，但当前是迁移中间态。规则：

- **新代码写在 `lhgp/` 下**，用规范名；
- **`longtask/` 里的实现暂不搬**（除了修 bug）——搬移是大变更，须单独 PR；
- 两边同名文件中，一侧是**真身**（>5 行的实现），另一侧是**门面**（≤5 行
  的 re-export）。真身在哪一侧由历史决定，见下表。

## 真身位置地图

| 领域 | 真身（实现所在） | 门面（另一侧） | 说明 |
|---|---|---|---|
| **contracts/** | `lhgp/contracts/` | `longtask/contracts/` | 数据模型：draft/view/budget/authority/state_machine |
| **acceptance/** | `lhgp/acceptance/` | `longtask/acceptance/` | 验收：typed check、evaluator、verdict |
| **admission/** | `lhgp/admission/` | `longtask/admission/` | 准入：7 条件判定、offer、refuse |
| **forecast/** | `lhgp/forecast/` | `longtask/forecast/` | Deadline 风险快照模型 |
| **feedback/** | `lhgp/feedback/` | `longtask/feedback.py` | 用户评价 + 验收态 diff（types/store/diff） |
| **learning/** | `lhgp/learning/` | `longtask/learning.py` | 信号提取 + 自动模板演化（extractor/evolver） |
| **portfolio/** | `lhgp/portfolio/` | `longtask/portfolio.py` | dashboard 聚合 + 单合同 trace（summary/tracking） |
| **enforcement/** | `lhgp/enforcement/` | `longtask/enforcement.py` | deadline 多级升级 + lock（levels/enforcer/report） |
| **wiki/** | `lhgp/wiki.py` | `longtask/wiki.py` | 协议内 wiki 阅读器（read-only CLI；不接 SQL，扫 .index.json） |
| **memory/** | `lhgp/memory/` | `longtask/memory.py` | 协议级长期记忆（SQLite `memories` 表 / types / store / index / CLI；`record_evaluation` auto-mine hook） |
| **flow/** | `lhgp/flow/` | `longtask/flow.py` | 代码/手写流程图（AST walker / Mermaid / Excalidraw 渲染 / `## flow` wiki 段读取） |
| **adapters/processes** | `lhgp/adapters/processes.py` | `longtask/adapters/processes.py` | 三平台进程探测（win/linux/darwin） |
| **adapters/base+handles+manifest** | `lhgp/adapters/` | `longtask/adapters/` | 执行器协议面 |
| **promoter/escalation+fairness+proposals** | `lhgp/promoter/` | `longtask/promoter/` | 升级阶梯、公平性、提案校验（纯函数） |
| **persistence/decisions+events+errors** | `lhgp/persistence/` | `longtask/persistence/` | 事件词汇、决策记账、错误类型 |
| **persistence/insights+maintenance+timeline** | `lhgp/persistence/` | —（新模块无门面） | brief/board/stats、diff/prune、HTML 时间轴 |
| **rpc/handlers/_common** | `lhgp/rpc/handlers/_common.py` | `longtask/…/_common.py` | actor 派生、Principal 门禁、contract_id 校验 |
| **rpc/transport+server+errors+methods** | `lhgp/rpc/` | `longtask/rpc/` | JSON-RPC 传输与路由 |
| **runtime CLI** | `longtask/cli/` | `lhgp/cli/` | main/tick/runner/daemon_loop/doctor/watch |
| **persistence/store+schema+leases+attempts** | `longtask/persistence/` | `lhgp/persistence/` | SQLite CRUD、迁移、租约 |
| **persistence/projections+context+notifications** | `longtask/persistence/` | `lhgp/persistence/` | 文件投影、上下文编译、通知 outbox |
| **promoter/reconcile+records** | `longtask/promoter/` | `lhgp/promoter/` | 重启恢复四分支 |
| **adapters/subprocess+registry+factory** | `longtask/adapters/` | `lhgp/adapters/` | 子进程适配器、执行器注册表 |
| **mcp_server** | `longtask/mcp_server.py` | `lhgp/mcp_server.py` | 47 个 MCP 工具（含 6 个 P6 评价/学习/portfolio/trace/deadline 工具） |
| **cli/daemon_proc+daemon_loop+tick+runner** | `longtask/cli/` | `lhgp/cli/` | daemon 生命周期与主循环（daemon_loop 内调用 `_enforce_deadlines`） |

**简记**：协议概念（模型/验收/准入/预测/传输）在 `lhgp`；运行时机械
（CLI/store/适配器/daemon）在 `longtask`。新功能如果偏概念 → `lhgp`；
偏运行时 → `longtask`；拿不准 → `lhgp`（因为它是最终目标）。

## 分层依赖（必须保持单向）

```
cli/ ──────► rpc/handlers ──────► persistence/store
  │                │                     │
  ▼                ▼                     ▼
promoter/ ────► contracts/ ◄────── persistence/events
  │                                      │
  ▼                                      ▼
adapters/ ──────────────────────────► persistence/schema
```

- `contracts/` 是零依赖的纯数据层：不 import persistence、不 import cli；
- `persistence/` 只被上层 import，不反向依赖 `promoter/` 或 `adapters/`；
- `adapters/` 可依赖 `persistence/types`，但 persistence 不 import adapters；
- 门面文件（`longtask/lhgp` 互指）允许双向，但**只允许 re-export，不允许
  在门面里写新逻辑**。

## 关键模块职责（一句话）

| 模块 | 做什么 | 不做什么 |
|---|---|---|
| `lhgp/contracts/state_machine` | 合法状态迁移表 | 不做 I/O |
| `lhgp/rpc/handlers/_common` | actor 派生、Principal 门禁、ID 校验 | 不做业务逻辑 |
| `longtask/cli/tick` | daemon tick 主决策（风险→升级→派工） | 不直接 spawn（交给 runner） |
| `longtask/cli/runner` | attempt 生命周期（spawn/poll/finish/stale） | 不做调度决策 |
| `longtask/promoter/reconcile` | daemon 重启后的四分支恢复 | 不派新工（除了通知 verifier） |
| `longtask/adapters/subprocess_adapter` | argv 拉起、observe/collect/cancel | 不做验收判断 |
| `lhgp/acceptance/evaluator` | 运行 typed check 并判定 pass/fail | 不写权威状态 |
| `longtask/mcp_server` | MCP 工具 schema + 参数 → RPC 转发 | 不绕过 handler 直接写库 |

## 新功能检查清单

添加新功能时：

- [ ] 放在正确的树（概念 → lhgp，运行时 → longtask）
- [ ] 不在门面文件里写逻辑
- [ ] 分层依赖保持单向（见上图）
- [ ] MCP 工具有 inputSchema + 描述（if-then 格式）+ annotations
- [ ] 每个公共函数有类型注解（strict mypy 会查）
- [ ] CLI 命令帮助文本 + `--help` 可用
- [ ] 测试放在 `tests/unit/` 或 `tests/integration/`，文件名以 `test_` 开头

## 常见陷阱

1. **门面 import \*** 会把模块级名泄进命名空间——facade 必须显式 `__all__`。
2. **局部 import 遮蔽**：在函数内 `from x import connect` 会遮蔽模块级
   `connect`，导致同文件其他函数 UnboundLocalError——新代码一律用模块级导入。
3. **RPC-RpcError 双类**：`lhgp.rpc.errors.RpcError` 和
   `longtask.rpc.errors.RpcError` 是不同的类（facade 链不合并类对象）。
   跨树 except 时用 `lhgp.rpc.errors` 的那个（canonical）。
4. **frozenset 迭代序不定**：用枚举构建 SQL IN 子句时先 `sorted()`。
5. **CJK E501**：textwrap 按字符数断行对中文失效（宽字符占两列）——
   长中文行手动重排。
