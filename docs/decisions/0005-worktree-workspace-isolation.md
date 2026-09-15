# ADR-005: 用 git worktree 把「同一工作区串行」换成可并行的隔离分区

## 状态

**提议 → 2026-09-11 用户定调：协议层不做专门的工作区隔离设计**。

> 「不需要专门针对工作区隔离进行过多设计，因为目前的 CLI 都本身有
> 各种 worktree 的功能。」

本 ADR 保留作为**问题登记与拒绝方案的记录**，但不再作为待实现的提案推进。
详见下方「2026-09-11 定调」。与 ADR-004 决定 5 的关系：ADR-004 明确「多份合同可共存 ≠ 可安全
并行修改同一工作区」，本 ADR 正是在回答后半句。

## 日期

2026-09-10

## 背景

来源：`openpi-dev/openpi` 的每次调用一个 git worktree 的隔离做法，及其
「未知即保留」的回收纪律。对照本协议的现状：

1. **同一工作区只能串行**。`src/longtask/cli/tick.py` 在发现同一 `workspace_root`
   上已有别的活合同持租约时，记 `dispatch/deferred`（reason
   `workspace occupied by another live contract`）并跳过本轮。这不是隔离，是排队。
2. **`workspace-write` 目前只兑现到 cwd 绑定**。`SubprocessAdapter.prepare()`
   如实返回 `Enforcement.PARTIAL`（DESIGN §9 编译表）：约束被翻译成了「在哪个目录
   启动」，而不是「改动只可能落在哪里」。
3. **两条合同想同时改同一个仓库时，协议没有能表达这件事的词汇**——只能一个做完
   另一个再上，或者用户手工复制一份 checkout 并把它写成两个不同的 `workspace_root`
   （此时协议根本不知道它们是同一个仓库，也就无法阻止两边写同一个文件）。

代价是真实的：长周期合同的排队时间会挤进 Deadline 余量，而 Deadline 恰恰是本协议
的核心资源。

## 决定（提议）

新增**可选**的工作区隔离模式，默认关闭，不影响存量合同：

1. 合同可声明 `execution.isolation: worktree`（枚举，默认 `none`）。声明后，
   适配器在 `prepare` 阶段为该 attempt 建立一个 git worktree：
   - 位置在仓库的 `.git/pi-worktrees` 等价命名空间下（具体路径由实现定，
     必须让 `git worktree list` 可辨认）；
   - 从当前 HEAD 拉分支，**不携带 gitignored 内容**（`.env` 之类不进新 checkout）；
   - 该 attempt 的 `workspace_root` 解析为这个 worktree，`deny_paths` 的归一化
     与比较仍以它为准。
2. 租约的分区判定从「`workspace_root` 相同即互斥」放宽为「`scope_paths` 前缀
   重叠即互斥」。同一仓库的两个 worktree 在**路径上不重叠**，因此可并行；
   一旦两个合同的 `scope_paths` 指向同一子目录，仍然串行（既有
   `_path_prefix_overlap` 逻辑不动）。
3. 收尾（attempt 结算或取消）**不自动合并、不自动 apply、不强制删除**：
   - 已提交的分支保留、checkout 可回收；
   - 有任何未提交改动 / 未跟踪文件 / ignored 文件 / detached HEAD / Git 探测失败
     → **保留整个现场**，只写一份有界交接清单（分支名、HEAD、tracked 改动补丁、
     未跟踪清单），交给用户裁决；
   - 回收只用不带 `--force` 的形式，且只在能证明为空时执行。
4. 崩溃恢复新增一个分支：守护进程重启后发现的孤儿 worktree 一律保留并登记事件，
   绝不静默回收——「状态不明就保留现场」优先于「清理干净」。

## 考虑过且拒绝的方案

### 保持串行，只把排队原因讲清楚

- 优点：零风险，不动调度语义。
- 拒绝原因：并行能力缺失会让长周期合同把排队时间吃进 Deadline 余量；而
  `workspace-write` 的 `enforcement=partial` 会一直是「宣称而非兑现」。

### 直接对主 checkout 做文件级加锁（不建 worktree）

- 优点：不需要 Git 知识，实现最小。
- 拒绝原因：锁的是「谁在写」，仍然要排队；且无法给执行者一个可自由改动的目录，
  `workspace-write` 的语义还是没兑现。

### 让适配器自己决定隔离，协议不表达

- 优点：协议层零改动。
- 拒绝原因：隔离与否直接影响「同一工作区能否并行」这条**调度**语义，藏在适配器里
  等于调度器看不见它，`dispatch/deferred` 的判定会与事实不符。

## 后果

- **覆盖面变宽**：同一仓库上的多份合同可以真正并行，`enforcement` 有望从
  `partial` 升到 `full`（前提是「改动只落在 worktree 内」可被证明）。
- **验收解释面变窄**：`file-exists` / `command-exit-zero` 等 typed check 目前相对
  `workspace_root` 解析；改为 worktree 后，**交付物不在用户的工作目录里**——
  必须同时决定「谁把改动带回主 checkout」（用户手工合并 / 后续合同 / 不考虑）。
  这是本提案最需要用户拍板的一点。
- **回收是新的风险面**：孤儿 worktree 会占磁盘且不是事件、不是状态，只能靠
  reconcile 分支发现。宁可留现场也不能误删用户改动。
- **不是首发条件**：ADR-004 已把「不把复杂能力作为首发条件」写进决定 7，
  本提案排在 Developer Preview 之后更稳妥。

## 待用户裁决的问题

1. 合同里的 `workspace_root` 是继续指向**主 checkout**（worktree 是派生物），
   还是改为指向 worktree（主 checkout 成为「合并目标」）？前者对存量语义更保守，
   后者对验收更直观。
2. 改动如何回到主 checkout：用户手工合并，还是由协议再派一份「合并合同」？
3. 隔离模式是**每合同**声明，还是**每执行器**声明（同一执行器总是隔离）？
4. 是否接受「首版只在 Linux/macOS 生效，Windows 因缺少等价机制而拒接」？
   （Windows 有 `git worktree`，但路径/锁文件行为需要单独验证。）

## 2026-09-11 定调（用户决策）

**协议层不做专门的工作区隔离设计。** 现有的执行器 CLI（codex、agent-cli、
Hermes、qwen 等）本身已经具备各种 worktree 能力，协议再设计一套属于重复
造轮子。

### 后果

1. **ADR-005 不再作为待实现提案推进**。它保留为「问题登记 + 拒绝方案」的
   记录，防止未来有人把同一套设计重新提起来。
2. **档 4 并行加派（PARALLEL）失去主要动机**。档 4 原本依赖分区租约来保证
   并行安全；工作区隔离既然交给执行器，协议层就没有能力也不该承诺并行。
   这与 `fix/parallel-tier-honesty`（commit 3ccbbb4）的诚实化方向完全一致：
   `decide()` 不产出 PARALLEL，停滞一律串行重派。
3. **分区租约机制（`Partition` / `check_partition_compatile`）维持「设计先行、
   未接线」状态**，且现在更清楚地**不应被接线**——除非未来出现协议层必须
   自己隔离的场景（目前没有）。
4. **待裁决的 4 个问题全部关闭**：工作区隔离是执行器的事，协议不管。

### 协议层该管什么

协议层只负责**声明与观察**，不替代执行器做隔离：

- 执行器注册表 `capabilities` 可以（也应当）声明自己是否支持隔离执行
  （例如 `sandbox.isolation: worktree|none`），供 `prepare` 阶段做准入匹配；
- 合同不声明 `allow_parallel` 也能工作——多个合同共享同一仓库时，由各自的
  执行器 CLI 处理隔离，协议只保证串行派工不主动制造冲突（既有
  `_workspace_holder_other_than` 的 workspace 排他已经覆盖最常见情形）。

### 未来若重新打开

只有当出现「协议层必须自己隔离、不能依赖执行器」的硬性场景时，才需要重新
打开本 ADR。目前看不到这样的场景。
