---
name: longtask-contract
description: 教模型通过 LHGP CLI 或 MCP 起草、批准、跟踪和交接远期目标合同。
---

# longtask-contract（DESIGN §17）

`longtask`（协议正式名 LHGP）远期任务协议的契约 skill——**教会 AI 怎么用这个工具**。

P6 采用双轨入口：优先使用 `lhgp` / `lhgp-mcp`，现有 `longtask` /
`longtask-mcp` 仍是兼容别名；两者写入同一份合同与审计账本。

这不是协议本身（见 `DESIGN.md`），是模型侧的接入文档：教你怎么
通过 CLI / RPC 跟协议对话，谈判什么样的合同、写什么样的交接、什么时候
该把工作切给远期 vs 自己当场干。读完之后你应该能立即起草一个 `longtask
prepare` 调用，并把任务交出去。

## 1. 这个工具解决什么问题

每个 AI 都会遇到这类用户：

- "把第三卷的力量体系对照表整理出来，deadline 下周五"
- "用 Codex 也跑一遍，交叉验证"
- "今晚别跑，明天提醒我再决定"

`longtask` 是**专门承接这种「跨会话、跨 agent、跨今天」任务**的协议：

- **跨会话**：任务在 `state.db`，不依赖任何 AI 进程；A 会话死了 B 接着干
- **跨 agent**：你（这个模型）不擅长写测试 → 协议派给 Codex；Codex 干
  完不擅长验证 → 协议派给其他 CLI 当 verifier
- **跨今天**：现在是凌晨三点，deadline 在十二小时后——协议
  `wakeup/rtc-armed` 唤醒自己，到点派工

运行中如需查看提醒历史，使用只读 `lhgp notifications --goal-id <id>`，
默认不会把通知 payload 带回对话；只有用户明确要求审计详情时才追加
`--include-payload`。

使用 `get_contract` 跟踪时，优先读取响应中的 `deadline_snapshot`：其中的
`forecast_p50/p90`、`slack`、`risk`、`confidence` 与 `next_decision_at`
是协议当前风险判断的依据；快照用于决策和审计，不构成严格按时完成承诺。

所有会改变状态的 MCP 工具都支持 `request_id`。网络或模型重试时必须复用
同一个 `request_id`；若省略，MCP 会按方法和参数生成稳定请求键。只读查询
无需依赖该键。

**对比**：

| 形态 | 你当场干 | cron / 自动化 | 远期任务协议 |
|---|---|---|---|
| 跨会话 | ❌ 会话死就丢 | ✅ 但只能跑固定命令 | ✅ |
| 跨 agent | ❌ | ❌ 一般一对一 | ✅ 执行器池化 |
| 灵活时机/抉择 | ✅ 你在想 | ❌ 时刻表驱动 | ✅ 紧迫度+deadline+升级阶梯 |
| 接受方可知 | ❌ 在你上下文里 | ❌ 在 cron 配置文件 | ✅ 交接文件、上下文快照 |

当一个任务**不急于现在完成**、可能跨多次会话、或可能用不同模型接力时，
适合用 `longtask`。

## 2. 三个层级：什么时候用什么

按用户的"急不急"+ "谁来干"决定走哪条路径：

| 用户说法 | 路径 | 你的动作 |
|---|---|---|
| "我等你给我看看" | **当场干** | 直接答，不用 `longtask` |
| "等明天早上再给我看" / "等 Codex 跑完再继续" | **远期任务** | `longtask prepare` 立合同 |
| "下周五前完成" | **远期任务**（默认） | `longtask prepare`，deadline 写那会儿 |
| "今晚别跑" + 半夜 deadline | **远期任务 + L1 唤醒** | `longtask prepare`，合同 context.required=true 触发 L1 注册 |

**判断要点**：如果任务能"在我这次会话里答完"，不要立合同。立合同是
**承认任务超出了我这次会话的边界**（时间、工具、专长）。

## 3. 跟协议对话：CLI 子集

`longtask` 装好后最常用的子命令：

```bash
# 1. 立远期合同（草稿状态）
longtask prepare \
  --contract-id lt-20260903-001 \
  --title "整理力量体系对照表" \
  --objective "产出第三卷全部登场角色的力量体系对照表..." \
  --deadline 2026-09-12T18:00:00+08:00

# 2. 批准（从 drafted 转到 active，协议开始调度）
longtask approve lt-20260903-001

# 3. 查看状态
longtask get lt-20260903-001
longtask list

# 4. 修改可修订区（soft_guidance、acceptance、workload；冻结区不可改）
longtask patch lt-20260903-001 --revision N --guidance '{"note":"..."}'
# --revision 必填（CAS）；--workload-hours 可改工作量；冻结区字段 patch 拒接

# 5. 暂停/恢复/取消
longtask pause lt-20260903-001
longtask resume lt-20260903-001
longtask cancel lt-20260903-001

# 6. 修完成不了的事：转给人裁
longtask arbitrate lt-20260903-001 --decision archived --note "作废交人裁"
# 合法 decision：complete / archived / active
```

在 MCP 中，建议先调用只读 `lhgp_doctor`（兼容名
`longtask_doctor`）检查已启用 CLI；诊断失败时先修复本机环境或调整
registry，再批准合同，避免把不可启动的 executor 计入长期任务预算。

当执行预算耗尽但交付物可能已经就绪时，不要重新起草合同；直接请求一次
“验收现状”。该命令只派 verifier，不会再派 executor：

```bash
longtask request-verification lt-20260903-001 \
  --reason "执行预算已耗尽，请核对当前工作区是否满足验收"
```

在 MCP 中使用 `lhgp_request_verification`（兼容名
`longtask_request_verification`）。请求会先写入 `verification/requested`
事件，再由 daemon 在下一次 tick 幂等地派生独立 verifier。终态合同、已有
verifier 进行中、或验证预算也已耗尽时，协议会拒接并说明下一步；`blocked`
会在成功请求后恢复为 `active`，原升级历史仍保留。默认每份新合同保留两次
verifier 机会；若用户明确设置 `verification_attempts_reserved`，以显式值为准。

完整子命令：`longtask --help`。

`longtask get <contract_id>` 返回合同快照、隔离的 `decision_history` 与
`attempt_history`。需要控制上下文大小时，可传 `--decision-limit N` 和
`--attempt-limit N`；MCP 的 `get_contract` 使用同名参数。

返回值还包含最近的 `verification_history`（最多 20 条）。验收请求后应按
`requested → consumed → started` 顺序判断进度：只有 `started` 才代表 verifier
已实际派发；若出现 `consumed` 但没有 `started`，应读取同一合同的事件和升级记录，
按拒接原因决定是否修订预算、调整候选或交给用户仲裁。

## 4. 起草一份好合同（最重要的一节）

**决定这四点就够了**，其余字段都有合理默认：

### 4.1 objective：写验收，不是写方法

`objective` 是冻结区（`contract` 直引，不可改），写完就是合约。所以：

- ❌ "用 Python 写一个工具" —— 这是方法，不是完成标准
- ✅ "在 runtime/workspace-x/ 下产出 palindrome.py 和 tests.py；is_palindrome
  忽略大小写、空格、标点；`python -m unittest tests` 全部通过" —— 这是结果

写的时候想象："交付完，**谁、用什么方式**能在 5 分钟内判定 pass 或 fail？"

### 4.2 acceptance.checks：逐条可核对

把 standard 拆成**可独立判定**的子项。每条写得让一个陌生人都能照着核对：

- ❌ "实现正确" —— 怎么算正确？
- ✅ "workspace 根目录存在 palindrome.py" —— 跑 `ls` 就能判
- ✅ "is_palindrome('A man, a plan, a canal: Panama!') == True" —— 直接 `python -c` 验
- ✅ "`python -m unittest tests` 在工作区内执行全部通过" —— 跑命令验

DESIGN §5.2 强调"自己考自己不算数"——协议会派一个 verifier 独立核对，
你的 checks 要让 verifier 能照着判（不要写"看起来对"）。

**两条硬约定（模型第一次用就踩过的坑，来自真实运行记录）**：

1. **typed check 的 `target` 相对 `workspace_root` 解析**。合同声明了
   `workspace_root=/x/ws`，那么 `file-exists:charfreq.py` 指向
   `/x/ws/charfreq.py`。写成 `file-exists:ws/charfreq.py` 会找
   `/x/ws/ws/charfreq.py`（双层前缀，不存在）。
2. **command check 在守护进程环境执行**（`shell=False`、cwd 为
   workspace_root、PATH 继承 daemon）——daemon 的 PATH 通常**没有**
   项目虚拟环境。命令里引用解释器要么写绝对路径，要么接受协议侧
   `undetermined`、由 verifier 的判定块填补裁决（SPEC §12.4）。

### 4.3 hard_constraints：声明而非禁止

typed `acceptance.checks[].kind` 使用线协议枚举：`file-exists`、
`file-content-matches`、`command-exit-zero`、`artifact-present`、
`structure-valid`、`observable`、`user-assertion`。设计文档中的抽象分类名
（如 `artifact_exists`、`command`、`schema`）不能直接作为 kind。

`hard_constraints` 是适配器**翻译前的声明**。翻译不了 = 拒接。所以写
**可被适配器翻译的能力**：

- ✅ `file_effects.mode: workspace-write`（适配器能绑定 cwd）
- ✅ `file_effects.workspace_root: <绝对路径>`（必填，否则拒接）
- ✅ `network.mode: deny`（只有声明独立网络策略的适配器能接）
- ❌ "禁止写到 ~/ref/"（这是 deny_paths 里写，**目前**适配器只校验不会强制
  落实，靠执行器自己的沙箱——明确告知用户）

### 4.4 deadline 与工作量

- `deadline_at`：写 ISO 8601 字符串（含时区）。越早紧急程度越高，但太短
  可能没做完——你给的是真实预期，不是乐观估计
- `workload_initial_hours`：**如实**填。协议用 `workload/time_left` 算紧迫度
  u。u ≥ 1.0 才会触发 RESPAWN 派工。**你低估工作量 = 协议以为不急 = 拖到
  deadline 才动**——这是 v1/v2 真实运行踩过的坑
- `budget.max_dispatches`：默认值 5 已经够绝大多数任务；想留更多尝试机会
  就调大，触顶自动转 `blocked(need-user)` 不假装能完成
- `command-exit-zero` typed check 可在 `args.timeout_seconds` 指定更短的命令
  上限；运行时还会按合同剩余 Deadline 收紧。超时只表示
  `undetermined`，应由 verifier 判定块或用户仲裁补充证据。

## 5. 你的工作流

```text
用户模糊需求
   ↓ 和用户谈判（DESIGN §17 的核心）
拆成 objective + acceptance.checks + constraints
   ↓ 写合同（longtask prepare）
冻结区钉死，提交即生效
   ↓ 立即批准（longtask approve）
u >= 1.0 时协议主动派工
   ↓ 跟踪（longtask status / get）
verifier 独立核对
   ↓ 全部 pass → 合同 complete
   ↓ 有 fail → 退回 active，下轮再派
```

## 6. 跨 attempt 续跑：交接文件是黄金通道

你或别的 agent 干到一半，handover.md 是下一个会话的**唯一**起点。它至少
要有：

```yaml
current_stage: implement-tests       # 当前阶段
source_attempt_id: att-20260901-001  # 上一个 attempt id
remaining:                            # 没干完的事，逐条
  - 修复 tests.py 错误断言
  - 加 empty string 用例
next_action: 改 assertFalse          # 下一步第一件该做的事
estimate_remaining_hours: 0.2        # 剩余工时
```

v2 实现的"交接附言融 task_prompt"是协议自动把 `next_action` 加到下一 attempt
的任务文本——你只要写好 handover，协议替你交接。

## 7. 你作为执行者被协议唤起时

会收到一个 `task_prompt` 字符串 + 你的工作区。task_prompt 包含：

- 合同 `objective`（必含）
- **交接附言**（如果上一 attempt 留下 handover.md，自动追加 `next_action`
  和 `remaining`）
- 工作区根目录的 `context/attempts/<your-attempt-id>/active.md` 快照
  （合同锚点 + 交接 + 最近 attempt 终态）

**你该做的**：

1. 先读 `active.md` 确认范围（特别是 acceptance.checks）
2. 在你的工作区（cwd 或 task_prompt 里的 `workspace_root`）写代码/产出
3. 完成时通过协议的方法 `attempt/write-back` 报告 attempt_state=succeeded
   （带 `progress_note` 简述做了什么）；或 failed（带失败原因）
4. **不要自作主张调 `longtask complete` 或 `arbitrate`**——协议会派 verifier
   独立核对，verifier 报告 succeeded 后协议自动 complete

如果你的进程会长期存活且与协议有网络通道：

- 心跳：`lease/renew` 每 5-15 分钟一次
- 进度更新：`attempt/write-back` `progress_note="..."`（落 `context/scratch-updated` 事件）
- 真正出错：写 `attempt_state=failed reason="..."` 让协议重新派

## 8. 别做这些

- ❌ 觉得"任务小/用户在场"就**不**立合同——用户断网就丢一切
- ❌ 把 objective 写成"用 Python 实现"——这是方法不是结果
- ❌ acceptance.checks 写"看起来对"——verifier 判不了
- ❌ 不写 handover 就退出——下一个会话是素人
- ❌ 自己报 succeeded 后再自己改代码自验——§5.2 自己考自己不算数

## 9. 故障速查

| 现象 | 排查 |
|---|---|
| 合同准备了但永不派工 | 算 u = workload/time_left；u<1.0 不派。提高 workload 或缩短 deadline |
| 派了一轮就停了 | 查 budget.attempt/started 数：超 max_dispatches 自动 blocked(need-user) |
| 跨进程轮询丢句柄 | daemon 续命依赖持有 Popen；headless harness 的已知缺口（内部运行记录） |
| 执行者 succeeded 不被识别 | agent-cli node 主进程是 Popen 持有，但内部 worker turn/end 才会让主进程退——需要总控补录 attempt/succeeded 或 harness 在 stdout 输出结构化 attempt/finished 事件 |

## 10. 相关文档

- `DESIGN.md`（协议规范本体，唯一权威）
- `README.md`（仓库入口）
- `CONTRIBUTING.md`（代码侧纪律）
（真实运行对比记录在预览期内为私有归档）
- `quality/claims.json`（治理真相源：哪些设计已通过测试验证）
