# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); version numbers
follow [SemVer](https://semver.org/spec/v2.0.0.html); dates in ISO 8601.

## [0.1.0a14] - 2026-09-11

本版把 a13 之后积累的十一轮增量（即下方 `[Unreleased·二]`…`[Unreleased·十一]` 各节）
与**一轮完整外部审计的整改**合并交付。审计覆盖五个面——CLI 与守护进程、持久化
存储、RPC 双命名空间、文档承诺与制品——逐条按「先实测、再分类、然后修复或去广告」
处置：凡修复都先做反向验证（测试先红后绿），凡结论都以可复现的测量为据。

两个数字：七道门 7/7 全绿，`1644 passed, 11 skipped`，覆盖率 79.9%（下限 78%）。
公开交付此前停在 a6（Releases 页），本版把它接回源码当前状态。

### Fixed

- **合同状态机在唯一写入口强制（B1）**：`LEGAL_TRANSITIONS` 一直存在，RPC handler
  也一直在守它（approve/pause/resume/cancel/arbitrate 五处），但
  `update_contract_state` 自己不校验——同一个非法转移「走 RPC 被拒、走守护进程或直调
  却能落库」，审计记录与实际状态可以互相矛盾。现在非法写入抛
  `IllegalStateTransitionError`（写库之前，state/revision 都不动），RPC 边界映射为
  `STATE_FORBIDDEN`。**自反写入（X→X）放行**：tick 的 `_judge_verifier_outcomes` 在
  验收失败时写 `new_state=ACTIVE` 而合同本就是 active，它要做的是把
  `acceptance_status` 置 FAILED。实测依据：全量测试埋点记录了 src 侧 13 种真实转移
  （全部合法或自反），而 15 次非法转移全部来自测试绕过 handler 走捷径，已逐个改为
  合法路径。
- **损坏行在读路径上自解释地失败（B5）**：`contract/get`、`goal/prepare`、
  `goal/admission-check` 三个 handler 完全没有 store 异常映射，损坏行会以
  `json.JSONDecodeError`/`KeyError` 这类**不属于 `StoreError` 层级**的裸异常穿过 RPC
  边界；且每个 handler 各写一份映射（本包内三种写法），漏写即漏报。改为类型化
  （`StoreTamperedError`）+ 边界统一映射（`STORE_TAMPERED`，`RETRYABLE=False`）。
- **`patch_contract` 的修订快照不再丢字段（B6）**：它用手写枚举重建 `ContractDraft`，
  15 个字段漏了 1 个带 `default_factory` 的 `auto_approve`——不报错、不为空，而是
  静默填入安全基线，于是每个被 patch 的修订都把「已授予自动批准」记成「未授予」，
  而活行仍是真实授权（审计记录里一次静默的授权降级）。改用
  `dataclasses.replace`，此类漂移结构上不可能再发生。
- **attempt 生命周期事件必须带真实角色（B9/B9b）**：`_fail_attempt`/`_mark_stale`/
  取消路径与 SPEC §12.4 **通道 1**（会话型 harness 的 `attempt/write-back`）写入的
  事件 `role` 列曾是 `None`/`'model'`，而 `events.role` 的值域是
  executor/verifier/daemon/user/promoter/scheduler/system。后果：核验器成功的事件
  在 tick 裁决里**一条都看不到**（实测修复前 0 条、修复后 1 条），合同因此无法完成。
- **执行器并发额度不再被终态 attempt 永久占用（B4）**：容量记账手写
  `('admitted','running','orphaned')`——把终态 `orphaned` 算成在跑（一条失联 attempt
  永久占死额度），又漏掉 `starting`/`waiting`（额度被超额放行）。改为从
  `AttemptState` 推导在飞集合。
- **消除 CAPACITY_FULL 热转（B3）**：容量饱和时把 `next_decision_at` 置 `now`，与
  「blocked 的过期决策点原样返回」相乘 → 守护进程每轮算出 `until=0` → 不睡 → CPU
  空转。改为置 NULL，重试节奏交回心跳。
- **`request_id` 跨合同归属守卫补齐到 canonical 侧**、**分发表漂移与 canonical 门面
  缺导出**、**wheel 补齐 flowgen skill**、**operator profile 的重复/过期工具条目**、
  **拒接的验收请求不再被误标为已兑现**、**stdout 协议通道与子进程编码入规范并钉住**。

### Added

- **`evidence` 验收证据独立表（SPEC §13.1，schema v6）**、**attempt 消耗台账
  （usage 自报 + schema v5 + `stats` 聚合）**、**成本预算线 `budget.max_cost`**、
  **`budget`/`deadline` 轴的强制面**、**MCP 工具面 profile（按角色收窄 + 越界拒调）**、
  **R4a 可复制的无模型本地示例**（`examples/local-no-model/`，doctor→…→satisfied 全链路）、
  **ADR-005**（协议层不做专门的工作区隔离设计）。
- **回归测试**（本版净增 90 条，`1554 → 1644 passed`）：状态机 9×9 全枚举乘积上断言
  「守卫判定 == 表 + 自反规则」而不是手写字面清单；损坏行 9 种形态的读路径与边界映射；
  修订快照与活合同逐字段一致（覆盖 `dataclasses.fields` 全部字段）；`events.role`
  值域与两条判定通道；门面覆盖与真身方向；行尾策略。

### Documentation

- **去广告两处未交付能力**（原条目保留 + 追加状态，不静默删除）：
  - **E2 饥饿保护（B8）**：`fairness_states` 恒为空、`observe_tick` 只在测试里被调用，
    「连续 5 tick 未派工自动提前」在生产里从不触发；DESIGN §8.3 承诺的是另一套语义
    （额度不足 + u ≥ 1.0 → `blocked(need-user)`，默认 10 轮），阈值与动作都不一致。
    a6 的 CHANGELOG 曾宣称已交付，就地更正。
  - **7 个只有名字的 RPC 方法（C4）**：`context/refresh`、`context/promote`、
    `control/notify`、`control/followup`、`control/steer`、`control/spawn`、
    `lease/release` 在枚举里有名字但两侧分发表都没有 handler（调用返回
    `STATE_FORBIDDEN: method not implemented`）。DESIGN §3/§7/§11.2/时序 A/§15 与
    SPEC §14.2 逐处标注「预留未实现」，claims 记 `accepted_debt`。
- **修正三份方向性文档**：ARCHITECTURE 的「真身位置地图」`rpc/handlers` 方向按实测
  重写（B2 的根因就是地图记反，导致安全守卫只加在一侧）、canonical 命名空间与入口
  shim 的方向表述、CONTRIBUTING 与地图对齐。
- **更正安全加固证据里已被实测推翻的判断**（两个 `RpcError` 类曾被记为不同类，实测
  是同一个类对象），并更新其 deferred 清单状态。
- **`.gitattributes` 声明行尾策略**：仓库内存 LF、任何平台检出 LF（此前由各机器
  `core.autocrlf` 决定，每次 `git add` 都刷警告）；工作树 346 个 CRLF 文件按字节
  归一（与索引 blob 逐字节相同，renormalize 暂存内容改动为 0）。

### Notes

- 本版**不含**任何线协议、schema 语义或错误码的破坏性变更（`PROTOCOL_VERSION` 仍为 1）；
  schema 迁移 v5/v6 由 `ensure_schema` 幂等补齐。
- 审计整改的分类原则：A 类（真缺陷）修，B 类（设计如此/不适用）不修并说明，
  C 类（可能有副作用）不动。被实测推翻的审计假设（源码编码「损坏」、README 快速上手
  「不可用」、插件版本「不一致」、两个 `RpcError`「不同类」）逐条记录为**不成立**，
  而不是照单修改代码。

## [Unreleased]

> **当前没有未发布变更。** 本节与下方 `[Unreleased·二]`…`[Unreleased·十一]` 各节的
> 内容已随 **0.1.0a14**（2026-09-11）交付；逐轮记录按原样保留以便追溯，不再作为
> 未发布项。`[Unreleased·N]` 是分轮工作日志，`[0.1.0aN]` 才是发布记录。

吸收外部同行的几处工程做法，落点都在**已有保证**上——不新增协议语义，
线协议、schema、错误码不变（DESIGN v0.8，把 §14.1 两条威胁从「宣称」升级为
「可验证的结构」）。来源：`openpi-dev/openpi`（Pi 编辑器的社区扩展包）的终端输出
清洗、不可信数据的显式边界、进程树终止、漂移守护测试四条做法。

### 更正

- **7 个 RPC 方法只有名字（2026-09-11 审计 C4）**：`context/refresh`、
  `context/promote`、`control/notify`、`control/followup`、`control/steer`、
  `control/spawn`、`lease/release` 在 `Method` 枚举与 `IDEMPOTENT_METHODS` 里声明，
  但两侧 `HANDLERS` 表都没有 handler——调用 fail-closed 返回
  `STATE_FORBIDDEN: method not implemented`（运行时说的是真话）。文档此前把它们与
  已实现方法并列，现逐处标注「预留未实现」：DESIGN §3/§7/§11.2/时序 A/§15 与
  SPEC §14.2。缺口清单钉在
  `tests/unit/test_rpc_dispatch_tables.py::KNOWN_UNIMPLEMENTED`，claims 记
  `rpc-advertised-but-unimplemented-methods`（accepted_debt）。
- **E2 饥饿保护未交付（2026-09-11 审计）**：`v0.1.0a6` 的 CHANGELOG 宣称「饥饿检测
  （连续 5 tick 未派工自动提前）」，实测该规则**从未在生产中触发**——tick 传给
  `apply_fairness_order` 的 `fairness_states` 恒为空 dict，`observe_tick` 只在测试里
  被调用。公平性只有容量记账半是接线的（且 soft cap 默认 0 = 不限，需运营显式配置
  才有约束力）。原条目已就地更正，`fairness.py`/`tick.py` 加接线状态说明，
  `docs/RELEASE-PLAN.md` §5 标注 E2 待独立 SPEC 与测试，DESIGN §8.3 的饥饿保护
  同样标注未实现。

### Added

- **`lhgp.untrusted`（新包，零第三方依赖）**：`sanitize.py` 做结构性清洗，
  `boundary.py` 造可验证注入围栏（DESIGN §14.2）。
  - 清洗落在适配器**唯一的输出出口** `_MonitoredProcess.stdout_text()/stderr_text()`：
    CSI/OSC/DCS/SOS/PM/APC 与两字节转义、C0/C1/DEL、除 ZWNJ/ZWJ 外的 Unicode `Cf`
    全部剥离，保留 `\t`/`\n`/`\r`，且**幂等**。逐个消费方清洗必然漂移成几份口径。
  - 围栏 token 由正文摘要派生（`LHGP-UNTRUSTED-<sha256 前 12 位>`），正文里形似
    围栏的前缀被改写成不可能匹配的形态——「围栏内 = 全部正文」由构造保证。
- **`resume.py` / 上下文快照的 handover 段改走围栏注入**：`handover.md` 是上一轮
  执行者写的文件，原先在续跑 brief 里**逐字注入且无标注**，在快照里以裸
  `## 交接` 段落出现（版式上等于告诉下一个执行者「这段与协议同源」）。
  同一个文件里 `handover_prompt_addendum` 早已标了「不可信」——两处口径不一致，
  现在统一到围栏。快照用 terse 声明：它有 `max_bytes` 容量合同（超限直接拒接
  启动 attempt），每个 attempt 都要付这份字节。
- **`processes.terminate_tree()`**（DESIGN §14.3）：POSIX 走 `killpg`，Windows 走
  `taskkill /T`。`SubprocessAdapter.cancel()` 与重绑进程的 `terminate()` 都改用它。
- **`tests/integration/test_process_tree_termination.py`**：真起两级进程（父→孙），
  断言孙进程确实死了。判定用「pid + 启动时间」双重比对而非裸 pid——Windows 的
  pid 会被复用，只比对 pid 会把「已死、pid 被别人接管」误判成还活着。
- **`tests/unit/test_mcp_tool_surface.py::TestToolAnnotationDrift`**：4 条守护，
  钉住注解分类的完备性与单一来源。

### Changed

- **MCP 工具注解收敛到单一真相源**。原先 7 个工具在自己的 schema 里内联写
  `annotations`，加上两个集合，一共三处可写、谁也不知道谁赢。实际后果：
  - `lhgp_prepare_goal`（立合同）、`lhgp_propose_plan`、`lhgp_send_message`
    （落消息并进入下个 attempt 上下文）被声明为 `readOnlyHint=False` +
    `destructiveHint=False`——对宿主来说这是「既不只读也不破坏」，**不会请求
    人工确认**，模型可以直接改掉持久承诺；
  - `lhgp_resume_attempt` 与 `_DESTRUCTIVE_TOOLS` **互相矛盾**：集合说 destructive，
    内联说不是。内联赢，于是集合里那条是死代码；
  - `longtask_user_confirm_spec_verdict` 谁都没写——它是 CANDIDATE 合同唯一的
    签字出口，却以中立注解示人。
  现在内联声明全部删除，两个集合是唯一真相源，派生改用**直接赋值**（不是
  `setdefault`），内联声明再也覆盖不了它。分类结果 27 只读 + 24 destructive = 51，
  零中立、零交集、零幽灵条目。
- **`SubprocessAdapter.spawn()` 在 POSIX 传 `start_new_session=True`**：子进程自成
  会话/进程组组长，取消时一次信号覆盖整组。Windows 的拉起参数与既有形态保持一致
  （该参数在 Windows 被忽略，因此显式只在 POSIX 传）。
- **`DESIGN.md` v0.8**：§14.1 威胁表新增/改写三行，新增 §14.2（不可信外部文本）、
  §14.3（取消以进程树为单位），保证清单补两条，术语表补三条，§19 记审批行。

### Fixed

- **取消只杀直接子进程，harness 的 worker 成孤儿继续写工作区**。已复现：起两级
  进程后调用旧的单进程终止，父进程已停而孙进程仍存活——对合同来说这是「已取消
  却还在改盘」，没有事件、没有报错，只有盘上多出来的改动。新集成测试能抓住它
  （旧行为下该测试变红）。
- **ANSI 着色序列插进 `lhgp-verdict` 围栏会让有效证据退化成「无证据」**。
  `parse_verdict_block` 拿到的若是未清洗原文，`\x1b[32m` 夹在围栏与标记之间即
  匹配失败 → 返回 `None`（协议规定「无证据」）。清洗后同一段输出重新可解析，
  由 `test_sanitize_recovers_verdict_fence_that_ansi_wrapped` 锁定。
- **`safe_process_group()` 拒绝在任何不确定的情况下 `killpg`**：只有
  `getpgid(pid) == pid`（进程自己是组长）才返回组号。否则组号可能指向**推动者
  自己所在的组**，killpg 会把守护进程连坐杀掉——这是本模块最不能出的错，
  判定条件因此取「我是组长」而不是「和我不在同一组」。

### 第二轮：四层审查（类型 / 死代码 / 边界 / 真跑）

按 `docs/wiki/playbook/four-layer-review.md` 审了上一轮的改动，本轮修的是审查
查出来的东西，不是想出来的东西。

#### Fixed

- **四个标识符校验器全部被尾随换行穿过**。Python 的 `$` 也匹配「尾随换行之前」的
  位置，所以 `re.match("^ab$", "ab" + chr(10))` **是匹配的**：`lhgp.untrusted` 的围栏
  label、`resume` 的路径分量、`rpc/handlers/_common` 的 contract_id（两棵树各一份）、
  `scheduler/wakeup` 的 task_id 都会接受带尾随换行的值。其中 label 那条是自己写的，
  且 docstring 明写「含换行的标签本身就能伪造一行新的围栏，因此这里 fail-closed」——
  **行为与自称的保证不符**，这是本轮最该修的一条。全部改为 `\Z`（纯收紧，只拒掉
  尾随换行，正常值不受影响）。`wakeup` 那条实际用 `fullmatch`，本来就免疫，
  一并统一。
- **`output_sanitized` 可观察性缺口**：清洗会改写证据的字节，但 collect 结果里
  看不见。现在与 `output_truncated` 并列上报——审计必须能区分「执行者的原始输出」
  与「协议改写过的输出」，否则「不可信文本一定经过清洗」这条保证没有可观察面。

#### Added

- **`tests/unit/test_identifier_anchors.py`**：把 `$` 这一类锚点缺陷按校验点逐条钉住
  （label / 路径分量 / contract_id 两棵树 / 点段），并锁定「收紧之后正常值仍被接受」。
- **`tests/conformance/test_adapter_scenarios.py` 新增 3 条**：清洗旗子为真、为假
  （恒为 True 的旗子是假信号，比没有更糟）、输出截断旗子为真且保留字节不超预算。
  顺带补上一个从生产出来就没被任何测试断言过的旗子（`output_truncated`）。
- **`docs/wiki/playbook/four-layer-review.md`**：四层审查的可复用方法，含各层的
  判定词汇、本仓库的已知形状（façade 星号导入使该层对类型与死代码检测为黑箱），
  以及第 4 层的反向验证判据（「失败测试名必须等于预期名」）。

#### 审查发现但未修（需你裁决）

- **整套分区租约机制没有生产入口**：`Partition` / `check_partition_compatible` /
  `scope_paths` / `scope_stages` 只有测试在构造与调用；`lease/partition-conflict`
  事件从未被发出，`PARTITION_CONFLICT` 错误码从未被抛出，`partition_id` 在所有
  派工调用点都取默认值。而 `UrgencyTier.PARALLEL` 是可达的：它消耗一份
  `max_escalations`，事件流里写「parallel dispatch (§6.2/§7.1)」，但 tick 与
  RESPAWN 共用同一分支、只派一个重置 attempt。**即「并行加派」的行为等于重派，
  而租约登记说它是并行。** 让并行安全的那套机制（分区互斥）没有任何入口。
  注：`decide()` 在租约活着时封顶到 REMIND，所以这不会造成并发写；问题在
  「声明与行为不符 + 事件记录不实」，属 R3b（多合同隔离与接力边界）范围。
  修法二选一：把 PARALLEL 的行为与措辞都降为「串行重派」，或实现分区分配。
  两者都改协议语义，按 CONTRIBUTING 需先过 DESIGN 审批。

### 说明（未吸收的部分）

`openpi` 的产物提交回执（原子发布 + 每产物 SHA-256 + `predecessorSha256`）**没有**
吸收：它解决的问题是「journal 与 result 是唯一副本，崩溃在多次原子替换之间会留下
混合集」，而本协议的文件投影在 DESIGN §3.1 下**可从事件表完整重建**（投影「可落后
可重建，不可超前」），`_atomic_write` 已给出单文件原子性。缺的是「重建」这一动作的
触发，不是「回执」。不属于本轮范围，记在此处备查而非静默略过。

## [Unreleased·十一] 档 4 并行加派诚实化：调度器不再在决策历史里说谎

四层审查（第 2 层死代码）在轮二就发现了这个缺口，一直挂到本轮——因为它是
唯一一条「**系统在说谎**」的问题，优先于其它一切。

### 缺口（实证）

`decide()` 在估算停滞且可加派时产出 `UrgencyTier.PARALLEL`，并把
"parallel dispatch (§6.2/§7.1)" 写进决策理由落库；而 tick 里
`case RESPAWN | PARALLEL` 共用同一分支，只派一个重试 attempt——**行为与档 3
完全相同**。同时：

- 分区租约机制（`Partition` / `check_partition_compatible` / `scope_paths` /
  `scope_stages`）只有纯函数与单测，**没有任何生产调用点**；
- `partition_id` 在所有派工调用点都取默认空值；
- `lease/partition-conflict` 事件与 `PARTITION_CONFLICT` 错误码从未被写出；
- 旧实现还为这"额外的并行"扣了一次 `max_escalations`。

即：**档位名、决策历史、预算记账三处都在声称一件没发生的事。**

### Changed

- `decide()` 不再产出 PARALLEL：估算停滞一律如实返回 RESPAWN，理由自陈
  「serial respawn (partitioned parallel dispatch is not implemented; 
  partitions_requested=…, escalations_left=…)」——被请求的并行意图仍留在
  审计里（不隐藏），但不再假装已执行。
- 不再消耗 `max_escalations`（没做额外的事，就不收额外的预算）。
- 四处代码注释标注「未接线」：`urgency.py`（枚举值保留的理由）、`lease.py`
  （模块级接线状态）、`events.py`（两个预留事件类型）、`tick.py`（case 兼容
  历史数据重放）。
- 文档声称对齐实现：DESIGN §6.2 六档表、§6.3 阈值表、§7 租约条目、§7.1 标题
  改为「设计已定，尚未接线」并明令「不得描述为已有能力」；§11.7 错误码、§16
  非目标同步标注；SPEC 的 red 档建议去掉"分区并行"、`allow_parallel` 标注
  可声明但不产生并行行为。

### Tests

- 改写 `TestParallel` → `TestStalledRespawnIsHonest`（档位/理由/预算三项如实）；
- 新增 `TestParallelTierIsUnreachable`：**穷举决策输入空间**（tier × 租约 ×
  停滞 × 可分区 × 两种预算 × 多个取值，>100 组）断言 PARALLEL 不可达——
  比手挑用例更硬，也锁住「未来有人无意恢复那条分支」。
- 行为不变性：停滞场景的**动作**与修前一致（仍是串行重派），改的只是记录与
  记账；既有 1549 条测试仅 5 条 escalation 断言随契约更新。

### 未做（等裁决）

档 4 的**实现**（分区分配 + 工作区隔离）与工作区级的 `git worktree` 隔离是
同一件事的两面，统一在 ADR-005 的 4 个待裁决问题里。
## [Unreleased·十] 指令注入面的截断纪律（第七轮同款，补上漏掉的那一处）

第七轮修了 handover 附言的截断，但注入面清单里还有一处同类裸切片：
**用户指令**（`context.py` 的 directive 段落）用 `text[:240]` 截断后直接进
active.md → 进提示词——截断后看起来仍是一条完整指令，模型会把半句话当全部
执行，且看不出后面还有内容。

### Changed

- `truncate_at_line_boundary` 泛化：标记改为参数（默认「超出长度预算已截断」），
  两个调用点各自传指向全文位置的标记（handover.md / 消息层）——不复用对方文案。
- 指令注入改走该函数（行界 + 显式标记，标记计入 240 字符预算）。

### Tests

- 新增/改写 3 条：**驱动生产路径**（send_message → compile_context_snapshot，
  断言快照正文含标记且长度受控）、短指令不动、两种标记文案不互相复用。
- 反向验证记了一条教训：第一版用例只调助手函数，把生产调用点退回裸切片时
  **照样全绿**——测试必须打在接线上，改驱动真实路径后才红。

## [Unreleased·九] 完成事件行的凭据加固（自报与凭据自报可区分）

第八轮的容错改动让我回头审视了一个既有薄弱面：`attempt/finished` 事件行
本质是**自报**的完成声明——任何打印出该行的输出都会被采信，包括 harness
把文档里的示例回显出来（而容错又略微放宽了触发面）。不能靠改窄识别来修
（那会把合规 harness 一起误伤），要靠**让能力差异可观察**。

### Added

- 事件行 MAY 携带 `session_token`（spawn 时已注入子进程环境的 per-attempt
  凭据）：匹配 → `completion_attested=True`；不携带 → `False`（仍采信，
  存量 harness 零影响）；不匹配或本 attempt 未注入却携带 → **拒绝该行**。
- 标记随 observe/collect 结果与 `attempt/succeeded` 事件 payload 一并落库，
  审计可区分「谁有能力声明完成」。
- 示例 `worker.py` 改为推荐形态（回带 token），集成测试断言全链路
  `completion_attested=True`。

### Tests

- `tests/unit/test_finished_event_attestation.py` 7 条：裸事件采信且标记
  未凭据 / 匹配 token 采信且标记 / 错 token 拒绝 / 未注入却带 token 拒绝 /
  非字符串 token 拒绝 / Python 默认分隔符形态仍认（容错与凭据并存）/
  别的事件与非 JSON 仍拒绝。
- 反向验证：摘掉 token 校验后三条拒绝用例同时变红。

## [Unreleased·八] R4a 可复制本地示例（无模型全链路）

RELEASE-PLAN R4a 的交付物：`examples/local-no-model/` —— doctor → prepare →
approve → execute → verify → satisfied 全链路，**无模型账号、无网络、无密钥**。
此前仓库只有 `drafted → cancelled` 的控制面示例，从未有一条可复现的「真跑完」
链路（问题 2 分析里指出的最大证据空洞）。

### Added

- `examples/local-no-model/{run_example.py,worker.py,checker.py,README.md}`：
  - 执行者/核验者是两个普通 subprocess 程序，分别走两条 stdout 协议通道
    （`attempt/finished` 事件行 / `lhgp-verdict` 判定块）；
  - 派工、回收、交叉验收全部由**真实调度主循环** `run_daemon_loop` 产生，
    不自己拼 tick、不伪造事件；失败时打印可执行排查出口；
  - 中英文 README 入口链接已加进 README.md / README.zh-CN.md。
- `tests/integration/test_local_no_model_example.py`：断言终态、交付物内容、
  executor+verifier 两条真实 attempt 均 succeeded、且 `attempt/*` /
  `verification/*` 事件中**不存在 actor=user 的记录**（成功非人工补写）。

### Fixed

- **事件行识别只认紧凑 JSON（第三方 harness 的静默陷阱）**：适配器按字面前缀
  `{"event":"attempt/finished"` 扫描，而 Python `json.dumps` 默认在 `:` 后加空格
  ——合规的事件行扫不到，且失败是静默的（退化成"等进程退出"，跨进程时直接
  变成 §11.3 detached）。改为正则先认形态（容忍键值间空白）、仍须 `json.loads`
  通过才采信；实测 4 种合法形态识别、2 种负例（别的事件/非 JSON）拒绝。

### 开发中实测确认的两个既有语义（非缺陷，已写入示例排查出口）

1. 每轮新建 runner ≈ 重启守护进程：在飞 attempt 被 reconcile 判为 detached
   （退出码不可回收）→ failed（SPEC §11.3 的诚实边界）；
2. 注入时钟只推动合同决策时间，子进程需要真实时间退出——两者都要给。

## [Unreleased·七] 交接附言截断纪律：行界 + 显式标记

四层审查（第 3 层边界）在注入面清单里剩下的最后一处裸切片：
`handover_prompt_addendum` 用 `text[:1200]` 截交接摘要——切在半行中间时，
下一个执行者读到一句无头无尾的话，且看不出后面还有内容。**「被截断」
这个事实本身也是信息**（openpi bg_watch 的 `WATCH_LINE_MAX_CHARS` 同款
纪律：报告行封顶并显式以 … 收尾）。

### Changed

- 新增 `truncate_at_line_boundary(text, max_chars)`（context.py，纯函数）：
  多行文本截到**最后一个完整行** + 显式标记「已截断，完整内容读
  handover.md」；标记字节计入预算（含标记总长不超上限，不偷偷突破）；
  单行超预算的退化场景（行界与预算不可兼得）硬切 + 标记，诚实边界
  写在 docstring。
- `handover_prompt_addendum` 改走该函数。

### Tests

- 8 条单测：短文本不动 / 恰好预算不动 / 多行切点为完整行（判据：body
  下一字符在原文是换行）/ 无半行残留 / 单行退化硬切 / 标记计预算 /
  预算小于标记拒收（fail-closed）/ 端到端超长 next_action。
- 反向验证：退回裸切片后 `test_addendum_uses_line_truncation` 红。

## [Unreleased·六] 成本预算线 budget.max_cost（台账的强制面）

第五轮回答「烧了多少」，本轮回答「最多烧多少」——预算第一次能按**钱**
而不是按次数画线（SPEC §12.3.2，DESIGN v0.11）。

### Added

- **`Budget.max_cost`（可选，正数，默认缺席）**：货币单位由部署约定，
  协议不解释。合同 wire schema 预算对象增可选键；草案解析对齐 JSON
  schema 的严格口径（数字字符串不宽收——`_strict_float` 的宽口径对既有
  字段成立，新字段若放行就是两套校验两个结论）；显式 `null` 拒收而非
  当作缺席（缺席与 null 是两种声明）。
- **decide() 成本分支**：成本耗尽 → `HAND_TO_USER`，且**先于**「无租约
  STEER 转 RESPAWN」的换挡——转向重派同样要拉新会话花钱。租约活着时
  维持 §7 封顶（成本线是派工侧的门，不改变提醒行为）。
- **tick 接线**：仅在合同声明 max_cost 时才扫台账（其余合同零额外查询）；
  口径 = 该合同全部 attempt 的 cost_estimate 合计（executor 与 verifier
  都烧钱）。
- **测试 20 条**：单测 18（解析 6 / raw 校验 2 / decide 分支 5 / JSON
  schema 2 / 存取回环 2 / 加既有回归）+ tick 端到端 2（触线者 blocked
  且零派工、对照组同轮正常派工、未触线正常派工）。

### Fixed

- **存取回环丢字段（本轮端到端测试首跑即抓到的真 bug）**：budget 的
  序列化/读回是 store 里**三处手写字段清单**（save、revisions 快照、
  `_row_to_contract_view`），Budget 数据类加了字段但三处都没跟——
  能力在场，钱线消失在存取之间，强制面静默失效。三处补全，并新增
  「写入即读回」回归测试钉死。教训与 real_entry 棘轮同款：**手写字段
  清单是漂移温床**，新增预算字段时必须全点对账（本轮用 grep
  `max_output_bytes` 找齐全部构造点）。

### 语义边界

- 未声明 max_cost 的合同行为与既往版本完全一致（既有 1506 条测试零改动
  通过）；
- 自报是下界（SPEC §12.3.1）：触线判断基于执行者自报的合计，压缩/缓存
  刷新可能使真实消耗更高——线的位置由用户在知晓该语义的前提下设定；
- 同一 attempt 重复写回按最后一次自报为准。

## [Unreleased·五] 核心声明落地为运行时证据：无动作窗口零派工

ROADMAP §六指标表里「无动作窗口 LLM 调用数」一直记为**未度量**——这是
「调度核心不依赖 LLM」这条首要卖点的可验证形式。lhgp 的 LLM 调用只发生在
executor/verifier attempt 内部，因此其运行时可观察面 = 安静窗口内零新
attempt、零派工事件。

### Added

- `tests/integration/test_quiet_window_zero_dispatch.py`：三类安静合同
  （ACTIVE+活租约在跑 / ACTIVE+紧迫度极低 / BLOCKED 等人）连跑 5 轮 60s tick，
  断言 attempt 计数零增量、attempt/started 与 escalation/dispatch-deferred
  零新增、三个合同状态逐字不变。任何让安静窗口产生派工的改动（QUEUED 误
  升级、blocked 误唤醒、租约误判死）在此变红。
- ROADMAP 指标表：起点列从「未度量」更新为「已度量」，指向该测试。
- claims 登记为 `quiet-window-zero-dispatch-measured`。

## [Unreleased·四] attempt 消耗台账（openpi 第四轮吸收：usage / 成本维度）

openpi 有完整的消耗面：workflow `usage()` 返回 token/成本/上限、`/usage` 查
服务配额、`usage-changed` 事件流。lhgp 把预算当硬边界，却只记「派几次」
（max_dispatches），**没有任何账记「烧了多少」**——stats 只有墙钟和退出码。
本轮补台账；预算强制（budget.max_cost）动合同冻结区 schema，明确留待单独审批。

### Added

- **`lhgp/persistence/usage.py`**：usage 自报的形状校验（纯函数）。
  `input_tokens`/`output_tokens` 必填非负整数，`cache_read_tokens`/
  `cache_write_tokens`/`cost_estimate` 可选；负数、错型（含 bool 伪装 int）、
  未知键一律 `UsageInvalidError` 拒收。
- **schema v5**：`attempts.usage_json` 列。新库 DDL 直接带列；旧库经
  `_migrate_v4_to_v5` 幂等补列（`_add_column_if_missing` 同款，v4 库实测
  升级后可重复执行 ensure_schema）。
- **顺手拔掉一颗上轮迁移埋的雷**：`StoreConfig.schema_version` 是与
  `STORE_SCHEMA_VERSION` 并存的手写死值，v3→v4 迁移时常量升了它没升，
  靠「恰好相同」活到 v5——本次升 5 后立刻炸出：所有 runner 集成测试
  集体 `StoreTamperedError`（配置期望 4 < 库 5 被只读拒收）。已修并加
  漂移守护测试（两处值必须一致，不再靠巧合）。
- **write-back 链路**：RPC `attempt/write-back` 与 MCP `lhgp_write_back`
  接受可选 `usage`；拒收发生在任何落库之前（attempt 行无副作用、无终态
  事件）；终态事件 payload 携带 usage 与 attempts 行并行供审计；不携带
  usage 的存量写回零变化。
- **stats 聚合**：`build_stats` 新增 `usage_totals`（跨 attempt 透明合计，
  不换算不外推）；没记过台账不出现该键；存量脏数据（手改库）跳过不抛。

### 语义（SPEC §12.3.1）

- 自报是**下界**：上下文压缩、缓存刷新可能使真实消耗更高，消费方不得当精确值；
- 同一 attempt 重复写回按最后一次自报为准（记终局累计，非逐次增量）。

### Tests

- `tests/unit/test_attempt_usage.py` 21 条：校验 9 边界（负数/bool/错型/
  未知键/None/缺字段）、schema v5 新库与 v4 旧库幂等升级、写回落库+终态
  事件并行、拒收无副作用、无 usage 向后兼容、stats 跨 attempt 合计/
  空库无键/脏数据跳过。反向验证：摘掉 RPC 校验（负数落库）与摘掉 stats
  聚合，各自命中预期测试名。

## [Unreleased·三] 判定块截断丢失可区分（openpi 第三轮吸收：assertWatchableOutput 原则）

openpi 的 `assertWatchableOutput` 原则：**派生观察依赖的缓冲被预算驱逐后，
观察必须显式失效，不能静默降级成"没找到"**。这条原则在 lhgp 有一个已实证的
缺口，本轮修复。

### 实证（修复前）

真子进程复现：同一个 verifier 脚本（1000 行噪声 + 末尾判定块）——
无预算时 `parse_verdict_block` 正常解析（verdict=True）；预算 4KB 时
verdict **静默变 False**，`output_truncated=True` 但**没有任何下游消费这个
旗子**。verifier 跑成功了、证据也写了，却按协议退化为"无证据 → 人工仲裁"，
用户白等一轮，且审计里看不出原因。

### Fixed

- **`verdict_from_output(stdout, *, output_truncated)`**（`lhgp/acceptance/verdict.py`）：
  输出被截断**且**未解析出判定块 → 抛 `VerdictSourceLossError`；截断但块在
  截断点之前 → 正常返回（块自足）；未截断 → 与 `parse_verdict_block` 一致。
- **`runner._finish_attempt` 消费截断事实**：捕获该异常并写入事件 payload 的
  `verdict_source_loss` 字段——下游同为 undetermined/人工仲裁，但「部署侧
  预算问题（调大重跑即恢复）」与「verifier 未按约定输出」责任方不同，
  必须可审计地分开。
- SPEC §12.4 补截断语义条款（MUST 可区分）。

### Added

- conformance 3 条（真子进程：截断抛错 / 无预算正常 / 无块无截断仍 None），
  载荷用临时脚本文件构造——`-c` 的多层转义会把字符串字面量撕开，本轮
  复现脚本三次踩坑后改用文件传递，教训写进了测试 docstring。
- unit 4 条（`verdict_from_output` 纯函数面），反向验证：守护条件改 `if False`
  后 `test_truncated_absent_block_raises_source_loss` 变红。

### 甄别后判定不吸收（openpi 同源机制已有 lhgp 结构等价物）

- **Goal blockedAudit（连续 3 turn 才算真 blocked）**：lhgp 的 tick 主循环对
  BLOCKED 合同直接 `continue`、`decide()` 只对 ACTIVE 合同运行，结构性避免了
  "同一阻塞每轮重复升级"；风险通知有 revision 级幂等键
  （`{cid}:risk-red:revision-{rev}`）。无缺口。
- **Context Pivot（30K token 门槛的定向压缩）**：lhgp 的 auto-handover
  （`check_handover_due` 60%/90% 双水位 + 60s 去抖 + HANDOVER_DUE 事件）已
  覆盖同一问题面，且走的是"换下一个 attempt"而不是"压缩当前会话"——
  与跨会话架构更一致。无缺口。
- **result-budget（父上下文 headroom 按比例分配子结果预算）**：lhgp 的
  `max_output_bytes` 是合同冻结区硬预算，语义不同（成本上限而非上下文
  分配）。若未来做"多 attempt 结果聚合进单一提示词"再回来抄这个。

## [Unreleased·二] 工具面 profile（基线 6e5f2f8 之后，分支 feature/tool-surface-profiles）

依用户裁决实施：「相对严厉的改动」——51 个工具全量挂进宿主已影响正常使用。
基线先行：main 已锚定在 6e5f2f8（7 门全绿存档），本段全部改动只在分支上。

### Added

- **`src/longtask/mcp_profiles.py`**：工具面六档 profile。`tools/list` 只返回
  profile 内工具；**`tools/call` 对 profile 外工具一律拒调**（-32602
  "outside this server's profile"）——隐藏必须是真的不可达，否则模型仍可凭
  名字硬调，「看不见」只是装饰。`initialize` / `tools/list` 应答带 `profile` 字段。
  - `executor`(11) / `verifier`(7) / `planner`(12) / `operator`(36) / `full`(35 正名)
    / `legacy`(52 全量)；
  - **未设置 `LHGP_MCP_PROFILE` = legacy**，既有安装一次也不变；
  - 未知取值**启动即退出**并说明原因，不静默回落；角色清单引用不存在的工具名
    同样启动即报错（宁可不启动，不静默少暴露）。
- **`lhgp_user_confirm_spec_verdict` 正名**：此前 CANDIDATE→PASSED 的唯一 MCP
  入口只有 `longtask_user_confirm_spec_verdict`（别名轨独有）——直接清别名
  等于删掉签字能力本身。正名与别名同 handler、同 schema，分类 destructive。
- **`tests/unit/test_tool_profiles.py`**（17 条）：声明一致性 / 行为 / 默认不变 /
  尺寸守护（executor/verifier/planner ≤ 全量一半+6，operator = full 减别名轨）。

### Changed

- **`_dispatch` 双处接入 profile**：tools/list 过滤 + tools/call fail-closed；
  绕过 `serve_stdio` 的未初始化 context 拒绝服务（防静默全量）。
- **契约同步**：ARCHITECTURE marker 51→52（35 正名 + 17 别名）、
  `LEGACY_ONLY_SUFFIXES` 4→3（user_confirm 移出）、annotations destructive
  集合补正名、SKILL 场景决策表统一 `lhgp_*` 正名（原先三种轨道名混用）并加
  profile 说明、DESIGN v0.9 新增 §11.8、SPEC §19.3 补「清别名前必须先补齐
  正名缺口」条款。

### 量化（改动动机）

- 全量 schema 34,007 B（粗估 8.5K token），其中 37% 为兼容别名重复；
- executor 面 4,662 B（**−86%**）、verifier 2,563 B（−92%）、planner −79%；
- 执行者原本能看见 `lhgp_approve_goal` 等 Principal-only 入口——模型调用会被
  AUTH_FAILED，但**能看见就会去试**，每次都是注定失败的往返。

### 反向验证（四项变异全部命中预期测试名）

1. 摘掉越界拒调 → `test_call_outside_profile_is_refused` 红；
2. executor 塞入 approve → `test_executor_cannot_see_principal_gates` 红；
3. operator 丢签字门 → `test_operator_can_reach_the_signature_gate` 红；
4. 未知 profile 静默回落 legacy → `test_unknown_profile_is_rejected_not_silently_defaulted` 红。
另做过真子进程端到端：executor 下 tools/list 恰 11 个、硬调 approve 被拒、界内 health 正常。

### 未做（下一步，需再过审批）

- 17 个 `longtask_*` 别名的删除（SPEC §19.3：保留至少一个次版本；正名缺口已补齐，
  剩余三个别名-only 名字均有正名对应）。
- PARALLEL 档行为与措辞（见「审查发现但未修」，属 R3b）。

## [0.1.0a13] - 2026-09-10

绑定时机：第 9 轮外部审查指出，上一轮（a12）引入的内容指纹绑错了时间点——
收尾代码读的是**收尾时**的最新验收，而核验器实际收到的要求是在**启动时**定的。
结论成立，这是一条能走完的误完成路径；同时处理 macOS CI 的一项启动时序竞争。

### Fixed

- **指纹改按 attempt 准入修订计算**：`cli/runner.py::_finish_attempt` 原先在回收时读
  `get_contract(...).draft.acceptance` 并盖章，因此「核验进行中修订验收」会把**新**要求的
  身份刷到为**旧**要求跑出的证据上，`user_confirm` 随后匹配通过——new.txt 从未存在而合同
  已 COMPLETE/passed。现在经 `persistence/store.py::acceptance_at_revision` 从
  `contract_revisions` 的不可变修订快照取身份（`attempt_evidence_binding`），三个生产端
  （runner 回收、reconcile 崩溃恢复、write-back RPC）均改用它。快照写一次且不参与清理，
  所以「当时被要求做什么」在数据库生命周期内都可回答。
- **快照解析不到时不降级**：`attempt_evidence_binding` 返回 `{}`而不是回退到当前验收——
  无身份事件在下游被拒，代价是重新核验。回退到「当前」正是这个洞的成因。
- **`test_unix_socket_round_trip` 与 `bind()`/`listen()` 竞争**：测试轮询
  `endpoint.exists()` 后直接 `connect()`，但创建 socket 节点的是 `bind()`，`listen()` 在其后——
  macOS 在这个窗口里抛 `ConnectionRefusedError`，而 Linux 会把连接排入 backlog 而暂时抢赢。
  改为等到「真的能连上」（复用 `tests/wait_budget.py`）；同时把产品侧 `listen()` 提到
  `chmod` 之前，窗口从三条语句缩到一条——排序消不掉竞争（节点由 `bind` 创建），
  因此客户端必须重试而不能把「文件存在」当作「服务就绪」。

### Added

- **`tests/integration/test_verifier_evidence_timing.py`**：驱动真实回收路径
  （`AttemptRunner._finish_attempt` + stub adapter）与真实修订链，3 条：事件必须带
  准入修订的指纹、运行中修订必须使人工确认被拒、无修订时仍应能完成。
  **已做反向验证**：把绑定点改回读当前行后前两条变红（其中一条 `DID NOT RAISE`），
  确认测试能抓住这个 bug而不是跟着实现自圆其说。
- **`tests/unit/test_acceptance_content_binding.py` 新增 4 条**：修订解析与未知名修订、
  write-back 生产端的时序、运行中修订的消费端拒绝，并把生产端扫描从「有
  `evidence_binding`」升级为「有 `attempt_evidence_binding`」——只盖章仍不够，必须从
  attempt 的修订拿章。

### Notes

- 审查者的建议里只提了「把指纹在启动时固定」，没提扫描断言需跟着升级；只改绑定点
  而不改扫描，下一次生产者退回读当前行仍会放行。
- macOS 那条修复在本地**无法执行验证**：本机 Python 3.13.13 无 `AF_UNIX`，该测试在
  Windows 上本来就 skip。已通过的是语法/类型/收集，真凭据靠 macOS CI 跑。

## [0.1.0a12] - 2026-09-10

验收内容绑定：第 8 轮外部审查用一个真实 MCP 会话复现出「改完验收条件后，
合同仍能凭旧证据变成 COMPLETE / passed」。根因是一处 fail-open 比较：
守卫只在两边哈希**都非空且不同**时拒绝，而 `spec_hash` 是可选且由调用方自报的。

### Fixed

- **核验证据改由 runtime 计算的内容指纹绑定**：`Acceptance.spec_hash` 原先兼任绑定依据，
  但它默认为 `None`、MCP 也只在调用方主动传时才转发，所以四种输入状态里只有
  「两边都填且不同」这一种会拒绝。更糟的是：本来老实用哈希的调用方在改验收时
  忘了重填，当前值变成 `None`，同一条规则反而把它读成「相等」而放行。
  新增 `src/lhgp/contracts/acceptance.py::Acceptance.content_fingerprint`——由 runtime 对
  `standard` + `checks`（typed、保序）+ `verifier` + `spec` 取 sha256，调用方无法伪造或漏填。
  `src/longtask/rpc/handlers/contract.py::_latest_verifier_evidence` 改为 **fail-closed**：
  缺内容标识一律视为不匹配。旧的 `spec_hash` 否决保留，本改动只收紧不放宽。
- **三个证据生产端全部盖章**：`cli/runner.py`（正常回收）、`promoter/reconcile.py`
  （daemon 重启后的崩溃恢复——原先这条路径写出的 verifier 事件**完全没有**内容标识，
  却在旧规则下无条件算匹配）、`rpc/executor_api.py`（write-back，payload 里嵌 `role`
  因而可被匹配器看到）。三处统一走 `evidence_binding()`，并由
  `test_every_verifier_success_producer_stamps_the_binding` 扫描兜住未来的第四个生产者。
- **拒绝文案说明下一步**：`STATE_FORBIDDEN` 现在区分 `edited`（验收被改）/
  `spec_hash`（调用方改标）/ `unbound`（旧证据无指纹，永不可信）。
  原来三种都显示「acceptance was edited」，运维会去找一次根本没发生过的编辑。
- **`_latest_verifier_evidence` 被连调两次**：`handle_contract_user_confirm` 里同一行
  复制粘贴了两遍，结果一致故无行为影响，但每次确认都多扫一遍事件流。
- **`test_canonical_modules_preserve_module_execution_entrypoints` 环境敏感**：子进程
  `--help` 输出含中文，而父进程按主机 locale（GBK）严格解码；环境里只要存在
  `PYTHONIOENCODING` 它就以 `UnicodeDecodeError` 变红——测的是外壳不是入口点。
  按本文件既有先例（cp1252 那条）把两侧编码钉成 UTF-8。

### Added

- **`tests/unit/test_acceptance_content_binding.py`**：18 条回归，覆盖审查者复现矩阵的
  四种 `spec_hash` 状态、无标识旧证据、仅改标不误伤、以及**必须仍然能完成**的阳性对照
  （fail-closed 改动同样有能力锁死正常路径）；另含指纹跨存储往返稳定、typed 与 mapping
  写法同值、每个实质字段变动都改变指纹。
- **claims 记账**：新增 `verifier-evidence-bound-to-acceptance-content`（44 条声明，43 verified）。
- **方法论沉淀**：`docs/wiki/playbook/regression-from-external-review.md` 新增 pattern 11
  「可选字段 + fail-open 比较」，并把它加进 New-PR checklist 第 14 项。

### Changed

- **口径修正**：`Acceptance.spec_hash` 的注释原本声称「runtime 会在合同准备时捕获 spec
  内容哈希」，而 runtime 从不重算它；MCP 工具描述与 `schemas/contract.schema.json` 同步
  改为「调用方自报标签，不作为证据绑定」。
- **修正本文档的重复编号**：playbook 里有两个 `## 8`，导致 `tests/conftest.py` 把
  real_entry 规则引向 `pattern 7`（实为 pattern 8）。尾部章节重编为 9 / 10，
  两处内部引用同步修正。

## [0.1.0a11] - 2026-09-10

门禁可信度：本轮不增加新能力，而是把四类「门绿着但并没有在看东西」的失效
改成真信号。两项是 P0 语义缺陷，其余是可机器校验的棘轮与口径。

### Fixed

- **计划批准门读注入时钟**：`src/longtask/persistence/store.py::_has_recent_plan_approval_for_goal`
  的新鲜度截止原先取 `_dt.now(UTC)` 墙钟，而非调用方传入的 `now`。seeded-clock
  测试因此永远看到「窗口已过期」并在规则之前返回，包括
  `test_narrowed_grant_blocks_auto_activate`——它一直是「代码没跑就通过」。
  改为整条判定链走同一个 `now`，窗口语义与 `PLAN_OVERRIDE_LOOKBACK_SECONDS` 对齐。
- **授权漂移现在真的作废旧批准**：修时钟后上条测试变红，暴露出文档承诺的
  「收窄授权必须阻止自动激活」实际只实现了「granted_set 非空」。新增
  `_grant_scope` / `_grant_still_covers` / `_grant_as_of`：从 `GOAL_AMENDED` 审计链
  重建「批准当时的授权」（`patch_goal` 每条事件都带完整 post-patch plan），并比较
  **覆盖**而非相等——加宽授权保留旧批准，换域/收窄/撤销 wildcard 必须重新
  `lhgp_plan_signoff`。`tests/unit/test_trusted_pre_authorization.py` 新增 6 条参数化
  drift 回归锁住边界。
- **`lhgp memory expire` 报告的是 id 列表不是计数**：`src/lhgp/memory/cli.py` 直接
  `print(f"expired {n} ...")` 而 `expire_due()` 返回 `list[int]`，输出
  `expired [1] due memories`。改为 `len(...)`，与 `daemon_loop.py` 的 `dropped {n}` 口径一致。

### Added

- **三个 0% 新模块的 focused 测试**：`tests/unit/test_templates_validate.py`（五层
  校验，98%）、`tests/unit/test_timeline_render.py`（自包含 HTML、转义与损坏 payload
  降级，100%）、`tests/unit/test_memory_cli.py`（add/list/search/show/expire 的退出码
  契约，100%）。全库覆盖率 77% → 79.1%。
- **覆盖率地板基线化**：`quality/coverage-baseline.json` + `scripts/quality_gate.py::coverage_fail_under`，
  脚本内不再有 `--cov-fail-under=<字面量>`；基线读不到则拒绝通过。`tests/unit/test_coverage_ratchet.py`
  钉住地板来源，并相对 HEAD 校「只许上调」。
- **`real_entry` 告警上棘轮**：`tests/conftest.py` 在收集阶段比对
  `quality/real-entry-baseline.json`，欠款 114 条（7 个文件）固定为基线；新增一条未标记的
  真实入口测试会让整个会话以 `UsageError` 退出。之前它是淹没在日志里的重复警告。
- **claims 锚点可达性校验**：`scripts/claims_check.py::check_pinned_sha` 用
  `git merge-base --is-ancestor` 确认 `pinned_sha` 从当前历史可达，错误消息区分
  对象丢失 / 浅克隆 / 不可达三种情形；两个 CI workflow 改为 `fetch-depth: 0`。
  历史脱敏后旧锚点 `29833dc…` 已失效，这正是该检查要拦的那类静默失效。
- **子进程等待预算集中旋钮**：`tests/wait_budget.py` 把 13+ 处硬编码墙钟超时
  （5/8/10/15/20s）改成 `budget(...)` 并将 `time.time()` 换为 `time.monotonic()`，
  满载机器上由 `LHGP_TEST_WAIT_SCALE` 一个变量加额度，而不是改八个文件。
- **仓库卫生回归**：`tests/unit/test_repo_hygiene.py` 断言 `git ls-files --ignored --cached
  --exclude-standard` 为空——`.gitignore` 挡不住已跟踪文件，这个状态以前无人守。

### Changed

- **arch 门覆盖两棵命名空间树**：`scripts/arch_check.py` 此前只查 `src/longtask`，
  `src/lhgp` 可以任意越层。接入后发现真违规：`src/lhgp/contracts/resume.py` 直连
  SQLite，已归位到 `src/lhgp/persistence/resume.py`（`contracts` 层保持纯数据）。
- **取消跟踪 67 个误提交文件**：`scratch-trash/` 54 个、`quality/` 下 9 份门运行日志
  与 3 份 commit message 草稿、`_move_list.json`，均 `git rm --cached`（磁盘文件保留）。
- **版本自述与发布元数据重新对齐**：`src/longtask/__init__.py::__version__` 与配套
  `skills/longtask-contract/MANIFEST.json` 停在 `0.1.0a6`，而包已发到 `a10`——`lhgp --version`、
  `lhgp doctor` 与 MCP `implementation_version` 四处都在报错版本号。两者一并升到
  `0.1.0a11`，`tests/unit/test_p6_plugin_package.py` 新增三方对账（pyproject / 运行时常量 /
  `uv.lock`）把这类漂移变成红门；`PROTOCOL_VERSION` 仍按 DESIGN §11 独立演进。
- **文档口径与实现机器对账**：MCP 工具总数由 `tests/unit/test_mcp_tool_surface.py` 对照
  文档 marker（51 = 34 `lhgp_*` + 17 `longtask_*`）；`ARCHITECTURE.md` 补齐工具表；
  `docs/RELEASE-PLAN.md` 不再复述包版本、并标注旧基线提交号脱敏后不可达；
  `docs/wiki/playbook/quality-gate.md` 与 `CONTRIBUTING.md` 改为指向基线文件。

### Notes

- 本轮改动全部仍属参考实现与门禁层，没有新增对外的协议能力；`docs/LHGP-SPEC.md`
  的声明口径未变，`quality/claims.json` 只重锚 `pinned_sha` 与复验日期。
- 未动账：`src/lhgp/wiki/__init__.py` 的浏览命令（10%）与 `src/lhgp/flow/cli.py`（24%）
  仍欠 focused 测试；`real_entry` 基线的 114 条欠款待逐文件判定后下调。

## [0.1.0a10] - 2026-09-08

Resilient contract execution:从「能跑」到「死了也能接着干」。

### Added

- **Auto-handover detector (DESIGN §4.1)**:`src/longtask/persistence/context.py::check_handover_due`
  在 `active.md` 逼近 context 窗口时返回 True。`HANDOVER_DUE_DEBOUNCE_SECONDS=60`
  warm 分支自动 debounce,`overdue` (ratio ≥ 0.9) 立即触发。`HANDOVER_DUE` 审计
  事件记 `reason=auto_handover_warm` / `auto_handover_overdue`。`_resolve_data_root()`
  PRAGMA helper 让 in-memory DB 走 None 路径不被错误处理。
  **`src/longtask/cli/daemon_loop.py::_check_handover_due`** 每轮 tick 遍历
  `runner.running_attempts()`,在 `run_daemon_tick` 之前对 overdue attempt
  补 HANDOVER_DUE 事件(warm 分支事件已由 detector 自己落,不再双发),best-effort
  单 attempt 异常不破坏整轮 tick。

- **Transient retry for RPC dispatch**:`src/longtask/rpc/dispatch.py::with_transient_retry`
  装饰器,指数退避 + 抖动,对 `ConnectError` / `ReadTimeout` /
  `RemoteProtocolError` / stdlib `ConnectionError` / `TimeoutError` / 429/502/503/504
  自动重试,4xx(除 429)和 5xx(除 502/503/504)直接上抛不重试。
  `src/lhgp/rpc/server.py::route()` 用此装饰器包住默认 conn 路径,每次重试
  写 `RETRY_ATTEMPTED` 事件带 attempt/delay/error_type/method,审计失败
  永不破坏重试(closure try/except 吞掉)。默认 conn 路径无法跨调用传 conn,
  该路径 audit 静默 skip(commit message 文档化)。

- **Structured Plan gate**:`src/lhgp/contracts/plan.py` 新 dataclass `Plan` /
  `PlanStep` / `PlanValidation` + 白名单 `ALLOWED_ACTIONS = {read file, run command,
  ask user, verify acceptance, write file, search code}`。`Plan.validate(view)` 四道
  检查:step 数 ≥ 1、action 在白名单、rationale 含 objective 关键词、每个 acceptance
  check 被某 step 的 target 或 expected_outcome 覆盖。approved → 落 `PLAN_APPROVED`;
  rejected → 落 `PLAN_REJECTED` 带 `rejection_reasons`。两个入口对称:
  - CLI:`lhgp plan submit <contract_id> [--from <file>] [--submitted-by ...]`,
    从 stdin/file 读 JSON。
  - MCP:`lhgp_submit_plan` (新增 49 个工具之一),相同 pipeline。
  - 写侧三事件:`PLAN_SUBMITTED`(必落)→ `PLAN_APPROVED` 或 `PLAN_REJECTED`。
  - **`src/longtask/cli/dispatch.py` 在 runner 端强制**:`_has_recent_plan_approval`
    查 24h lookback 内最近一次 PLAN_APPROVED/PLAN_REJECTED,后续被 PLAN_REJECTED
    推翻的 approval 不算数。`_plan_gate_required(contract)` opt-in via
    `contract.draft.context["gate"] == "plan"`,默认 off 不破坏现有合同。
    开启且 gate 不满足时,`DISPATCH_REFUSED` + `reason=plan gate: no recent
    PLAN_APPROVED event`,返回 None → caller 走 blocked(any path that bypasses
    `plan submit` / `tool_submit_plan` 都不能派发 gated 合同)。

- **Resume brief entry point**:`src/lhgp/contracts/resume.py::build_resume_brief`
  读 `active.md` + `handover.md` 拼一份 self-contained brief(可直接喂入新
  LLM 会话,免去重新推导项目背景)。`active.md` 缺失 → `FileNotFoundError`;
  `handover.md` 缺失 → 用 `_(no handover.md written)_` 占位,best-effort。
  `_derive_next_attempt_id` 纯函数(可 override,默认从 `attempt_id + iso 时间`
  派生)。每次调用写 `ATTEMPT_RESUMED` 事件带 `from_attempt_id` /
  `next_attempt_id` / actor。两个入口:
  - CLI:`lhgp attempt resume <contract_id> <attempt_id> [--next-attempt-id ...]`
    把 brief body 打到 stdout。
  - MCP:`lhgp_resume_attempt` (新增 49 个工具之一),返回结构化 dict
    (contract_id / attempt_id / next_attempt_id / active_md_path /
    handover_md_path / body)。

- **6 new EventType entries**:`src/lhgp/persistence/events.py` 新增
  `RETRY_ATTEMPTED` / `PLAN_SUBMITTED` / `PLAN_APPROVED` / `PLAN_REJECTED` /
  `HANDOVER_DUE` / `ATTEMPT_RESUMED`,wire string 与先前 4 个 stream pin
  的字面量完全一致,**零 protocol drift**,已落盘的事件无需迁移。

### Quality

- 7/7 quality gate 全过(format / lint / arch / deps / claims / mypy /
  pytest+coverage)。无 new `# type: ignore`、无 new `Any` returns。
- 测试 +30(7 → 1114 passed, 6 skipped, 75.62% coverage)。新增 18 个
  覆盖本轮产品面:
  - 12 个 `tests/unit/test_plan_gate.py`(helper 函数 + 4 条 dispatch 分支)
  - 3 个 `tests/integration/test_mcp_server.py`(真实调
    `tool_submit_plan` / `tool_resume_attempt` 并查审计事件流)
  - 3 个 `tests/unit/test_auto_handover.py`(`_check_handover_due` daemon tick
    集成:overdue emit / warm 不双发 / missing active.md skip)
- Verifier 标 APPROVE。fix 完所有上一轮报告的 must-fix:`attempt resume`
  块在 `main.py` 重复 3 份已 dedupe,过期 docstring 已清理,daemon loop
  现在真调 `check_handover_due`,plan gate 在 runner 端真实拦截。
- Worktree 痕迹已清(`.worktrees/attempt-resume` / `feat/attempt-resume` 分支
  / `feat/rpc-transient-retry` 已合入 main 后删除)。

## [0.1.0a9] - 2026-09-07

memory-and-wiki Phase 2 + 3 + 4:协议从「能写入」到「会沉淀/会看自己」。

### Added

- **Failure-driven memory**:`src/lhgp/feedback/lessons.py::mine_lesson_if_due(conn,
  contract_id, min_failures=3)` 当 contract 累积 ≥ 3 个 `ATTEMPT_FAILED` 或
  `verdict=reject` 信号时,自动写一条 `GOTCHA` 全局记忆。新事件类型
  `MEMORY_LESSON_MINED`(audit 账本)。Hook 通过 `record_evaluation` 的
  REJECT 分支触发(failure 路径),不污染 ACCEPT 评估。`source_event_id`
  fallback:REJECT-only cluster 没有 ATTEMPT_FAILED 时,fallback 到
  `user_evaluations.evaluation_id`,审计账本永远不会 NULL。
  `last_reject_id` 走 `last_reject_id` audit payload key。
- **Protocol-aware flow**:`lhgp flow contract <id>` 走 contract 引用代码的
  call graph,不是任意 .py。`walk_contract(conn, contract_id, src_root=None)`
  从 `acceptance.standard` / `acceptance.checks` / `execution.target` /
  `context.python_module` 抽 seed 串,优先按模块路径解析,失败回退到
  substring grep(3 个 cap,触发 cap 时打 WARNING)。支持 `import X` /
  `from X import Y` 字面 statement 自动转 module path。`path.stat().st_size`
  预检 ≥ 2 MiB 的文件直接拒,避免 OOM。空结果产 contract:`<id>` 占位节点。
- **Self-publishing wiki**:`lhgp wiki publish` 把 active contract 渲染
  到 `<wiki_root>/auto/<id>.md`(Obsidian frontmatter + sectioned body:
  objective / status / acceptance / deadline / next_action / memory /
  related)。`next_action` 从 `handover.md` 解析。terminal 7d 后回填 banner,
  fresh terminal 不动。Path safety: contract_id 走 slug 归一化
  (`[^A-Za-z0-9_.-]` → `_`) + `is_relative_to(auto_dir)` 检查
  防止 `..` 越界。Wiki dispatcher 走 `longtask.cli.main` 拿连接。

### Changed

- `src/lhgp/wiki.py` → 包 `src/lhgp/wiki/{__init__,sync}.py`;公共 surface
  (`WikiEntry`, `wiki_command`, `WIKI_ROOT`, `INDEX_PATH`, `REPO_ROOT`)
  不变,新加 `publish_active_contracts`。
- `MemoryIndex._gather_candidates` 3 SQL → 1 SQL(unions GLOBAL + PROJECT +
  DOMAIN via `json_each` 标签匹配)。100K+ memory 表不再 3 次 indexed scan。
- `MEMORY_AUTO_MINED(_FAILED)` actor 字段截断到 64 字符 + warning log。
- `Memory.body_md` cap 文档化:64 KiB UTF-8 BYTES ≈ 21K CJK chars。
- 死代码删除:`wiki_command(publish)` 死分支、`_WalkContext.methods` dead
  state、`Flow.node`(未使用)、`ast_walker` 重复装饰器/嵌套类 fallback。
- `wiki/sync.py` 的 `TERMINAL_STATES` 改 import `lhgp.contracts.state_machine`
  唯一来源,不再本地重复定义。

### Security

- P1:wiki publish path traversal。`contract_id = "../../etc/passwd"` 在
  旧实现下能逃出 `auto/` 目录(已验证)。新实现 slug 归一化 +
  `is_relative_to` 双重防御,5 个 parametrised 回归测试
  (`..` POSIX / `..\` Windows / subdir/space/collision)全部 0 文件逃出。
- P1:CR-only line endings 归一化为 CRLF,匹配 codebase 其它文件
  (`contract_flow.py` / `test_contract_flow.py` 之前是 Mac OS Classic
  CR-only,在某些 editor / pre-commit hook 下显示为一行)。

### Quality

- 7/7 quality gate 全过(ruff format / lint / arch / deps / claims /
  mypy / pytest+coverage)。
- 测试 +45:1049 → 1055 (含 5 个 path safety + 1 个 REJECT-only
  fallback + 1 个 4-type digest + 1 个 violation list length +
  2 个 size pre-check + 1 个 mem 64KiB exact cap + 1 个 stage
  ingest 等)。
- 覆盖率 76.17% (target ≥ 70%)。
- Verifier 4-agent 集群 review(2 fresh-context 报告,2 deep
  walk-throughs)产 2 P1(已修)+ 7 P2(已修 5,留 2:CHANGELOG/ARCHITECTURE
  在此 commit)+ 7 P3(已修 4,留 3 nit)。

## [0.1.0a8] - 2026-09-07

memory-and-wiki Phase 1：协议内 wiki 合并发布版。

### Added

- **协议内 wiki**（`docs/wiki/`）—— git-tracked 纯 markdown vault,Obsidian 守则
  （wikilinks + 反链 + 层级 tags + frontmatter schema + `^blockid` 锚点）。
  **不是数据库**:wiki 是源码,可被 git diff / IDE 搜索 / 静态站生成器读。
- **种子页 11 个**:`glossary.md`(协议术语表)+ `index.md`(主入口 MOC)+
  `playbook/index.md`(playbook MOC)+ 8 个 playbook
  (add-event-type / schema-migration / sql-binding / add-mcp-tool /
  add-cli-subcommand / write-test-first / quality-gate / daemon-tick-hook),
  从 P6 28 个 commit 提炼。
- **索引生成器**（`scripts/build_wiki_index.py`）—— 扫 frontmatter + 双向 wikilink,
  产 `docs/wiki/.index.json` (schema_version 1)。`outgoing_raw` 兜底未解析引用
  (MOC 引用未来页) 与 `outgoing`(已解析) 分两路 —— AI 拿到的信号"未解析 =
  待写"是真实的。
- **CLI**:`lhgp wiki list / read / search / show-graph`,纯 read-only,
  不启 daemon 也能用;通过 `.index.json` 工作。
- **双命名空间 facade**:`src/lhgp/wiki.py`(canonical)+ `src/longtask/wiki.py`(< 5 行 re-export)。
- **Phase 2: protocol memory 子系统**（`lhgp/memory/`）—— SQLite `memories`
  表(schema v3→v4, 3 索引: scope+kind+created_at、expires_at 偏、
  source_contract_id 偏);`Memory`/`MemoryKind`/`MemoryScope` + `MemoryIndex`
  (global / domain / project 三层, capacity 合同, 单条 truncate fallback);
  `lhgp memory add / list / search / show / expire` CLI;`compile_context_snapshot`
  在 deadline 快照之后 / handover 之前接入 1/10 `max_bytes` budget;
  `record_evaluation` auto-mine hook:rating ≥ 4 + 有 comments → PATTERN
  (score 0.6/0.7),REJECT + 有 comments → GOTCHA (score 0.7, expires 365d),
  失败 logging.warning 不污染 evaluation 写入。
- **Phase 3: flowgen 代码/手写流程图**（`lhgp/flow/`）—— `walk_source` 抽
  Python 源(def/class/method + self.x 与 Class() 本地解析 + 动态调用落 external,
  自循环 / 重复边丢弃);`render_mermaid` 出 `flowchart TD` 块(Mermaid 词法
  安全的 id 冲突解决);`render_excalidraw` 出可粘贴的 JSON 场景
  (确定性网格布局);`wiki_parser` 抽 `## flow` mermaid 段 + `list_flow_pages`
  扫目录(隐藏目录 / 编码错误跳过);`lhgp flow ast / wiki / list-flow-pages` CLI。
- **新增 wiki 页**:`playbook/flowgen.md` —— 三个用法的取舍 + CLI 速查 +
  wiki `## flow` 约定 + 含一段手写流程图。
- **独立 verifier 子代理复核后的 P0 ship-block 修复**(commit `ccbc781`):
  - `expire_due` 实际无调度,过期记忆永远累积。`daemon_loop._expire_due_memories()`
    每 tick 跑在 `_enforce_deadlines` 之后,best-effort,n>0 时
    `append_event(MEMORY_EXPIRED)` 落审计账本。`EventType.MEMORY_EXPIRED`
    加进枚举。
  - auto-mine 启发式从 `"topic:" in comments` 翻 scope 但没加
    `topic/<domain>` 标签,`MemoryIndex.retrieve` 永远找不到,DOMAIN
    记忆是死信。改 anchored 正则 `r"^\s*topic:\s*([a-zA-Z][\w\-]+)"`,
    翻转 scope 同时把 `topic/<domain>` 写进 tags。
- **verifier 5 条 non-blocking 改进**(commit `29efbb2`):
  - `walk_source` 现在处理装饰器(`FunctionDef`/`AsyncFunctionDef`/
    `ClassDef` 的 `decorator_list` 都进 call visit)与嵌套类
    (`ClassDef` 递归,qualified name 索引,`self.Outer` 解析到嵌套类)。
  - Mermaid `_safe_id` 强制 ASCII(Mermaid 词法 `[A-Za-z_]`,Python
    `str.isalnum()` 默认接受非 ASCII Unicode,会卡老版渲染器)。
  - Excalidraw 箭头 `width`/`height` 改用 bounding box(`abs(delta)`),
    `points` 数组带符号,back-edge 不再产出 `width=0 / height<0`。
  - `skills/flowgen/SKILL.md` 加 FAQ 行:第二次写 `## flow` 没生效
    是因为 parser 只取第一段,删旧的再写。
  - `tests/unit/test_lhgp_namespace.py` 补 `lhgp.memory` / `lhgp.flow`
    ↔ `longtask.memory` / `longtask.flow` 的 facade identity 测试
    (P6 模式对称,任何 facade 改名都抓得到)。
- **新增 skill**:`skills/flowgen/`(SKILL.md + MANIFEST.json 0.1.0a1)——
  教模型用 `lhgp flow` 三种入口。

### Quality

- 7/7 quality gate 全过(ruff format / lint / arch / deps / claims / mypy / pytest+coverage)。
- 测试 +95 (P1 113 / P2 50 / P3 43 = 206 新增;最终落到 985 tests,差
  异是 simplify 与 P0 ship-block 阶段把若干 helper-only test 合并)。
- 覆盖率 75.35% (target ≥ 70%)。
- Phase 1 验证报告(子代理)发现 3 个 ship-block 项,已在 0.1.0a8 修复:
  1. CHANGELOG 入口(本条)
  2. ARCHITECTURE.md 真身位置地图 wiki 行(见下)
  3. 索引 `_resolve_link` 加 basename fallback + frontmatter `related` 进
     outgoing + 隐藏目录排除
- P2 修 5 个 production bug:from_db_row plain-tuple column index (-1 → 5)
  静默把 `tags_json="4"` 解析成 4、`_opt_dt` naive 视为 UTC、index domain
  过滤 `topic/<domain>` 标签(top_n 之前的下标剥离)、
  `make_pattern_memory(kind=, scope=)` 让 auto-mine 不再硬编码 PATTERN。

## [0.1.0a7] - 2026-09-07

反馈回路 + 多合同管理 + deadline 强制（P6）合并发布版。

### Added

- **反馈回路**（`lhgp/feedback`）：`user_evaluations` + `acceptance_diffs`
  两张表。1-5 评分、verdict、comments；diff 走 workspace walk 算
  before/after 文件指纹。6 个新事件类型：`USER_EVALUATION_SUBMITTED` /
  `ACCEPTANCE_DIFF_COMPUTED`。
- **多合同管理**（`lhgp/portfolio`）：`portfolio_summary`（全量合同 +
  by_state / by_deadline / by_acceptance 计数 + 最新 evaluation join）、
  `trace_contract`（单合同事件流 + 最新 eval / diff）。事件类型
  `PORTFOLIO_SUMMARY_VIEWED`。
- **Deadline 强制**（`lhgp/enforcement`）：NORMAL / WARNING(30%) /
  URGENT(5%) / BREACHED 四级，`DeadlineEnforcer` 返
  `EnforcementAction`（republish / notify_user / request_verification /
  lock_new_attempts）。daemon_loop 每 tick 顶部调
  `_enforce_deadlines`，breached 落 `DEADLINE_BREACH_LOCKED` + rebuild
  projection；相同 level 重扫不重复落事件（幂等）。`MIN_ESCALATION_WINDOW`
  60s 守门。事件类型 `DEADLINE_LEVEL_ESCALATED` /
  `DEADLINE_BREACH_LOCKED`。
- **自动模板演化**（`lhgp/learning`）：`extract_template_signals` 从
  evaluation + diff 矿信号，`TemplateEvolver` 把高质量合同
  （`overall ≥ 0.7`）自动写 `templates/auto-<cid>-r<rev>.json`，同 stem
  碰撞自动加 `-n2` / `-n3` 后缀（永不覆盖）。事件类型
  `TEMPLATE_EVOLVED`。
- **建议通道**：`suggest_draft_improvements` 在 prepare 阶段读历史
  signals 返非破坏性 advisory。
- **Schema v3**：`_migrate_v2_to_v3` 幂等加 2 张新表 + 9 索引；不回填
  历史事件（保 revision CAS 干净）。`STORE_SCHEMA_VERSION = 3`，
  `StoreConfig.schema_version` 默认 3。`_check_schema_version` 高于本
  版本只读拒写（fail-closed）。
- **新工具**（6 个，`lhgp_*`）：`submit_evaluation` / `compute_diff` /
  `evolve_templates` / `portfolio` / `trace` / `deadline_report`（总数
  41 → 47）。
- **新 CLI 命令**：`feedback submit|diff`、`evolve-templates`、
  `portfolio`、`trace`、`deadline-report`。
- **双命名空间 facade**：`longtask/feedback.py` / `learning.py` /
  `portfolio.py` / `enforcement.py` 各自 re-export 自 `lhgp/`，身份
  钉住（`is` 测试）。

### Fixed

- `_enforce_deadlines` 在 `total_seconds ≤ 0` 时 `continue` 跳过 breached
  合同 → 改用 `None` 透传让 `compute_deadline_level` 看 `remaining ≤ 0`
  必返 BREACHED。
- `compute_acceptance_diff` summary 字段 `len(changes) - len(created)`
  把 deleted 算成 modified → 独立维护 `modified` 列表，summary 改用三类
  分别 count。
- `_acceptance_pass_rate` 在 1 列 cursor（返回 plain tuple）上
  `r["event_type"]` TypeError 被 `except Exception: continue` 静默吞
  → 改成 Row/tuple 双兼容；正信号只认 synthetic "passed"/"satisfied"
  是死代码（真实事件名里无），加 `EventType.CONTRACT_SATISFIED` 作为
  真实正信号。
- `extract_template_signals` vocabulary 循环同样 `r["payload_json"]` 失
  败被静默吞，导致 `vocab` 永远空、auto-evolve 模板
  `acceptance.checks` 退化成默认 `structure-valid` → 同样改双兼容。

### Quality

- 7/7 quality gate 绿（format / lint / arch / deps / claims / mypy /
  pytest+coverage）。测试 775 → 885（+110，5 个新 P6 单测 + 1 个集成
  + 4 个 namespace identity）。
- coverage ≥ 73% 维持。

## [0.1.0a6] - 2026-09-06

审计全部清零 + E1-E4 全部增强（PR #15-#19），合并发布版。

### Added

- **E1 Deadline 校准**：按合同内主导执行器的成功样本（≥2）校准
  forecast p50/p90；低样本降级链保留。
- **E2 多合同公平调度**：紧迫档位降序派工 + per-tick 容量记账（可选上限）。
  <br/>**更正（2026-09-11 审计）**：本条原写「+ 饥饿检测（连续 5 tick 未派工自动
  提前）」，实测**未交付**——`apply_fairness_order` 收到的 `fairness_states` 恒为空
  dict，`ContractFairnessState.observe_tick`（唯一推进状态的方法）在生产代码里没有
  调用点，`starved` 列表恒为空，规则从不触发；DESIGN §8.3 承诺的
  「连续 N 轮额度不足 + u ≥ 1.0 → 升级 blocked(need-user)」是另一套语义，同样未实现。
  饥饿保护待 E2 独立 SPEC 与测试后再接线。
- **E3 提案审批闭环**：`lhgp proposal-apply` 命令 + 提案结构校验
  （stages/id/contract_id/≤20）+ 冻结区强制（executor/model/budget/
  authority 字段一律拒绝——计划提案不得走私执行权限）。
- **E4 模板库**：code-change / research / release-check 三内置模板
  随 wheel 分发。
- **新工具**（39 个）：brief（接手包）、board（风险看板）、stats
  （成本台账）、propose_plan（提案通道）；diff / prune-events /
  template / proposals / proposal-apply CLI 命令。
- **macOS 完全支持**：进程身份模型（ps etime/state），CI 升阻断。

### Fixed

- 12 项 Required（幂等归属校验 / Goal 身份保护 / 决策事务化 /
  doctor quick_check / 迁移 WAL checkpoint / 并发限额生效 / verifier
  作用域统一 / reconcile 补派验收 / stale 释租约 / fence 终止 /
  PID 身份 / 宽限配置接线）。
- 6 项深化（投影原子写 / 快照体积上限 / token 0600 / quiet hours
  验证 / patch 错误语义 / 假 request_id 声明）。
- pytest 8.4.2 → 9.0.3（PYSEC-2026-1845）。

## [0.1.0a5] - 2026-09-06

**macOS 全面支持**：darwin 进程身份模型落地，macOS CI 升级为阻断平台。

### Added

- macOS 进程探测：libproc `proc_pidinfo(PROC_PIDTBSDINFO)` 替代方案改为
  `/bin/ps`（etimes/state 机器可读字段）——跨版本无偏移依赖，
  僵尸判活、身份比对、优雅终止在 macOS 与 Linux/Windows 行为一致。
- 接手包 `lhgp brief`、风险看板 `lhgp board`、成本台账 `lhgp stats`、
  修订差异 `lhgp diff`、事件清理 `lhgp prune-events`、内置模板库
  `lhgp template`（39 个 MCP 工具，+4：brief/board/stats/propose_plan）。
- Goal 计划提案通道：`lhgp_propose_plan` 只落 goal/proposed 事件，
  模型可提议、不越权直写（ADR-004 规则 6 配套）。

### Changed

- macOS 从「实验性/非阻断」升级为完全支持；CI 三平台全部阻断绿。

## [0.1.0a4] - 2026-09-05

合并发布版：包含 a1–a3 全部内容 + MCP 工具面收口（PR #12、#13）。
此前三个增量 Release 已并入本版本，Releases 页只保留当前完整版；
细粒度历史见本文件各版本段。

### Added

- MCP 工具面收口（35 个工具）：dispatch 层强制 inputSchema
  （required/类型/未知键三查，修复「参数静默丢弃」根因）；
  `list_contracts` 暴露 cursor 翻页；新增 `goal/prepare` 工具
  （返回 admission offer、支持 goal_id/stage_id——激活
  advance_goal/goal_contract_draft）；schema↔handler 一致性回归
  （AST 全量断言，approve CAS 类 bug 进不了主干）。

### Changed

- 16 个工具描述重写为 if-then 场景式（何时用/传什么/返回什么/错误含义）；
  SKILL 新增「场景 → 工具决策表」。

### Fixed

- `attempt_status` 对不存在的 attempt 返回 UNKNOWN_ATTEMPT（原假 ok）。
- `contract_id` 非字符串直接 VALIDATION_FAILED（原静默 str 化）。

## [0.1.0a3] - 2026-09-05

多 Agent 协作开发扩展 + MCP 工具面加固（PR #11，34 工具全量审计后修复，
三平台 CI 实测通过）。

### Added

- **多 Agent 协作开发协议**（docs/AGENT-COLLAB.md）：基于本仓库被多个
  agent 反复改动的真实失败史，把「无验收标准/谎报完成/环境盲区/无证据链/
  破坏半径失控」五类失败映射到 LHGP 机制，附命令级操作手册。
- **改动合同模板**（templates/code-change-contract.json）：可直接
  `lhgp prepare --file` 的代码改动合同，验收命令为 pytest + quality_gate，
  由独立 verifier 重跑判定。

### Fixed

- MCP `prepare_contract`：字符串 acceptance_checks 在边界 fail-closed
  （此前 "file-exists" 会被逐字符拆成 11 个垃圾检查冻进合同）。
- `goal/update` 收归 Principal（ADR-004 规则 6：模型只能提议、不得直写
  权威计划）；工具描述如实标注。
- `attach_to_executor` 无活租约时进度显式报 `progress_error`，不再静默
  丢弃让模型误以为已存。
- 审计归属修正：verification/interrupt 事件用真实派生 actor，模型动作
  不再记成用户动作。
- MCP `approve` 工具转发 `expected_revision`（CAS 键名与 handler 一致）。
- SKILL 文档：修正跑不通的 arbitrate/patch 命令示例，补 approve/
  update_goal 的 user-only 指引。

## [0.1.0a2] - 2026-09-05

Deadline 可靠性与合同防篡改（PR #10，分支 hardening/deadline-contract-guards，
GitHub CI 三平台实测通过）。

### Security

- `contract/patch` 现在要求 Principal（user）——模型客户端修订合同一律
  AUTH_FAILED（RPC-C1 审查的遗漏面）。
- `budget` 加入 FROZEN_FIELDS：patch 试图改预算从静默忽略升级为显式拒绝。

### Fixed

- 过期的 `next_decision_at` 原样返回（调用方钳 0 立即唤醒）：一个 blocked
  合同的过期决策点不再让 daemon 回退整周期休眠、吞掉 active 合同的未来
  唤醒点——deadline 唤醒稳定性。
- 档 3（steer）无活租约时立即重派（DESIGN §6.2）：deadline 剩余窗口不再
  浪费在无会话可干预的空提醒上——deadline 灵活性；预算不足如实交用户。
- 验证预算耗尽 → 合同转 `blocked(need-user)`：daemon 不再把不可验收的
  合同一轮轮重派烧光执行预算（防破坏性跟进）。

## [0.1.0a1] - 2026-09-05

安全加固补丁：发布后四域深度审查（进程/租约、持久化、RPC/MCP 边界、调度
预算）发现的 9 项 Critical 修复，全部附回归测试。详见
[security-hardening-2026-09-05](docs/evidence/security-hardening-2026-09-05.md)。

### Security

- `contract/approve|pause|resume|cancel|arbitrate` 现在要求 Principal
  （actor=user）——模型客户端自批准合同返回 AUTH_FAILED 并指引 CLI 通道
  （SPEC §4.2）；此前模型可起草+自批准+指定验收命令形成任意命令执行链。
- `goal/prepare` 与所有入口的 contract_id 统一为安全 slug 校验（拒绝路径
  分隔符/`..`），堵住投影写到数据根之外的路径穿越。
- 执行者 `attempt/write-back` 不得把合同直推完成态（STATE_FORBIDDEN）；
  完成结论只能由 verifier 裁决路径写入。

### Fixed

- `ensure_schema` 的一次性历史改写（complete→satisfied 等）加版本门控，
  不再每次启动改写 daemon 刚写入的合法终态；tick 终态跳过集合改用
  `TERMINAL_STATES`，已完成合同不再被重新仲裁成 EXPIRED。
- 真实 v1 结构库的平滑升级（迁移列索引移到补列之后 + events 缺列兜底）。
- `write_back(events=())` 形态补 request_id 簿记事件，幂等重放不再二次
  执行状态迁移。
- verifier 派发不再抢夺在跑 executor 的活租约（dispatch/deferred 推迟），
  且纳入 kill switch 管辖。
- workspace 排他归一化升级为 realpath + casefold，大小写/符号链接别名
  不再绕过。
- `poll_attempts` 对 fenced 租约/缺失 generation 防护，单个 attempt 的
  租约异常不再击穿 daemon 进程。

## [0.1.0a0] - 2026-09-05

首次公开发布（Developer Preview）。内部开发者预览切割于 2026-09-01；
2026-09-05 完成产品定位统一、发布阻断项修复（B1/B2）与候选制品验证后发布。

### Changed

- Product name set to **限期合同中枢 / Deadline Contract Hub** (ADR-004 revision);
  "multi-agent task contract runtime" is retained as the category description only.
- Position LHGP as a multi-agent task contract runtime with an LLM-independent
  scheduling core; retain protocol names, package identity and compatibility.
- Align English/Chinese README and specification framing; distinguish multiple
  contracts and submitted-plan progression from optional future semantic planning.
- Replace the incomplete automatic-execution quickstart with an isolated,
  non-LLM contract lifecycle smoke. Add ADR-004, an executable release-first plan
  and an audit recording known blockers. This is documentation work, not a
  runtime fix or a new release.

### Added

- MCP tool safety annotations (`readOnlyHint`, `destructiveHint`,
  `openWorldHint`) and strict JSON argument validation.
- Read-only `notifications` CLI audit command with status/limit filters and
  goal filtering and opt-in payload output; the MCP notification tool remains
  payload-redacted by default.
- LHGP dual-track entrypoint polish (`lhgp --version`, `lhgp-mcp`) and updated
  plugin/roadmap documentation.
- Release artifacts now validate embedded plugin metadata (strict SemVer,
  `lhgp` identity, and canonical `lhgp-mcp` command) during CI.
- Canonical `lhgp` Python namespace facades now cover the runtime layers,
  while legacy `longtask` imports remain compatible during migration.
- Persistence schema helpers and notification outbox exports are now explicit
  under `lhgp.persistence`, with lazy loading preserved for legacy import order.
- Canonical RPC and persistence package APIs now expose stable protocol,
  schema, event, notification, and executor-side entry points with lazy
  loading where legacy import order requires it.
- `contract/get` now exposes contract-scoped `decision_history` and
  `attempt_history`, with bounded `decision_limit` / `attempt_limit` controls
  across RPC, CLI, MCP, and model skills.

### Fixed

- POSIX liveness probes now recognize zombie processes via `/proc` state and
  reap daemon children with `waitpid(WNOHANG)` before escalating a stop to
  SIGTERM, so daemon shutdown is graceful again on Linux instead of always
  being reported as forced.
- Linux process start times now come from `/proc/<pid>/stat` field 22 (stable
  after exit) instead of `st_ctime` (which changes when a process exits), and
  `reattach` binds provably-terminated runs whose pid has been fully reaped,
  per the documented branch-2 settlement semantics.
- CI runs the gate on macOS as a known-gap, non-blocking platform for this
  preview (the identity model has no `/proc` equivalent there yet).
- CLI and MCP `--help` no longer crash with `UnicodeEncodeError` on non-UTF-8
  consoles (e.g. cp1252); unencodable help characters are replaced.
- The `executor/health` integration test no longer depends on a
  machine-installed `codex` CLI (hermetic fixture uses the interpreter).
- mypy analysis is pinned to the Windows platform (the authoritative
  typecheck platform) so the three-OS CI matrix typechecks identically, and an
  unused Rust cache step was removed from CI.
- Timeout cancellation failures now orphan the attempt and retain its lease for
  reconcile grace/fencing instead of releasing execution control before the
  external process is known to have stopped.
- Verifier `attempt/started` events no longer consume the executor
  `max_dispatches` budget; execution and verification budgets remain separate.
- Detached daemon and subprocess test lifecycle cleanup, including parent
  pipe closure after child exit.
- MCP canonical aliases now retain their safety annotations and report the
  correct server identity when launched through `lhgp-mcp`.
- Schema startup reconciliation now repairs partially upgraded v2 databases,
  including missing external-handle capability columns, before daemon scans.
- Doctor output and README quickstart commands now use the canonical LHGP name.
- Claims evidence is re-anchored to the latest verified implementation commit;
  the full 7-gate suite remains green with 574 tests at 81.47% coverage.
- Unavailable L1 wakeup channels now emit one deduplicated `wakeup/degraded`
  event per daemon lifecycle instead of writing noise on every tick.
- Failed wakeup task disarms remain tracked and are retried on the next daemon
  tick, preventing stale OS scheduler entries after transient errors.
- dogfood v5 now preflights `request-verification` for blocked deliveries and
  falls back to a revision contract only after an explicit protocol refusal.
- Verification request consumption is now recorded by request event ID, so a
  terminal verifier cannot cause the same user request to be dispatched again.
- `contract/get` and MCP `get_contract` now expose bounded verification history,
  making request, consumption, and verifier-start state directly model-readable.
- `lhgp doctor` now checks launch executables for enabled registry entries and
  reports actionable missing-CLI diagnostics before dispatch.
- MCP now exposes read-only `lhgp_doctor` / `longtask_doctor` aliases so models
  can run the same preflight diagnostics before selecting an executor.
- L1 RTC wakeup targets now use the earliest available decision, wakeup, or
  deadline-safety point; a later safety margin can no longer postpone an
  earlier `next_decision_at`, and past decision points are clamped to an
  immediate wakeup.
- The Windows L1 adapter now arms one-shot Task Scheduler entries that call
  authenticated `daemon/wake`; fired signals are audited and consumed before
  the next decision tick, while non-Windows platforms remain explicit
  `wakeup/degraded` fallbacks. Its date serialization follows the actual
  Windows `schtasks.exe` `yyyy/mm/dd` parser, verified by a real create/query/
  delete smoke.
- L1 RTC wakeup targets now use the earliest available decision, wakeup, or
  deadline-safety point; a later safety margin can no longer postpone an
  earlier `next_decision_at`.

### Developer Preview release (2026-09-01 cut)

The reference implementation is now feature-complete for the scope declared
in [DESIGN.md v0.7](DESIGN.md). All 7 quality gates (`format / lint / arch /
deps / claims / typecheck / test+coverage`) pass on Windows; CI matrix
expands to ubuntu + macOS.

#### Protocol surface (DESIGN §5, §11.1)

- 24 JSON-RPC methods over the control plane, dispatched through
  `src/longtask/rpc/handlers.py` + `src/longtask/rpc/executor_api.py`.
- MCP server thin layer (`longtask-mcp` script entry, `src/longtask/mcp_server.py`)
  exposing core `longtask_*` tools plus LHGP aliases and audit/control
  extensions to any MCP-compatible agent harness.
- §17 `longtask-contract` skill (`skills/longtask-contract/SKILL.md`) teaching
  models how to draft contracts, write handovers, and avoid common pitfalls.

#### Daemon lifecycle (DESIGN §3.3, §15.2)

- `longtaskd` start / stop over a real detached subprocess (Windows:
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, POSIX: `start_new_session`).
- Graceful stop via `daemon.stop` flag, hard kill only on grace expiry.
- `longtask-mcp` (stdio JSON-RPC 2.0) ships as the model-facing companion.

#### Attempt lifecycle (DESIGN §3.4, §5.1, §5.2, §7, §10)

- `AttemptRunner` (`src/longtask/cli/runner.py`) drives `start_attempt` +
  `poll_attempts`: prepare re-validation, spawn, heartbeat lease renewal,
  terminal collect, and stale marking.
- Dead-lease reclaim path on redispatch (§7): `reclaim_lease` not bare
  `acquire_lease`.
- §5.2 cross-check verifier: `_dispatch_verifier` auto-derives a verifier
  attempt (`role: VERIFIER`) with a candidate ≠ executor; the
  `_judge_verifier_outcomes` tick-end hook then promotes
  `contract.completed` on verifier `attempt/succeeded` or sends the
  contract back to `ACTIVE` on `attempt/failed`.

#### Ephemeral context (DESIGN §4.1)

- `compile_context_snapshot` materializes `context/attempts/<id>/active.md`
  and `scratch.md` per attempt; the `ContextPolicy` parses
  `contract.context.{required,max_bytes,expires_after_minutes}`.
- Capacity contract is fail-closed: overflow raises `CapacityRefusedError`
  and persists `context/capacity-refused` before refusing attempt start.
- Handover digest is auto-merged into `task_prompt` so re-dispatched
  attempts inherit verification-failure context (closes the
  v1 real-run "second attempt wasted" gap found in internal runs).

#### Persistence, budget, contracts

- Budget hardness (§6.3) via `attempt/started` event count, not a static
  draft field. `max_dispatches` consumed on every successful dispatch;
  exhausted → tier 5 `blocked(need-user)`.
- Frozen-zone immutability enforced on `patch`; only `soft_guidance` /
  `acceptance` / `workload_estimate` are mutable.
- `request_id` idempotency on every state-mutating RPC (§11.3).

#### Executor-side RPC (DESIGN §11.2)

- `attempt/status` returns the attempt's event history + lease posture.
- `lease/renew` renews with fencing (lease gen / holder match); mismatches
  return `LEASE_FENCED`.
- `attempt/write-back` writes progress + attempt state with the same
  fencing; `request_id` is honored for retry semantics.

#### Layered wakeup (DESIGN §6.4, ADR-0002)

- L0 power guard via `SetThreadExecutionState` on Windows, port-injectable.
- L1 plan-task registration with `max(next_wakeup, deadline - margin)`.
- New event types: `wakeup/sleep-guard`, `wakeup/rtc-armed`,
  `wakeup/rtc-fired`, `wakeup/degraded`.
- L2 / L3 require external infrastructure and remain `accepted_debt` (see
  `quality/claims.json#strict-deadline-wakeup-design`).

### Known limitations (accepted debt, not blockers for v0.1.0a0)

- L2 / L3 wakeup not implemented (cloud + relay, external infra).
- Headless harness subprocess lifetime vs. `Popen` handle alignment
  (observed in internal real runs).
- `control/spawn` is vocabulary-only; external RPC to `AttemptRunner`
  not yet exposed.
- Agent harnesses (Claude Desktop etc.) each need their own `lhgp-mcp`
  registration in their MCP client config — out of protocol scope.

### Documentation & examples

- Internal real-execution archives (raw event chains, cross-check
  verifier catching a wrong unit-test assertion, and an 8-step MCP
  lifecycle trace) were retained privately during the preview period.

### Internal (commit-level highlights)

- `911862d` — §4.1 ephemeral context
- `2b34348` — executor-side RPC
- `84f0b46` — §5.2 verifier auto-dispatch
- `7405173` — §17 skill
- `79c0f78` — MCP server thin layer
- `5 prior commits` (daemon lifecycle, layered wakeup, end-to-end runs)
