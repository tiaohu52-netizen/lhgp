# 远期任务协议（Long-Term Task Protocol）设计文档

> 文档修订：v0.13（stdout 通道编码入规范：§12.1 声明完成事件行与 lhgp-verdict 判定块 MUST 为 UTF-8，适配器 spawn 时注入 PYTHONIOENCODING=utf-8）
> 协议版本：v0.1（草案；线协议与 schema 版本独立于本文档修订号）
> 日期：2026-09-11
> 状态：设计阶段；v0.8 把 §14.1 里两条原本只是「宣称」的威胁处置——提示词注入污染交接、注入 shell 命令——升级为可验证的结构（围栏 token 由正文摘要派生，正文无法提前闭合），并补上取消时的进程树终止。线协议、schema、错误码均不变。
> 定性：**agent 外协议**。不随会话存在而存在，不因会话消失而消失。会话是燃料，不是容器。

---

## 1. 一句话定位

一份存活的合同 + 一个跨会话的推动者。用户提前声明目标、约束、禁令和 Deadline；协议在盘上持久保存合同，按紧迫性主动寻找、唤醒、干涉、加派**任何可用的 agent 应用**推进目标，直到完成、预算耗尽或 Deadline 裁决。

与「闲时任务」的分界：闲时任务是机会主义的（有空就做），远期任务是合同制的（必须做完，且越拖越急）。

### 1.1 本质定性：harness 之外的 harness

**本协议在体系结构上是一个外置 harness，搭载于各 harness 之上，而非某个 harness 的插件。** 现有 Agent 应用（agent-cli、Codex、Hermes、Qwen 等）本身就是完整的 harness：它们管理模型上下文、沙箱、工具集、审批和会话生命周期。远期任务协议不从属其中任何一个；它把这一批 harness 统一抽象成可替换的执行资源池，自己只做一件事：把一份持久合同推进到底。

```
┌──────────────────────────────────────────────────────────────┐
│ 远期任务协议（本层：合同 + 调度 + 租约 + 推动，不含模型）        │
│   longtaskd：持久状态、Deadline 仲裁、升级阶梯、预算           │
├──────────────────────────────────────────────────────────────┤
│ 插件层（挂载点，每个 harness 一个薄适配器）                     │
│   agent-cli bridge │ Codex subprocess │ Hermes bridge │ …          │
├──────────────────────────────────────────────────────────────┤
│ 各 harness / Agent 应用（被搭载者，能力被声明、被调度）          │
│   每个 harness 自己管自己的模型上下文、沙箱和工具                │
└──────────────────────────────────────────────────────────────┘
```

这个外置 harness 与内部 harness 的分工：

- **不依靠模型**：合同状态、Deadline、租约、预算、事件溯源全部由独立守护进程持久化，没有模型也能跑（能拒接、能记账、能裁决）。会话死了，合同还活着——这正是「外置」的意义。
- **一定暴露给模型**：协议主动把一组工具和能力（`context/refresh`、`context/promote`、`attempt/status`、结构化进度文件、交接文件写入、升级请求）注入当前执行者的上下文，让模型能读取合同、更新进度、请求刷新上下文、提出验收。模型不需要知道守护进程怎么实现，但必须能用它。
- **插件挂载**：每个 harness 一个薄适配器，负责把合同翻译成自己的入场提示词、沙箱参数和控制面。适配器不复制合同状态，只做接线。

#### 与自动化任务的根本区别

本协议与 cron 任务、CI 流水线、定时脚本不属于同一物种：

| 维度 | 自动化任务 | 远期任务协议 |
|------|-----------|--------------|
| 目标来源 | 脚本里写死的固定动作 | 模型与用户谈判出的合同条款 |
| 执行者 | 固定脚本/工作流 | 任意已注册 Agent harness，可换可加派 |
| 智能 | 零智能，出错即停或重试 | 每个执行者都是一个 LLM Agent，可规划、判断、写交接 |
| Deadline | 触发即执行，不考虑后果 | 紧迫性驱动升级阶梯，Deadline 到期转人工裁决而非硬杀 |
| 任务规模 | 分钟级、脚本级 | 可承担跨越数日、需要多阶段多执行者接力的大型任务 |
| 完成定义 | 退出码 0 | 合同验收条款通过，带证据 |

一句话：**它是能让任意 Agent harness 在 Deadline 前主动推进大型长期目标的外置协议**；自动化是脚本在做，远期任务是 Agent 在合同约束下做。

## 2. 核心定性（五条公理）

1. **合同外置**：合同、进度、交接状态全部活在文件系统里，不依附任何会话、进程或 harness。会话死了，合同还活着。
2. **会话即燃料**：任何 agent 会话只是被协议临时雇来的执行者，用完即弃、随时可换。续跑的连续性来自盘上交接文件，不来自上下文窗口。
3. **协议不假设执行器能力**：对执行器只有两条最低门槛——命令行可拉起、读得懂入场提示词。其余能力（可干涉、沙箱、计划模式）全部作为**能力声明**注册，由适配器翻译。
4. **约束要么兑现要么拒接**：合同携带的硬约束（禁令、路径边界、Deadline）是声明式的。执行器要么能完整翻译成自己运行时的强制面，要么必须**拒接**这次分发——禁止降级执行。这是「合同」与「愿望」的分界线。
5. **不靠模型但必暴露给模型**：协议自身的持久化、调度、租约和裁决不依赖任何模型在线即可成立；但执行中的模型必须能使用协议提供的上下文、进度和提升工具，否则续跑和协作无从谈起。

## 3. 四个平面

```
┌─────────────────────────────────────────────────────┐
│ 推动层 Promoter（紧迫性引擎）                          │
│  升级阶梯 + 工作租约 + 执行器池调度                     │
├──────────────┬──────────────────────────────────────┤
│ 调度层        │ 执行层（适配器，可插拔）                  │
│ ticker/唤醒源 │  agent-cli / codex / hermes-subagent / …    │
├──────────────┴──────────────────────────────────────┤
│ 持久层 Persistence（唯一永生的东西）                    │
│  合同目录 + 交接文件 + 执行器注册表                      │
└─────────────────────────────────────────────────────┘
```

上下文投影是横切能力，不是第五个业务平面：由持久层按版本重建，由执行层装配，由推动层按容量和准入合同提供给当前 attempt。

### 3.1 持久层（协议核心资产）

一个由 SQLite/WAL 权威状态和可读文件投影组成的合同目录。数据库保存合同索引、修订、attempt、租约和事件；文件用于模型读取、交接、人工编辑和故障排查。所有文件投影都能从数据库事件重建，不能绕过协议直接改数据库或把文件改动当成已提交状态：

```
~/.longtask/
  state.db                 # SQLite/WAL：合同索引、租约、attempt、事件的事务权威
  contracts/
    <contract-id>/
      contract.yaml        # 合同内容投影（通过协议修订，不直接改权威状态）
      handover.md          # 交接文件：下一个执行者读到的全部状态
      task_plan.md         # 阶段计划（planning-with-files 格式）
      progress.md          # 会话日志投影
      findings.md          # 中间发现投影
      context/
        policy.yaml        # 守护进程拥有的上下文准入合同（只读投影）
        stages/            # 每个阶段的持久摘要投影（只读投影）
          <stage-id>.md
        attempts/
          <attempt-id>/
            active.md      # 本次 attempt 的只读上下文快照，可重建
            scratch.md     # 本次 attempt 的可编辑临时区，可丢弃
            requests/      # 显式提升/刷新请求投影
      attempts/
        <attempt-id>.json  # 执行尝试的不可变身份和结果投影
      lease.json           # 当前租约投影（权威值在 state.db）
      log.jsonl            # 人类可读事件投影（权威事件在 state.db）
  registry.yaml            # 执行器注册表（用户框定的池子）
  config.yaml              # 全局配置：唤醒源、预算默认值、通知渠道
```

**设计依据**：Hermes cron 的 `jobs.json`（盘上文件 + 跨进程文件锁）已验证这种形状在 Windows 上可长期运行；planning-with-files 技能的 task_plan/progress/findings 三件套就是现成的交接文件格式。

#### 交接文件最低字段（handover.md）

「下一个素不相识的执行者能无缝续跑」是本协议的核心卖点，因此交接文件不能是自由散文。`handover.md` 每次写回必须包含以下区块（缺块的写回视为不合格进度，记录 `handover/incomplete` 事件但不阻断，连续缺块升级为 blocked）：

| 区块 | 必填 | 内容 |
|------|------|------|
| `current_stage` | 是 | 当前所处阶段 id 与阶段目标一句话 |
| `completed_evidence` | 是 | 已完成事项清单，每条带证据指针（文件路径 + 锚点/hash），不许只写「已完成」 |
| `remaining` | 是 | 未完成项清单，按建议顺序排列 |
| `estimate_remaining_hours` | 是 | 剩余工作量估算（小时），驱动紧迫度公式 |
| `next_action` | 是 | 下一个执行者开工第一步应做的事 |
| `constraints_digest` | 是 | 本合同禁令摘要（deny_paths、网络、进程策略），防新执行者不知情越界 |
| `open_risks` | 否 | 已知风险与未决假设 |
| `source_attempt_id` | 是 | 本份交接由哪个 attempt 写出，配合租约 generation 防旧会话冒充 |

`task_plan.md` 沿用 planning-with-files 的阶段结构；`progress.md` 是追加式会话日志投影。三者分工：plan 回答「路怎么走」，progress 回答「已经走到哪」，handover 回答「下一个人从哪接着走」。

#### 人类编辑与权威状态的门

`contract.yaml`、`handover.md` 等文件可被用户直接编辑，但**盘上的直接改动是未提交草稿，不是已提交状态**：

- 用户改了 `contract.yaml` → 守护进程检测到投影与库内版本不一致，标为 `dirty`，该合同暂停分发，直到用户通过 `contract/patch`（带 `expected_revision`）把改动正式提交，或 `contract/revert` 丢弃草稿以库为准重建投影。
- 用户改了 `handover.md` / `progress.md` → 提交路径是显式的「人工提升」：客户端调用 `context/promote` 并声明 `actor: user`，经原子校验后才进入权威历史。
- 直接改 `state.db` 永远不被承认；守护进程启动时校验库完整性，发现外部写入痕迹即拒绝启动并记录 `store/tampered`。
- 规则一句话：**文件是人机共读的界面，提交只有协议一个入口**。投影可以落后（可重建），绝不能超前。

### 3.2 临时上下文投影（Context Projection）

临时上下文解决的不是“把更多历史塞给模型”，而是让每个新执行者在有限窗口内获得**当前阶段真正需要的最小认知入口**。它是从合同、阶段摘要、进度和经批准的发现中编译出来的短投影，不是第二份任务真相源。

```
合同真相 + 阶段摘要 + 最新进度 + 获准发现
                    │
                    ▼  按 context policy 准入、裁剪、标注来源
     context/attempts/<attempt-id>/active.md
                    │
                    ▼  注入本次 attempt 的模型上下文
```

**可编辑不等于可改合同**：模型可编辑的是 `scratch.md` 工作区块（当前焦点、假设、下一步、风险和交接备注）；`active.md` 中的合同锚点、验收标准、禁令、阶段身份和来源标识是只读的。临时编辑在上下文过期或 attempt 结束时失效；只有显式执行“提升”（promote）才会写入 `progress.md` 或某个阶段摘要，并保留来源和 attempt id。

上下文准入规则本身也是合同的一部分。它至少声明：

- **准入源**：哪些合同字段、哪些阶段摘要、哪些进度片段和哪些 findings 可以进入
- **选择规则**：当前阶段、依赖阶段、最近更新或用户点名的材料如何筛选
- **优先级**：合同锚点高于阶段摘要，阶段摘要高于模型工作笔记；冲突不能靠后写内容覆盖
- **容量与新鲜度**：最大字节/token、摘要最长年龄、刷新时机和失效时间
- **可编辑范围**：允许模型修改的区块；只读区块以及禁止提升的内容
- **敏感信息规则**：密钥、凭证、其他合同和被排除路径永不因“相关”而自动进入
- **重建规则**：`active.md` 丢失、过期或来源版本不匹配时如何重新物化

上下文投影可由任意适配器使用；适配器只负责把它装配进目标 Agent 的上下文，不得自行扩大准入范围。若合同将上下文标记为 `required: true`，没有能力兑现该上下文合同的执行器必须拒绝接手；只有 `required: false` 的可选上下文才允许不带上下文启动，并记录原因，不能静默假装已装配。

### 3.3 调度层

一个 ticker（常驻线程或守护进程），只做三件事：扫合同状态、盯 Deadline、决定何时触发推动层。**它不执行任何任务。** 最外层用 Windows 任务计划程序保活——ticker 本身死了也会被系统拉起，形成两层唤醒保底。

调度器必须区分三种时间：`deadline_at`（合同声明的墙钟截止点）、`next_wakeup_at`（下一次推动检查点）和 `arbitrated_at`（实际做出裁决的时间）。调度器睡眠、重启或关机期间不能伪造“已经推进”；恢复后只根据当前墙钟和盘上状态进行一次可回放的仲裁。

### 3.4 执行层（适配器）

每个 agent 应用一个薄适配器，负责三件事：

1. **入场**：把合同 + 交接文件打包成该 agent 的入场提示词，shell 拉起（headless 或新会话）。
2. **翻译**：把合同的声明式约束翻译成该运行时的强制面（沙箱模式、审批策略、路径拒绝清单）。翻译不了 → 拒接。
3. **回报**：把执行结果写回交接文件，释放租约。

计划模式不再是协议的假设，而是**适配器的一次入场仪式**：目标 agent 有计划模式就激活它（先出计划过审批再动手），没有就用入场提示词模拟一个等价的「先规划、呈交、再执行」结构。

### 3.5 推动层（Promoter，紧迫性引擎）

见 §6 升级阶梯与 §7 租约。这是「远期」区别于「闲时」的心脏。

## 4. 合同 Schema（contract.yaml）

```yaml
schema_version: 1
contract_id: lt-20260831-001
title: 整理某设定文档第三卷的力量体系对照表
# 注：本例为脱敏规范示例。具体工作区路径、禁令清单随部署环境
# 由用户在立约时填写；协议不内置任何项目名或路径。

# ── 冻结区（创建后不可修改，修改 = 作废重立）──────────
objective: |          # 目标：一段人类可读的完成标准描述
  产出第三卷全部登场角色的力量体系对照表，
  以 markdown 表格交付到 指定路径，覆盖原文全部提及。
deadline_at: "2026-09-05T23:59:59+08:00"   # 合同墙钟截止点，显式时区
hard_constraints:      # 硬约束：适配器必须能翻译，否则拒接
  file_effects:
    mode: workspace-write
    workspace_root: "~/projects/lore/volume-3-power-table"
    deny_paths:
      - "~/reference-library/**"    # 示例：只许参考，禁止写入/回流
      - "~/projects/other-novel/**" # 示例：跨项目隔离
  network:
    mode: deny
  process:
    mode: restricted
  package_install:
    mode: deny

# ── 可修订区（修改需过用户审批门，修订号递增）─────────
soft_guidance:         # 软指引：注入提示词，模型读了照做
  style: 保持原文术语，不发明新设定
  reference_only: 参考材料只学技法，不混入设定正文
acceptance:            # 验收标准：合同条款的一部分——
  standard: |          # 由模型起草、用户裁定的自然语言标准
    对照表覆盖第三卷 47 章全部力量描写，
    术语与原文一致，无参考材料内容直接引用，
    表格可直接并入设定集而不需返工。
    （立约时模型与用户逐条谈定；修订走审批门，修订号 +1）
  checks:              # 双方谈定后固化的核对清单（可核对项）
    - 文件存在于指定路径且为合法 markdown 表格
    - 覆盖第三卷 47 个章节的全部力量描写
    - 无参考材料内容直接引用
  # 验收标准的主权在模型与用户。工具不定死任何标准，
  # 只携带一个说明性 skill（见 §17）教模型怎么起草和核对条款。
context:
  policy_ref: context/policy.yaml
  required: true       # 本合同是否要求执行器装配该临时上下文
execution:
  required_capabilities: [spawn, observe, cancel, context]
  allowed_control: [notify, followup, steer]
  allow_new_session: true
  allow_parallel: false
  acceptance_mode: contract_defined
  deadline_policy: best_effort
  # live session 干涉必须由合同明确授权，不能从执行器能力自动推导
  interference_scope: user_selected_sessions
workload_estimate:     # 工作量预估（执行中滚动修正，驱动紧迫度）
  initial_hours: 6
budget:                # 开销上限：推爆就地转 blocked
  max_dispatches: 8        # 最多自主分发次数
  max_escalations: 3       # 最多另起会话/加派次数
  max_concurrent_attempts: 1
  max_attempt_minutes: 90
  max_output_bytes: 1048576

revision: 3            # CAS 修订号，每次获准修改 +1
created_by: user
```

**冻结区 vs 可修订区**是这份 schema 的关键切分：Deadline、禁令、沙箱边界立合同时就钉死——紧迫性永远不能升级它们（§6.3 硬边界）；目标描述、验收标准、软指引可以改，但每次修改过用户审批门并递增修订号（学 agent-cli goal 的 `goal/change` 事件 + CAS）。

### 4.1 临时上下文准入合同（context/policy.yaml）

临时上下文是给当前执行者的**认知工作集**，不是第三份任务真相。它由合同、各阶段摘要、最新进度和获准发现编译出来；模型可以编辑其中的工作区，但不能直接改合同锚点或阶段摘要原文。

模型看到的是一份逻辑上下文，物理上拆成两部分：

- `context/attempts/<attempt-id>/active.md`：协议根据准入合同为**该次 attempt** 生成的阶段摘要和合同锚点快照，系统维护，带来源版本和过期时间。attempt 之间互不共享，避免并行执行者读写同一份快照。
- `context/attempts/<attempt-id>/scratch.md`：该 attempt 的临时可编辑区，保存当前焦点、假设、下一步、风险、问题和交接备注；attempt 结束后可以丢弃。

这种拆分不改变用户想要的体验：适配器可以把两者组合成一个上下文入口交给模型；拆分只是为了防止模型把派生摘要误写成持久真相。

`context/policy.yaml` 是主合同的受控子文档，有自己的 `policy_revision`。它由模型和用户共同确定准入语义，工具只负责按已批准的规则物化、限额、过期和审计，不替双方决定“什么内容才算相关”。

```yaml
schema_version: 1
contract_id: lt-20260831-001
policy_revision: 1
required: true
sources:
  contract:
    include: [title, objective, deadline_at, hard_constraints, acceptance]
    mode: readonly
  stages:
    include: all_summaries
    mode: readonly
    max_summary_bytes_each: 2400
    max_total_bytes: 12000
  progress:
    include: [latest, unresolved_risks, next_step]
    max_age_hours: 24
  findings:
    include: approved_only
    max_items: 12
selection:
  current_stage_first: true
  dependency_depth: 1
  overflow: compact_all_stage_summaries
limits:
  max_bytes: 24000
  max_tokens_hint: 6000
  refresh: [attempt_start, stage_complete, explicit_request]
  expires_after_minutes: 240
editable:
  file: context/attempts/<attempt-id>/scratch.md
  sections: [current_focus, working_hypotheses, next_actions, risks, open_questions, handoff_notes]
  promotion: explicit_only
  require_source_attempt_id: true
redaction:
  never_include: [secrets, credentials, other_contracts, denied_paths]
```

#### 阶段摘要格式

每个阶段只向上下文提供经批准的摘要，不把该阶段的完整 transcript（文本记录）重复塞进模型窗口。摘要至少包含：

```yaml
stage_id: research
stage_revision: 3
status: complete
objective: 明确资料范围和可验证结论
summary: |-
  已完成资料盘点，留下两项待核对假设。
open_risks:
  - 某来源的时间范围仍需复核
artifacts:
  - findings.md#source-map
source_attempt_ids: [attempt-004, attempt-006]
promoted_by: attempt-006
```

`include: all_summaries` 表示每个阶段的摘要都应有入口；若总容量不足，协议必须按 `overflow` 规则把各阶段摘要压缩成有来源标记的 digest，不能静默丢掉某个阶段。压缩后仍无法满足 `required: true` 的容量合同，则拒绝启动 attempt，并记录 `context/capacity-refused`。

#### 编辑、刷新与提升

- 模型只能通过现有文件工具编辑本次 attempt 的 `scratch.md` 允许区块；直接写入 `active.md`、`stages/` 或主合同的写入应被拒绝并记录。
- `active.md` 在 attempt 开始、阶段完成或合同明确要求刷新时重建。来源版本变了但快照未刷新时，适配器不得声称模型看到的是最新上下文。
- scratch 的内容是临时工作记忆。要进入 `progress.md` 或 `stages/<stage-id>.md`，模型必须提交一个带 `source_attempt_id`、目标阶段和证据引用的显式提升请求；请求可以通过现有文件工具写入约定的 request 文件，不新增模型专用工具。
- 推动者或持久层对提升请求执行原子校验：校验通过后追加上下文事件并替换摘要，失败则保留 scratch、拒绝部分写入，并记录原因。
- attempt 结束、租约回收或合同过期时，该 attempt 的 scratch 默认失效；其 `active.md` 可以保留用于审计，但 `expires_at` 之后不能再作为有效上下文注入。

上下文相关事件至少包括：

```text
context/policy-approved
context/snapshot-built
context/snapshot-expired
context/scratch-updated
context/promotion-requested
context/promotion-accepted
context/promotion-rejected
context/capacity-refused
context/rebuilt
```

事件只记录来源、版本、哈希、attempt id 和结果，不把整份上下文复制到每次模型请求日志。`active.md` 丢失、过期或来源版本不匹配时，协议必须先重建；重建失败且 `required: true` 时，attempt 不得启动。

## 5. 状态机

```
           用户审批通过
  drafted ────────────► active ──────► complete
                          │ ▲              ▲
        用户暂停/恢复      │ │ 续跑          │ 验收通过
           ◄─ paused ─────┘ │              │
                          ▼ │              │
                       blocked ────────────┤
              （原因码：need-user / lease-dead /
                budget-exhausted / constraint-refused /
                no-executor / acceptance-failed）
                          │
                          ▼
                       expired ◄── Deadline 裁决（不硬停，
                          │        保住中间成果，转人工裁决）
                          ▼
                       archived ◄── cancelled（用户主动终止，
                                           可从任意非终态进入）
```

- **blocked** 是唯一的「因问题而停」状态，带 lower-kebab-case 原因码 + 人类可读说明（学 agent-cli goal 的 GoalBlockReason）。
- **cancelled** 是用户主动终止的终态：进入后不再接受新 attempt，持有租约的 attempt 收到停止信号并按 cancelled/stale 结算，中间成果保留在交接文件中。
- **expired ≠ 作废**。Deadline 到期时若工作未完成：执行者收到停止信号，交接文件锁定快照，合同转 expired 等用户裁决（采纳部分成果 / 延期 / 作废）。长任务最贵的产物是不完整的中间成果，硬停等于全部作废。
- 每次迁移都是 `log.jsonl` 里的一条事件，只追加，可回放。

### 5.1 合同状态与执行尝试状态分离

合同状态回答“这件事整体走到哪一步”；`attempt` 状态回答“某个执行者这一次运行发生了什么”。两者不能合并成一条状态机：

```text
Contract: drafted → active → paused / blocked / complete / cancelled / expired → archived
Attempt:  admitted → running → waiting → succeeded / failed / cancelled / stale
```

- 一个 active 合同可以先后拥有多个 attempt；attempt 失败或租约失效不等于合同失败。
- `complete` 只表示合同按其 acceptance 条款被接受；执行者退出码为 0、输出非空或模型口头声称完成，都不能单独触发它。
- `expired` 是 Deadline 仲裁结果，不是某个 attempt 的退出码。合同过期后不得再启动普通 attempt；延期必须产生新的合同修订或新的合同，不覆盖原始 Deadline。
- 每个 attempt 都有不可变的 `attempt_id`、`executor_id`、`session_ref`、`lease_generation` 和 `started_at`。所有进度、上下文提升和控制动作都引用它们，避免旧会话的结果冒充新会话。

### 5.2 验收执行：默认另派核对 attempt（已定）

执行者自报完成不能触发 `complete`——自己考自己不算数。本协议默认采用**交叉核对**：

1. 执行 attempt 声明完成时，推动者派出一个 `role: verifier` 的新 attempt。verifier 默认只读工作区（`file_effects: read-only`），不继承执行 attempt 的会话上下文，只拿合同验收条款 + 交接证据指针。
2. verifier 逐条核对 `acceptance.checks`，产出结构化证据：每条 check 给出 `pass / fail / undeterminable` + 证据指针（文件路径、hash、行号或查询结果）。
3. 全部 `pass` → 合同转 `complete`，事件带 verifier 的 `attempt_id` 与证据集。存在 `fail` → 合同回到 active（紧迫度重算，通常直接高档），交接文件记录未过项。存在 `undeterminable` 且 verifier 声明无法进一步判定 → 升级 `blocked(need-user)` 由人裁定。
4. verifier 也计入预算（占 `max_dispatches` 一次）。合同可在 `acceptance` 上声明 `verifier: none` 选择「只在争议或 expired 时升级到人」，适用于低风险任务；未声明时默认 `verifier: cross_check`。
5. verifier 与执行者不能是同一 `session_ref`；预算允许时优先选不同 executor，防止同源盲区。

这一默认解决了「Deadline 前谁来跑 checks」：不是推动者自己定标准（它不拥有标准），而是把核对也变成一次受合同约束的、可审计的 attempt。

## 6. 推动层设计

### 6.1 紧迫度公式

```
urgency = 剩余工作量估算 ÷ 剩余时间
```

由「工作量滚动修正 + Deadline 倒计时」共同驱动，而非单纯墙钟。执行者在 progress.md 里每次更新剩余工作量估算；推动者据此把合同归入五个紧迫档。

### 6.2 升级阶梯（「想尽一切办法推进」的协议化）

| 档 | 紧迫度 | 动作 | 干涉强度 |
|---|---|---|---|
| 0 排队 | 余量充足 | 不打扰任何人，等合同自己的下次唤醒 | 无 |
| 1 提醒 | 余量收窄 | 向持有租约的活跃会话注入带倒计时的提醒轮次 | 轻推 |
| 2 转向 | 余量告急 | 对正在跑的会话 steer()，把话题扳到合同任务上 | 干涉 |
| 3 另起会话 | 无可用会话 / 现有会话磨蹭 | 从执行器池挑人，headless 拉起新会话，打包入场 | 接管 |
| 4 并行加派 | 单人仍不够 | 多执行者分头推进（需 §7 租约的分区机制）——**当前未实现**：机制未接线，`decide()` 不产出该档，停滞一律串行重派（见下方 §6.3 状态注） | 加派（预留） |
| 5 升级到人 | 预算内手段用尽 | 转 blocked(need-user)，带完整升级历史 | 交还 |

先例佐证：agent-cli schedule 的 followup() 证明了「往活跃会话注入提醒轮次」这条路是通的，`agent.steer()` 证明了「轮次进行中导向」是通的，Hermes cron 的 ticker 证明了「到点拉起新会话」是通的。升级阶梯 = 把三条既有通道按紧迫度编排。

#### 分档阈值（默认配置，可全局调整）

紧迫度 `u = 剩余工作量估算(小时) ÷ 剩余时间(小时)`。剩余时间 ≤ 0 时合同直接进入 Deadline 仲裁，不再走阶梯。默认分档：

| 档 | 触发条件 | 冷却约束 |
|---|---|---|
| 0 排队 | u < 0.25 | — |
| 1 提醒 | 0.25 ≤ u < 0.5 | 同一合同提醒间隔 ≥ 30 分钟 |
| 2 转向 | 0.5 ≤ u < 1.0 | 同一 session 每次 attempt 最多 steer 3 次 |
| 3 另起会话 | u ≥ 1.0，或无活跃租约且 u ≥ 0.5 | 消耗 1 次 `max_dispatches` |
| 4 并行加派 | **未实现**（设计保留）：触发条件是 u ≥ 1.0 且档 3 后估算连续两次未下降，但分区租约未接线，运行时**不产出**该档——停滞如实记为串行重派，理由自陈「parallel dispatch is not implemented」，且不消耗 `max_escalations` | 预留（当前不消耗任何预算） |
| 5 升级到人 | 预算任一项触顶，或档 4 后 u 仍 ≥ 1.5 | 立即，转 blocked(need-user) |

阈值和冷却都写在 `config.yaml`，属全局默认；合同级不可调（防止立约时「谈一个更急的梯子」架空预算纪律）。估算停滞的判定只信交接文件里带 `source_attempt_id` 的滚动估算，不信模型口头报时。

### 6.3 硬边界（升级阶梯永远不能碰的东西）

1. **禁令与沙箱边界**：推动者能升级的只有「谁来做、在哪做、做多急」。Deadline 再紧，deny_paths 一个字不松——否则合同不可信。
2. **预算**：另起会话与加派是真金白银的 API 调用。budget 耗尽 → blocked(budget-exhausted)，而不是无限加码。「想尽一切办法」的边界就是合同里写的数字，终极边界是用户本人。

### 6.4 跨关机语义与分层唤醒

机器关机跨过去的 Deadline，采用**仲裁时刻语义**：deadline 以 ticker 醒来后第一次扫描的墙钟为准裁决，不做追惩性补跑。开机后 ticker 先扫一遍全部合同，过期者统一转 expired。

仲裁时刻语义保住中间成果，但丢掉两样东西：机器睡着时 Deadline 前的剩余时间被浪费；机器关着过 Deadline 用户毫无知觉。为此引入**分层唤醒体系**（ADR-0002，四层各自独立可用，可渐进部署）。核心不变式：**唤醒源永远不是权威**——外部唤醒只能「推」（通知 + 唤醒信号），不能读合同内容、不能仲裁、不能写状态；仲裁仍只发生在 longtaskd 醒来后的首轮扫描。

| 层 | 名称 | 覆盖场景 | 机制与边界 |
|---|------|---------|-----------|
| L0 | 电源守卫 | 干活途中机器睡着 | active 租约存活或紧迫度 u ≥ 1.0 时持有系统电源请求（Windows `SetThreadExecutionState`）；事件 `wakeup/sleep-guard` |
| L1 | RTC/计划任务唤醒 | 睡眠（S3/现代待机） | 每个 active 合同在 `max(next_wakeup_at, deadline_at − safety_margin)` 注册带 wake 标志的 Windows 计划任务（复用 §3.3 保活通道，只加 wake 位）；S5 关机取决于 BIOS RTC alarm；事件 `wakeup/rtc-armed`、`wakeup/rtc-fired` |
| L2 | 云侧准时通知 | 关机跨 Deadline 的知晓 | 离线时**写前上传**：每次合同状态变更把未来 deadline 清单同步给云侧定时器，到点由云侧代发推送。数据最小化：上行仅 `contract_id`、`deadline_at`、通知渠道、scoped token；objective、禁令、交接内容永不上行。本地是权威，云侧是投影（§3.1 同构） |
| L3 | 常在线中继（可选） | 关机且想唤醒 | `longtask-relay` 跑在用户常在线设备（NAS/路由器/VPS），只做两个动作：到点推送通知、发 WoL magic packet；scoped token 仅授 notify + WoL，被攻陷的最坏后果 = 误唤醒/误通知，无法伪造合同状态 |

任一唤醒层离线/失效 → 记 `wakeup/degraded` 事件并降级保证声明（§11.4），绝不静默假装 strict。唤醒信号至少一次、可能重复，复用 `request_id` 幂等（§11.3）。实现排在 Developer Preview 之后（§16）。

## 7. 工作租约（同一合同不被写炸）

**问题**：现有会话在推进，推动者又拉了新会话，两边各基于交接文件干活，互相覆盖。

**机制**：执行者认领合同必须持租约（`lease.json`：持有者、心跳时间戳、超时阈值）。

- 租约活着 → 推动者只能提醒（档 1），不能接管、不能加派。
- 心跳断了（会话死、进程崩、机器重启）→ 租约自动回收，推动者才允许换人。
- **会话消失因此不是故障，只是租约到期**：下一个执行者凭盘上交接文件无缝顶上。这正是「会话即燃料」公理的机制化表达。
- 并行加派（档 4）**未实现**：设计上租约会细分为分区租约（合同工作分解成互斥分区，各租各的，交接按分区追加），但该机制尚未接线——`partition_id` 在所有派工处都取默认空值，`lease/partition-conflict` 与 `PARTITION_CONFLICT` 从未被写出。落地顺序见 ADR-005。
- **旧执行者隔离（fencing）**：每次租约变更都会让单调递增的 `lease_generation` 加一。执行者的一切写回（进度、上下文提升、结果、心跳）都必须携带自己的 `attempt_id` 与 `lease_generation`；generation 已过期的写回返回 `LEASE_FENCED` 并被丢弃，绝不污染新 attempt 的状态。这覆盖「A 卡死 → 租约回收 → B 接管 → A 苏醒继续写」的竞态。
- 与 agent-cli jobs 的本质差异：agent-cli 里 owner session 死了任务被取消（状态长在会话里）；本协议 owner 死了换人续跑（状态长在合同里）。

租约的权威状态在 `state.db` 的事务中变更（CAS：携带 `expected_generation`，冲突即失败重读）；`lease.json` 只是投影。跨进程文件锁（Hermes cron 已验证 Windows 上 msvcrt 的写法）仅用于投影写入和旧版本数据文件的互斥。

### 7.1 分区租约细则（档 4 并行加派）——设计已定，**尚未接线**

本节是**前置设计**：规则在此定稿、纯函数与单测已就位，但没有生产入口。
在 §6.3 的档 4 被真正实现（或正式取消）之前，任何文档、工具面或对外
说明都**不得**把分区并行描述为已有能力。

分区不是推动者随手切的工作，而是合同级的受控结构：

1. **谁切**：分区方案由当前持租约的执行者在交接中提议（或立约时预声明），推动者校验后固化。每个分区声明：`partition_id`、`scope_paths`（允许写入的路径前缀集合）、`scope_stages`（负责的阶段 id 集合）、`description`。
2. **互斥判定**：任意两个活跃分区的 `scope_paths` 不得有交集（前缀归一化后比较）；`scope_stages` 不得重叠。校验不过 → 拒绝加派，记录 `lease/partition-conflict`。无法干净分区的合同（如产出是单一文件）不允许档 4，只能串行换人。
3. **写回隔离**：每个分区有独立的 `lease_generation` 和交接追加段（`handover.md` 按分区段落追加，段头带 `partition_id`）；阶段摘要的 promotion 必须声明所属分区。全局合同字段（目标、验收、预算）任何分区都不可改。
4. **完成汇合**：所有分区 succeeded 后，推动者发起 verifier（§5.2）；任一分区失败只阻塞该分区，其余分区成果保留，失败分区可按档 3 重新派人。
5. **心跳与回收**：分区租约与整合同租约同机制——心跳断则该分区回收，其他分区不受影响。

## 8. 执行器注册表（sub2api 式资源池，用户框定）

**灵感来源**：sub2api 把一批异构后端统一成可替换资源池。本协议把机器上所有 agent 应用统一成可替换的执行资源池——推动者眼里没有「亲儿子」，只有池子里声明了能力的资源。

### 8.1 注册表条目（registry.yaml）

```yaml
agents:
  - id: agent-cli-web
    kind: bridge                    # 接入形态：bridge | subprocess（见 §12）
    launch:                         # 结构化启动声明，不是可拼接的 shell 字符串
      argv: [agent-cli, --profile, web, --headless]
      cwd: null
      env_allowlist: [agent-cli_CONFIG]   # 只透传白名单环境变量
    capabilities:                   # 与 §12.4 manifest 同一字段表
      spawn: true
      observe: true
      cancel: true
      notify: true
      followup: true
      steer: true
      interrupt: true
      context: required
      sandbox:
        file_effects: workspace-write
        network: unsupported
        process: unsupported
        enforcement: partial        # Windows ACL 后端只达到 partial
      acceptance_evidence: true
    limits: { max_concurrent_attempts: 2, max_output_bytes: 1048576 }
    cost_hint: medium
    enabled: true                   # 用户框定的开关

  - id: codex
    kind: subprocess
    launch: { argv: [codex, exec], cwd: null, env_allowlist: [] }
    capabilities:
      spawn: true
      observe: true
      cancel: true
      notify: false
      followup: false
      steer: false
      interrupt: true
      context: required
      sandbox: { file_effects: workspace-write, network: unsupported, process: unsupported, enforcement: partial }
      acceptance_evidence: true
    limits: { max_concurrent_attempts: 1 }
    enabled: true

  - id: hermes-subagent
    kind: bridge
    launch: { argv: [hermes, spawn], cwd: null, env_allowlist: [HERMES_HOME] }
    capabilities:
      spawn: true
      observe: true
      cancel: true
      notify: true
      followup: true
      steer: false
      interrupt: true
      context: optional
      sandbox: { file_effects: read-only, network: unsupported, process: unsupported, enforcement: partial }
      acceptance_evidence: true
    enabled: false                  # 用户框定：暂不参与

  - id: qwen
    kind: subprocess
    launch: { argv: [qwen], cwd: null, env_allowlist: [] }
    capabilities:
      spawn: true
      observe: true
      cancel: true
      notify: false
      followup: false
      steer: false
      interrupt: false
      context: optional
      sandbox: { file_effects: unsupported, network: unsupported, process: unsupported, enforcement: none }
      acceptance_evidence: false
    enabled: false
```

注册表条目只是 §12.4 manifest 的本地登记副本。两者冲突时，以适配器在 `health`/`prepare` 时上报的实际能力为准，并以事件记录差异；注册表声明的能力只能收窄分发范围，不能放大约束兑现。

### 8.2 框定（用户的两个控制面）

1. **池子范围**：`enabled` 开关逐个控制哪些 agent 可被推动者调用。用户可以框到只剩一个（指定承办人），也可以全开（自由市场）。默认全部 `false`，逐个显式开启——新增注册不自动入池。
2. **能力门槛**：合同可声明执行器要求（如 `require: [sandbox, plan_mode]`），不满足的池内成员对该合同不可见。比如禁令重的合同，`sandbox: none` 的执行器直接出局。

### 8.3 分发决策

推动者从「已框定 ∧ 满足合同能力门槛 ∧ 有空闲并行额度」的成员里挑，按 `cost_hint` 从低到高、`parallel_sessions` 余额内分配。挑不中任何人的合同不空转——转 blocked(no-executor) 等用户框定新成员。

#### 多合同公平性

多个 active 合同共享同一执行器池时的调度规则：

- **不抢活租约**：任何合同的健康 attempt（心跳存活）持有的租约与分区，不因别的合同更急而被回收。高紧迫度只能排队等额度释放或走自己预算内的档 3 另起会话。
- **全局并发帽**：`config.yaml` 设 `global_max_attempts`（默认 3），所有合同的 running attempt 总数不得突破。单合同再急也只能在帽内加派。
- **分配顺序**：每轮 ticker 按「紧迫档降序 → 同档按 u 值降序 → 再按 `next_wakeup_at` 早先」的顺序分发空闲额度，一轮内每个合同最多获得一个 attempt，防止单合同一轮吃光池子。
- **饥饿保护**：合同连续 N 轮（默认 10 轮，约 10 分钟）因额度不足未获分发且 u ≥ 1.0，升级 blocked(need-user) 并附「池子长期占满」说明，让用户决定加执行器还是砍任务——不静默等死。
  <br/>**状态（2026-09-11 审计 B8）：未实现。** 实测公平性只有容量记账半是接线的；
  `fairness.py` 实现的饥饿规则（连续 5 tick 未派工 → 提到队首）**从未被调用**——
  tick 传给 `apply_fairness_order` 的状态 dict 恒为空，`observe_tick` 只在测试里出现。
  本节描述的升级语义（额度不足 + u ≥ 1.0 → blocked(need-user)）与那套队列重排也不是
  同一条规则，阈值（10 vs 5）与动作（升级 vs 重排）都不一致。接线前先出 E2 的独立
  SPEC 与测试（`docs/RELEASE-PLAN.md` §5 E2），并统一本节与实现的语义。

### 8.4 能力探测（可选，v2）

注册表的能力声明初期由人工如实填写（与 AGENTS.md「先审后装」的精神一致）。v2 可加自动探测：适配器跑一次干跑自检，验证 launch 可用、沙箱可翻译、steer 通道通。

## 9. 约束编译（合同 → 各运行时的强制面）

| 合同策略 | agent-cli bridge | process/codex adapter | hermes adapter |
|---|---|---|---|
| `file_effects.mode` | 解析 agent-cli 沙箱策略；报告 `full/partial` | 映射到该 CLI 的沙箱参数；无法证明则拒接 | 映射到进程沙箱参数；无法证明则拒接 |
| `file_effects.workspace_root` | 绑定目标 Agent 的 cwd，并校验工作区 | 以结构化 cwd 传入 | 以结构化 cwd 传入 |
| `file_effects.deny_paths` | 由沙箱/文件策略执行 | 适配或拒接 | 适配或拒接 |
| `network.mode` | 只有存在独立网络策略时才能声明兑现；提示词不算 | 适配或拒接 | 适配或拒接 |
| `process.mode` / `package_install.mode` | 由宿主审批/沙箱能力单独证明 | 适配或拒接 | 适配或拒接 |
| `context.policy` | 由 bridge 物化快照并注入 Agent | 由 adapter 物化并注入 | 由 adapter 物化并注入 |
| `deadline_at` | 由外部 `longtaskd` 仲裁；不能借用 session-local schedule 充当协议时钟 | 同左 | 同左 |
| `control` | 仅对用户授权的 live Agent 调用 `followup/steer/interrupt` | 只声明 CLI 实际支持的动作 | 只声明实际支持的动作 |

**编译失败的默认行为是拒接**，拒接事件写入 `log.jsonl`，推动者换下一个候选。绝不静默降级。agent-cli 的 session-local Schedule 只能作为一次提醒渠道，不能作为 agent 外协议的持久 Deadline 保证。

## 10. 参考实现分工（本机零件映射）

> 本章是**本机落地参考**，不属于协议规范本体。公开 RFC 仓库中本章移至 `examples/local-deployment/`，规范正文只保留与具体 harness 无关的描述。

| 平面 | 复用什么 | 新写什么 |
|---|---|---|
| 持久层 | planning-with-files 三件套、跨进程文件锁写法（msvcrt） | 合同 schema 校验器、log.jsonl 回放器 |
| 调度层 | Hermes cron ticker 骨架（60s 循环 + 心跳文件 + 状态文件） | 剥离 cron 语义，换成合同扫描 |
| 保活 | Windows 任务计划程序 | 一个保活任务安装脚本 |
| 执行层 | 各 CLI 的 headless 入口（codex/agent-cli/qwen 均在 PATH） | 每应用一个薄适配器（~百行级） |
| 推动层 | agent-cli followup/steer 概念、cron 拉新会话逻辑 | 升级阶梯编排器、租约管理器 |
| 拦截面 | 无现成 → 新建独立 CLI 进程 | 本协议是 agent 外协议，不走插件系统 |

**宿主形态建议**：一个独立的 Python 小守护进程（`longtaskd`），复用 Hermes cron 已验证的锁/心跳/状态文件模式，不挂靠任何 harness——「协议不属于任何 agent」这个定性要求它连进程身份都是独立的。

## 11. 协议暴露面

远期任务不是某个 Agent 应用内部的隐藏服务。参考实现对外暴露一个版本化、语言无关的控制协议；CLI、Agent 插件和未来 UI 都只是这个协议的客户端。

### 11.1 传输与身份

v0.1 只保证**本机单用户**场景：

- 控制面由独立 `longtaskd` 进程提供。
- Windows 使用命名管道；Linux/macOS 使用 Unix domain socket。TCP 监听不是默认传输，避免把本地控制面意外暴露到网络。
- 首次启动生成随机 endpoint token，放在用户私有权限的运行目录；客户端必须通过 token 完成握手。
- 每条请求带 `protocol_version`、`request_id` 和 `client_id`。`request_id` 在重试时保持不变，服务端对有副作用的方法按幂等键处理。
- 外部 Agent 适配器不能直接改 `state.db`、`contract.yaml` 或事件日志，只能通过协议请求和受限工作区写入。

远程/多用户部署、OAuth、TLS 和跨机器调度不是 v0.1 的承诺；未来若开放网络传输，必须单独定义认证、租户隔离和权限模型。

### 11.2 方法集合

v0.1 的方法不是模型工具，而是客户端控制面：

```text
protocol/hello
contract/prepare       # 将模型与用户的谈判结果保存为 drafted 合同
contract/approve       # 用户批准，drafted → active
contract/get
contract/list          # 支持 cursor 分页，不返回无关合同内容
contract/patch         # 仅允许用户审批后的可修订字段，带 expected_revision
contract/pause
contract/resume
contract/cancel
contract/arbitrate     # Deadline/blocked/expired 的人工裁决
attempt/status
attempt/logs
context/refresh
context/promote
executor/list
executor/enable
executor/disable
executor/health
control/notify
control/followup
control/steer
control/interrupt
control/spawn
lease/renew
lease/release
protocol/events         # 按 cursor 读取事件，支持断线续读
```

每个方法都有结构化成功和错误返回；错误统一为：

```json
{
  "ok": false,
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "contract revision is stale",
    "retryable": false,
    "details": {}
  }
}
```

资源方法必须使用输入/输出分离的 schema：客户端提交 `ContractDraft`，服务端返回带 `contract_id`、`revision`、时间戳和当前状态的 `ContractView`。`list` 必须分页；所有涉及修订的操作必须携带 `expected_revision`，冲突返回 `409` 等价的 `REVISION_CONFLICT`，不能最后写入者覆盖前者。

### 11.3 事件、幂等和崩溃恢复

事件是数据库事务的一部分：状态快照、事件记录和幂等键在同一事务中提交。协议保证：

- **单次提交**：一个已接受的副作用请求最多产生一个逻辑事件；客户端重试同一 `request_id` 得到原结果。
- **不丢已提交状态**：数据库确认提交后，服务重启能恢复该状态；文件投影落后时，启动修复并记录 `projection/rebuilt`。
- **不伪造完成**：进程退出码、模型文本和 attempt 成功都不能替代 acceptance 通过。
- **至少一次推动**：通知、唤醒和新会话启动可能重复；适配器必须用 `attempt_id`/`request_id` 去重。协议不承诺外部 Agent 恰好执行一次。
- **旧执行者隔离**：所有写回必须带当前 `lease_generation`；失效 generation 的写回返回 `LEASE_FENCED`，不得污染新 attempt。

### 11.4 保证等级

每个合同和每个执行器都要声明保证等级，调度器只在两者相容时分发：

| 保证 | 含义 | v0.1 默认 |
|---|---|---|
| `durable_state` | 已提交的合同/事件/阶段摘要在守护进程重启后可恢复 | 必须 |
| `handover` | 新 attempt 能按协议读取合同和阶段摘要 | 必须 |
| `best_effort_wakeup` | 有调度器运行时会按 `next_wakeup_at` 尝试推动 | 是 |
| `strict_deadline` | 枚举（§6.4）：`none`＝best_effort，无外部保障；`notify_only`＝Deadline 时刻用户必收通知（依赖 L2）；`notify_and_wake`＝通知必达 + 睡眠机器尽力唤醒（依赖 L0+L1，L3 可选） | `none` |
| `sandbox_full` | 适配器能证明合同要求的策略全部执行 | 按合同 |
| `session_control` | 能调用合同允许的 notify/followup/steer/interrupt | 按合同 |
| `acceptance_evidence` | 能回传带来源的验收证据，而非只有自然语言结论 | 按合同 |

“可用”只能表示进程存在且通过健康检查，不等于拥有上述能力。保证等级变化必须写入事件并影响后续分发；不能修改已发生 attempt 的历史声明。

### 11.5 端到端时序

以下三条时序是适配器作者对齐行为的基准。每条只标「谁发请求、谁提交事务、哪个 id 幂等」，不展开 payload。

**时序 A：正常完成**

```
用户+模型(客户端)          longtaskd              执行器适配器           Agent harness
     │  contract/prepare ──►│ 校验schema，起草 drafted（事务：合同+事件）
     │  contract/approve ──►│ drafted→active，计算 next_wakeup_at
     │                      │ ticker 到点，紧迫档≥3 → control/spawn ──►│ prepare() 翻译约束
     │                      │◄── prepared（含 enforcement 证明）       │
     │                      │ 租约获取（CAS, generation+1）→ spawn ──►│ 拉起 headless 会话
     │                      │                                        │ 模型执行：写 progress/handover
     │                      │◄── attempt/logs, lease/renew（心跳）──│
     │                      │ 模型自报完成 → 派 verifier attempt ───►│ 逐条核 checks
     │                      │◄── 证据集全 pass                         │
     │◄── contract/get: complete（事件带 verifier attempt_id）        │
```

**时序 B：租约回收后换人续跑**

```
attempt-1 心跳中断（会话崩溃/机器重启）
  → 租约超时，守护进程回收（generation 2→3，事件 lease/reclaimed）
  → 推动层按紧迫档重新分发：新执行器 prepare/spawn
  → 新 attempt 读 handover.md（含 source_attempt_id=attempt-1）续跑
  → 此时 attempt-1 若苏醒写回：携带旧 generation → LEASE_FENCED，丢弃
```

**时序 C：Deadline 跨关机仲裁**

```
关机期间 deadline_at 已过
  → 开机，Windows 任务计划拉起 longtaskd
  → ticker 首次全量扫描：当前墙钟 > deadline_at 且合同未完成
  → 停止信号发向存活 attempt（如有），交接锁定快照
  → 合同 → expired（事件 contract/expired，仲裁时刻=arbitrated_at）
  → 用户 contract/arbitrate：采纳部分成果（→ complete w/ note）/ 延期（新修订+新 deadline）/ 作废（→ archived）
```

### 11.6 核心消息 schema（字段表）

线协议以 JSON-RPC 2.0 承载。以下字段表为规范级最小集；完整 JSON Schema 文件在仓库 `schemas/` 目录维护，本节字段名与之保持一致。

`ContractDraft`（contract/prepare 入参）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string ≤200 | 是 | 人类可读标题 |
| `objective` | string | 是 | 完成标准描述（冻结区） |
| `deadline_at` | RFC3339 带时区 | 是 | 墙钟截止点（冻结区） |
| `hard_constraints` | object | 是 | file_effects / network / process / package_install 四策略（冻结区） |
| `acceptance` | object | 是 | `standard` + `checks[]` + `verifier`（默认 cross_check） |
| `soft_guidance` | object | 否 | 注入提示词的软指引（可修订区） |
| `context` | object | 否 | `policy_ref` + `required`（默认 false） |
| `execution` | object | 否 | required_capabilities / allowed_control / 并行与会话策略 |
| `workload_estimate` | object | 是 | `initial_hours` > 0 |
| `budget` | object | 是 | 五项上限，均须为正整数 |
| `client_meta` | object | 否 | 客户端自由字段，不入权威语义 |

`ContractView`（服务端返回）：`ContractDraft` 全部字段 + `contract_id`、`revision`、`state`、`created_at`、`updated_at`、`next_wakeup_at`、`blocked_reason?`。

`AttemptInput`（适配器 prepare/spawn 入参）：`attempt_id`、`contract_id`、`revision`、`lease_generation`、`partition_id?`、`role`（executor|verifier）、`contract_snapshot`（冻结区+验收）、`handover_path`、`context_snapshot_path?`、`workspace_root`、`budget_remaining`。

事件 `payload_json` 最小公共字段：`actor`（user|daemon|adapter|model）、`reason?`、`evidence?`。各事件类型的专有字段在 `schemas/events/` 逐一定义。

### 11.7 错误码全集

统一错误对象见 §11.2。错误码分四族；`retryable` 由服务端如实填写。

| 错误码 | 触发场景 | retryable |
|--------|----------|-----------|
| `VALIDATION_FAILED` | 入参不合 schema（缺字段/类型错/负预算） | false |
| `UNKNOWN_CONTRACT` / `UNKNOWN_ATTEMPT` / `UNKNOWN_EXECUTOR` | id 不存在 | false |
| `REVISION_CONFLICT` | `expected_revision` 过期 | true（重读后重试） |
| `LEASE_FENCED` | 写回携带过期 `lease_generation` | false |
| `LEASE_HELD` | 合同租约被健康持有者占用，请求需持约 | true |
| `PARTITION_CONFLICT` | 分区路径/阶段重叠（**预留：分区租约未接线，从未被抛出**） | false |
| `CONSTRAINT_UNTRANSLATABLE` | 适配器无法兑现硬约束（拒接） | false |
| `CAPABILITY_MISSING` | 合同要求的能力执行器不具备 | false |
| `CONTEXT_CAPACITY_REFUSED` | 上下文压缩后仍超容量且 required=true | false |
| `CONTEXT_STALE` | 快照来源版本过期，需先 refresh | true |
| `BUDGET_EXHAUSTED` | 预算任一项触顶 | false |
| `STATE_FORBIDDEN` | 当前状态下不允许该操作（如对 complete 合同 spawn） | false |
| `STORE_TAMPERED` | 权威库发现外部写入痕迹 | false |
| `IDEMPOTENCY_REPLAY_MISMATCH` | 同 `request_id` 但 payload 不同 | false |
| `AUTH_FAILED` / `AUTH_REQUIRED` | token 缺失或错误 | false |
| `RATE_LIMITED` | 控制面请求过频 | true |
| `INTERNAL` | 未分类守护进程内部错误；message 必须可定位日志 | true |

新增错误码只能追加，不得改既有语义；客户端必须把未知错误码按 `INTERNAL` 处理并展示原文。

### 11.8 MCP 工具面 profile

MCP 网关（`lhgp-mcp`）的工具面按**调用方角色**收窄。这不是提示词约定，是服务端声明：

- `tools/list` 只返回 profile 内的工具；
- `tools/call` 对 profile 外的工具**一律拒调**（fail-closed）——隐藏必须是不可达，
  否则「看不见」只是装饰，模型仍可凭名字硬调；
- `initialize` 与 `tools/list` 应答携带当前 `profile` 名，宿主可直接看到自己拿到的是哪个收窄面。

选择方式：环境变量 `LHGP_MCP_PROFILE`，取值 `executor` / `verifier` / `planner` /
`operator` / `full` / `legacy`。**未设置 = `legacy`（全量，含兼容别名）**，既有安装零变化；
未知取值直接退出并报错，不静默回落。

| profile | 面向 | 数量（相对全量 52） | 关键边界 |
|---------|------|------|----------|
| `executor` | 执行者会话 | 11 | 拿不到任何批准权（approve / update_goal / user_confirm 均不在） |
| `verifier` | 核验者会话 | 7 | 只有读面 + 自己的写回通道 |
| `planner` | 规划 / Goal 层 | 12 | 可立合同、提计划，无执行者写回通道 |
| `operator` | 人的管理面 | 36 | 全部正名（full 减别名轨），含三个 Principal 门 |
| `full` | 全部正名 | 35 | 不含 `longtask_*` 兼容别名 |
| `legacy` | 兼容默认 | 52 | 历史行为，全量 |

角色 profile 只收正名——别名轨是迁移兼容面，新配置不再扩大它的暴露。
profile 引用不存在的工具名在启动时即报错（fail-closed，宁可不启动）。

动机（实测）：52 个工具的 schema 全量约 34 KB（粗估 8.5K token），其中 37% 是
兼容别名重复；executor 面降到约 13%。执行者会话原本能看见 `approve` 等
Principal-only 入口——模型调用会被 `AUTH_FAILED`，但**能看见就会去试**，
每次尝试都是一次注定失败的往返和一段被浪费的上下文。

## 12. Agent 应用插件接入

协议不要求每个 Agent 应用实现同一套内部代码。接入分为三种形态，按能力从低到高排列：

### 12.1 进程适配器（最低门槛）

适合只有 CLI、没有外部会话 API 的应用。适配器实现一个语言无关的 `ExecutorAdapter`：

```ts
interface ExecutorAdapter {
  readonly id: string
  describe(): Promise<ExecutorDescriptor>
  health(signal: AbortSignal): Promise<HealthResult>
  prepare(input: AttemptInput): Promise<PreparedLaunch> // 失败即拒接
  spawn(input: AttemptInput, launch: PreparedLaunch): Promise<SpawnResult>
  observe(attemptId: string): Promise<AttemptObservation>
  cancel(attemptId: string, reason: string): Promise<void>
  collect(attemptId: string): Promise<AttemptResult>
}
```

`spawn` 只能接收结构化 argv、cwd、环境白名单和已物化的上下文快照，不接收可拼接的 shell 字符串。适配器将 stdout/stderr、退出码、结构化进度文件和最终 artifact 写回协议；模型输出仍是不可信数据，不能直接变成下一条命令。

**stdout 通道编码（MUST）**：适配器回读的 stdout 上跑着两条协议通道——完成事件行（`attempt/finished`，见 SPEC §12.4）与验收判定块（`lhgp-verdict`）。两条通道 MUST 为 UTF-8：适配器按 UTF-8 解码，并在 spawn 时注入 `PYTHONIOENCODING=utf-8`，让 Python 执行者满足该约定。这不是可选的健壮性修饰：宿主 locale 不是 UTF-8 时（中文 Windows 为 cp936），缺失该约定会让一切非 ASCII 证据（verifier 的 `details`、含中文的路径）被静默替换成 U+FFFD——损坏不可逆且不报错，排查时只表现为「跑通了但证据是乱码」。非 Python 执行者的同一义务由 task_prompt 声明。

### 12.2 Agent 内插件/Bridge

适合 agent-cli 这类有公开 Agent handle 的应用。插件加载时只做接线，不复制合同状态：

1. 通过本机 JSON-RPC 向 `longtaskd` 注册 `agent_id`、`session_ref`、能力和授权范围。
2. 监听应用提供的 Agent 生命周期和状态事件，将健康/空闲/运行状态上报。
3. 当推动者发来 `control/followup` 或 `control/steer`，在**已授权且仍在线的确切 Agent handle**上调用对应方法，并显式使用插件/调度器 source；不得借用用户 source。
4. `control/interrupt` 只能作用于用户框定且适配器声明可中断的目标；结果回传 accepted/rejected/unsupported，不把“消息入队”伪装成任务完成。
5. Agent 退出或插件卸载时释放 bridge 注册和本地控制句柄，不删除合同或 attempt 状态。

agent-cli 的 `followup` 会排队后续轮次，`steer` 会把可唤醒的输入排入下一步骤；它们不是同一种干预，bridge 必须分别声明和记录。agent-cli 的 `Schedule` 仍只是 session-local 提醒，不能代替 `longtaskd` 的外部 Deadline 仲裁。

### 12.3 直接协议客户端

适合未来 UI、IDE 或其他 Agent 平台。客户端只使用 JSON-RPC 控制面和事件流，不需要加载插件；若它能够接受合同上下文，则按 `AttemptInput` 启动或恢复执行。

### 12.4 接入声明

每个执行器发布一个版本化 manifest：

```yaml
protocol_version: 1
executor_id: codex-cli
adapter_version: 0.1.0
transport: subprocess
capabilities:
  spawn: true
  observe: true
  cancel: true
  notify: false
  followup: false
  steer: false
  interrupt: true
  context: required
  sandbox:
    file_effects: workspace-write
    network: unsupported
    process: unsupported
    enforcement: partial
  acceptance_evidence: true
limits:
  max_concurrent_attempts: 1
  max_output_bytes: 1048576
user_policy:
  enabled: false
  allow_interference: false
  allow_spawn: true
```

manifest 是能力声明，不是能力证明。健康检查只能证明“现在能连接”；适配器必须在每次 `prepare` 返回实际 `enforcement` 和控制能力，合同要求不满足就拒绝。

### 12.5 Agent 模式的边界

在 Agent 应用内部，协议可以提供一个 `longtask` mode/skill 入口，让模型知道当前 attempt 的合同、上下文和交接规则；它不新增 bash、fs、subagent 等执行工具。模式负责语义引导，协议守护进程负责持久状态、租约、调度、预算和干预授权。应用没有原生 mode 时，适配器只注入等价的上下文说明，不宣称它拥有原生模式状态。

## 13. 技术选型与运行时保证

### 13.1 推荐：Python 3.11+ 参考实现

v0.1 推荐 Python 3.11+，理由是：

- 任务控制面是文件/数据库/进程编排，不是 CPU 密集型计算；C/C++ 不会自动提供更强的 Deadline、事务或沙箱保证。
- Windows、Linux、macOS 都有成熟的进程、信号、命名管道/Unix socket 和 SQLite 支持；适配器生态也更容易用 Python/CLI 接入。
- SQLite/WAL、`pathlib`、`subprocess`、`asyncio`、`jsonschema` 等基础足以实现 v0.1，不需要复杂服务栈。
- Python 版本适合先把协议迭代快、错误语义和参考适配器做实；性能瓶颈若出现，优先把单个扫描/解析热点替换为原生库，而不是提前把整个控制面改成 C++。

Python 不是安全边界。安全边界来自：SQLite 事务、结构化 argv、私有 IPC、路径规范化、适配器拒接、Windows ACL/平台沙箱和资源预算。v0.1 要求锁定 Python 大版本、提交唯一 lockfile/依赖清单、依赖安装脚本默认拒绝，并在 CI 中执行原生依赖审计。

### 13.2 何时考虑 Rust/C++/C

- **Rust**：当需要单文件分发、低常驻内存、强进程监督、跨平台系统服务或不想依赖 Python 环境时，适合做 v1 的 `longtaskd` 重写；协议 schema 和事件格式保持不变。
- **C++**：只有需要接入已有 C++ 宿主、系统级守护服务或大量原生 SDK 时再考虑。它增加构建和发布矩阵，不会替代协议级的事务设计。
- **C**：不推荐作为完整实现。字符串/进程/JSON/跨平台 IPC 和错误处理的工程成本会显著增加，收益不对应本项目的控制面性质。

语言可替换，但协议兼容性优先：所有实现都必须通过同一套 schema、事件、错误和崩溃恢复一致性测试。

### 13.3 存储与投影

SQLite/WAL 是 v0.1 的权威状态存储，单用户本机目标下提供事务、崩溃恢复和跨进程并发控制。`contract.yaml`、`lease.json`、`log.jsonl`、上下文文件和阶段摘要是可读投影，写入必须由投影器在事务提交后完成；投影落后可重建，投影超前不得发生。

事件表至少包含：`event_id`、`contract_id`、`attempt_id?`、`lease_generation?`、`event_type`、`payload_json`、`request_id`、`created_at`、`actor`、`schema_version`。数据库启动时执行 schema 版本检查；未知未来版本只读并拒绝写入，不能猜测迁移。

## 14. 保证与验证边界

### 14.1 威胁模型（本机单用户 v0.1）

明确防什么、不防什么。以下每条威胁对应既有机制，不引入新的安全设施：

| 威胁 | 场景 | 对应机制 |
|------|------|----------|
| 提示词注入污染交接 | 执行中被处理的文本诱导模型把恶意指令写进 handover，下一个执行者照做 | 交接写入走原子校验；handover 进入提示词时被**可验证围栏**包住：围栏 token 由正文摘要派生，正文里任何形似围栏的前缀被中和，闭合标记只可能由协议写出（§14.2）；硬约束在合同锚点只读区，不可被交接内容修改 |
| 控制序列污染证据与上下文 | harness 给输出上色/改标题/发双向覆写符，导致证据里出现方向欺骗文本，或 `lhgp-verdict` 围栏被 ANSI 序列插坏而「明明是有效证据却退化成无证据」 | 适配器在**唯一输出出口**（collect）做结构性清洗：剥离 CSI/OSC/DCS/SOS/PM/APC 与两字节转义、C0/C1/DEL、以及除 ZWNJ/ZWJ 外的 Unicode `Cf`；清洗幂等（§14.2） |
| 取消后孤儿进程继续写工作区 | 只终止直接子进程，harness 的 worker 成为孤儿继续改盘——没有事件、没有报错，只有盘上多出来的改动 | 取消以**进程树**为单位：POSIX 下 spawn 即 `start_new_session` 使子进程自成组长、取消时 `killpg`；Windows 下 `taskkill /T`（不带 `/F` 失败立即升级 `/F`）。组号不是该进程自己时不 killpg，避免连坐推动者（§14.3） |
| 模型伪造完成 | 模型口头声称完成或伪造退出码 | `complete` 只能由 verifier 证据集触发（§5.2）；退出码不构成验收 |
| 旧进程苏醒写回 | 卡死会话苏醒后继续写进度，覆盖接管者状态 | `lease_generation` fencing，过期写回 `LEASE_FENCED` 丢弃 |
| 路径穿越 | 合同或适配器构造 `../` 路径越出 workspace_root | 所有路径服务端归一化后与 workspace_root / deny_paths 比较；拒绝符号链接逃逸 |
| 本机恶意进程直连 | 同用户其他进程直接连命名管道/socket | endpoint token 握手；token 存用户私有权限运行目录；无 TCP 默认监听 |
| 直接改库伪造状态 | 用户或脚本绕过协议改 state.db / 投影文件 | §3.1 人类编辑门：直接改动视为 dirty 草稿或 tampered，不承认 |
| 适配器夸能力 | manifest 声称能兑现沙箱实际不能 | manifest 只是声明；`prepare` 必须返回 enforcement 证明，夸口即拒接（§12.4） |
| 注入 shell 命令 | 模型输出被拼接进 shell 执行 | spawn 只收结构化 argv；模型输出是不可信数据，永不直接变命令 |
| 工具注解失真误导宿主 | 新增工具忘了分类，宿主看到「既非只读也非破坏性」因而不请求人工确认，模型直接改掉持久承诺 | §11 的只读/破坏性两个集合是**唯一**分类真相源，注解由集合派生（直接赋值，内联声明覆盖不了）；`tests/unit/test_mcp_tool_surface.py::TestToolAnnotationDrift` 钉住完备性与无漂移 |
| 预算失控烧钱 | 升级阶梯无限拉新会话 | budget 五项上限 + 全局并发帽；触顶 blocked，终极边界是用户 |
| 密钥泄露进上下文 | 上下文准入把凭证/其他合同内容塞进模型窗口 | redaction 永不准入清单（§4.1）+ 容量与来源审计事件 |

**明确不防**（v0.1 边界外）：恶意 root/管理员、其他用户账户、网络远程攻击者（无网络监听面）、被攻陷的 harness 宿主本身。这些属于操作系统账户与宿主安全问题，协议不假装解决。

### 14.2 不可信外部文本：入站清洗与注入围栏

执行器的 stdout/stderr、`handover.md`、verifier 的自由文本，全部是**任意 harness 写的**，不是用户或协议写的。它们有两个落点必须同样处理：进入证据/投影（落盘），以及进入下一个执行者的提示词（注入）。

**入站清洗**（`lhgp.untrusted.sanitize`）：在适配器唯一的输出出口做，不做语义判断，只做结构性剥离——CSI/OSC/DCS/SOS/PM/APC 与两字节转义、C0/C1/DEL，以及除 ZWNJ/ZWJ 外的 Unicode `Cf`（含 U+202A–U+202E 方向覆盖）。保留 `\t`/`\n`/`\r`。清洗**幂等**，重复经过边界不累积损耗。

**注入围栏**（`lhgp.untrusted.boundary`）：只写一句「以下内容不可信」不够——正文可以自带一段看起来同样权威的收尾标记，把后面的内容伪装成边界之外。所以围栏 token 由**正文摘要**派生（`LHGP-UNTRUSTED-<sha256 前 12 位>`），并且正文里出现的任何形似围栏前缀都被改写成不可能匹配的形态。于是「围栏内 = 全部正文」由构造保证，不是靠约定。

这是**提示词层的防线，不是安全边界**：它不阻止模型被说服，只保证模型不会把执行者写的字当成协议或用户的话。清洗与围栏都不改变协议语义、线协议或错误码。

### 14.3 取消以进程树为单位

harness 普遍是「主进程拉起 worker」的两级结构。只终止直接子进程会把 worker 留成孤儿继续写工作区——对合同来说这是「已取消却还在改盘」：没有事件、没有报错，只有盘上多出来的改动。

- **POSIX**：spawn 时 `start_new_session=True` 让子进程自成会话/进程组组长；取消时对进程组发信号，一次覆盖组内全部成员。**只有当进程自己是组长时才 killpg**（`getpgid(pid) == pid`）——否则组号可能指向推动者自己所在的组，killpg 会把守护进程连坐杀掉。
- **Windows**：`taskkill /T` 按父子关系遍历。Windows 上没有可用的「礼貌的树终止」（不带 `/F` 只能给 GUI 目标发关闭请求，控制台 harness 一律拒绝），所以不带 `/F` 的失败**立即升级 `/F`**，不当作「再等等看」。

**诚实边界**：`taskkill` 按快照遍历，遍历开始后才 new 出来的曾孙可能被漏掉；本协议不做「保证全灭」的声明。进程是重绑句柄（非子进程）时同样只能尽力而为。

本项目不把“有测试”当成保证。保证来自协议规则和可观察事实：

- **持久性保证**：只对已提交事务负责；未提交的进度、模型输出和临时 scratch 可能丢失。
- **一致性保证**：同一合同的状态、attempt、租约和事件在一次事务中变更；旧 generation 写回拒绝。
- **调度保证**：守护进程在线时按时间检查；进程或机器离线期间只保留合同，不声称执行了工作。
- **干预保证**：只有目标 Agent 声明支持且用户授权的动作才会发送；消息接受只代表入队，不代表模型已经执行。
- **安全保证**：无法证明的 sandbox/network/process 约束拒绝分发；模型提示词不是安全边界。
- **验收保证**：工具不拥有验收标准；只有合同中由模型和用户共同裁定的 acceptance 条款及其证据能决定 `complete`。
- **不可信文本保证**：执行者写的内容进入提示词时，一定在可验证围栏内，且正文无法提前闭合围栏；进入证据与投影前一定过结构性清洗（§14.2）。
- **取消保证**：取消以进程树为单位发起，不只是直接子进程；组号无法确认为该进程自己时拒绝组信号，宁可少杀也不连坐推动者（§14.3）。
- **资源保证**：预算、并发、输出大小、单次运行时长和重试次数有上限；触顶后进入可解释的 blocked 状态。

只需实现与上述保证直接对应的少量一致性/崩溃/拒绝场景；不为覆盖率或“看起来完整”堆无意义探针。每一个保证都必须能在事件、状态或返回错误中被观察到。

## 15. GitHub 发布分级

### 15.1 RFC 发布（当前文档达到）

可以公开，但 README 必须明确这是协议设计预览，不是已完成的 Agent 编排工具。仓库应只包含脱敏后的规范、schema、示例和 skill，不上传开发总仓库内容。

### 15.2 Developer Preview（建议首个可安装版本）

必须至少具备：

- Python `longtaskd` 可安装、启动、停止和 `doctor`
- SQLite/WAL 权威状态和可重建文件投影
- 合同 create/approve/status/pause/resume/cancel
- 一个通用结构化 subprocess adapter
- 一个 fake executor，用于验证协议错误和崩溃恢复
- fencing lease、幂等 request、结构化错误码
- 本机 JSON-RPC/CLI 控制面
- 用户框定的 executor allowlist、能力门槛和预算
- dry-run、审计日志和全局 kill switch
- 明确的 `best_effort_wakeup` 语义

### 15.3 v1

再加入：

- agent-cli bridge 的 followup/steer/interrupt
- Agent 自动发现和能力探测
- 多 Agent 分区并行（档 4：设计见 §7.1，**当前未实现**；`allow_parallel` 已可声明但不产生并行行为，落地见 ADR-005）
- 外部严格 Deadline 唤醒源
- 跨平台安装包和升级/迁移机制
- 面向第三方的适配器 SDK 与一致性套件

## 16. 本期不做（Not Doing）

- ~~**外部云唤醒源（Chronos 式）**~~——已设计（DESIGN v0.7 §6.4 分层唤醒 + ADR-0002），实现排在 Developer Preview 之后
- **能力自动探测**——人工声明 + 拒接兜底够用
- **合同模板市场 / 复杂 UI**——先用文件 + CLI
- **跨机器分发**——本期单机协议
- **执行器间直接通信**——一切经由盘上交接文件，这是「会话即燃料」的推论
- **闲时任务合并**——两种语义（机会主义 vs 合同制），不混

## 17. 随协议分发的说明性 Skill

工具是工具，不拥有标准、不裁决对错。但协议附带一个 skill（如 `longtask-contract`），作用只有一个：**教会模型怎么用这套协议**——

- 怎么把用户模糊的“把它做好”谈判成可核对的验收条款（`standard` 起草 + `checks` 固化）
- 怎么写交接文件才能让下一个素不相识的执行者无缝续跑
- 怎么如实滚动修正工作量估算（紧迫度的燃料）
- 怎么按上下文准入合同读取各阶段摘要，并把当前工作记忆写入可编辑临时区
- 怎么在来源版本变化时请求刷新上下文，以及何时通过显式提升把临时结论写入阶段摘要
- 怎么在预算和禁令内选择升级手段，而不是直接伸手要人

Skill 是说明书，不是裁判。验收标准每一条都出自立约时模型与用户的谈判，工具侧零内置。Skill 也不能绕过协议的状态、预算、租约、沙箱或权限检查。

## 18. 未决问题（需用户拍板）

1. ~~**验收核对由谁执行**~~ **已定（2026-08-31）**：默认另派只读 verifier attempt 交叉核对，合同可声明 `verifier: none` 改为争议时升级到人。见 §5.2。
2. ~~**严格 Deadline 的外部唤醒源**~~ **已决（2026-08-31）**：采用分层唤醒体系（L0 电源守卫 / L1 RTC 计划任务 / L2 云侧准时通知 / L3 可选常在线中继），`strict_deadline` 细化为 `none / notify_only / notify_and_wake` 枚举。见 §6.4 与 ADR-0002。
3. **agent-cli Bridge 的宿主接入方式**：控制面和事件订阅可先用本机 JSON-RPC；具体由哪个 agent-cli profile 承载 bridge、如何发现目标 Agent handle，需在适配器实现前按公开 API 定稿。
4. **协议名称与注册表命名空间**：暂定为 `longtask`（守护进程 `longtaskd`，目录 `~/.longtask`）。公开发布前最终确认协议标识、版本前缀和第三方适配器命名规则；更名只影响标识，不影响 schema 语义。

## 19. 审批记录

| 日期 | 事项 | 结果 |
|---|---|---|
| 2026-08-31 | 设计文档 v0.1 | 草案建立 |
| 2026-08-31 | 协议、接入与保证等级修订 | v0.4 草案，已并入 |
| 2026-08-31 | 保证边界、协议暴露与技术选型补齐 | v0.5 草案，已并入 |
| 2026-08-31 | 本质定性（harness 之外的 harness）+ 时序/schema/验收默认/威胁模型/分档阈值/分区租约/公平性/脱敏示例 | v0.6 草案，用户审批通过 |
| 2026-08-31 | 跨关机语义与分层唤醒（strict_deadline 枚举化，L0–L3），ADR-0002 | v0.7 草案，用户审批通过 |
| 2026-09-10 | 不可信外部文本的入站边界（§14.2 清洗 + 围栏）、取消的进程树语义（§14.3）、工具注解单一真相源（§14.1 表） | v0.8 草案，依用户「把 openpi 的优点吸收进本协议」的指示实施；无协议语义变更（线协议 / schema / 错误码不变） |
| 2026-09-10 | MCP 工具面 profile（§11.8：六档角色收窄、越 profile 拒调、签字门正名 `lhgp_user_confirm_spec_verdict`） | v0.9 草案，依用户「进行相对严厉的改动」的指示实施；默认 `legacy` 零变化，MCP 应答新增 `profile` 字段（附加字段，非破坏） |
| 2026-09-10 | attempt 消耗台账（SPEC §12.3.1：usage 自报字段、schema v5、stats 聚合；预算强制明确不在本版本） | v0.10 草案，依用户「继续进行」的指示实施；纯新增字段，存量写回（不带 usage）零变化 |
| 2026-09-11 | 成本预算线（SPEC §12.3.2：budget.max_cost 可选字段、decide 强制分支、tick 台账合计） | v0.11 草案，承接 v0.10 台账；可选字段未声明零变化，合同 wire schema 预算对象增可选键（附加属性，非破坏） |
| 2026-09-11 | 档 4 并行加派诚实化（§6.2/§6.3 表、§7 租约、§7.1 标题、§11.7 错误码、§16 非目标统一标注「未实现」；decide() 停止产出 PARALLEL） | v0.12 草案，依用户「按你的建议做」的指示实施；行为不变（停滞本来就是串行重派），改的是决策历史不再写不实的「parallel dispatch」，且不再消耗 max_escalations |
| 2026-09-11 | stdout 协议通道编码入规范（§12.1：完成事件行与 `lhgp-verdict` 判定块 MUST 为 UTF-8；SPEC §12.4 同步声明；适配器 spawn 注入 `PYTHONIOENCODING=utf-8`） | v0.13 草案，依用户「以完善所有缺口为目标持续进行完善」的指示实施；线协议、schema、错误码均不变，解码宽容度也不变（非法字节仍以 U+FFFD 替换而非中断 attempt）——补的是「实现已假设 UTF-8，却从不保证、规范也没写」这一缺口 |

## 附录 A 术语表

| 术语 | 定义 |
|------|------|
| 合同 contract | 一份持久化在盘上的远期目标契约：目标、Deadline、硬约束、验收条款、预算 |
| 冻结区 / 可修订区 | 合同字段的两个集合：前者立约即钉死（Deadline、禁令、目标），后者经用户审批门可改并递增 revision |
| attempt | 一次执行尝试；合同可先后拥有多个 attempt，attempt 失败不等于合同失败 |
| 执行器 executor | 注册表中的 Agent 应用条目，由适配器代理其入场、观察、取消与回报 |
| 适配器 adapter | 每个 harness 一个的薄插件：翻译约束、拉起会话、写回结果，不复制合同状态 |
| 租约 lease | 执行者写回合同状态的凭据，带心跳与超时；超时即回收换人 |
| 租约代次 lease_generation | 每次租约变更单调递增的序号；写回必须携带，过期即 fenced |
| 分区 partition | 档 4 并行时把合同切成路径与阶段互斥的子工作面，各自持有分区租约 |
| 投影 projection | 由权威库事件重建的可读文件（contract.yaml、log.jsonl 等）；可落后可重建，不可超前 |
| 交接 handover | 给下一个素不相识执行者的续跑文件，含最低必填区块（§3.1） |
| 提升 promote | 把 scratch 中的临时结论经显式请求写入 progress/阶段摘要的动作，带来源 attempt id |
| 推动层 promoter | 紧迫性引擎：按分档阈值执行提醒/转向/另起/加派/交还人的升级阶梯 |
| 拒接 refuse | 适配器无法完整兑现硬约束时拒绝分发的动作；编译失败默认拒接，不降级 |
| verifier | 验收核对 attempt（§5.2）：只读、独立会话、逐条核 checks 并产出证据集 |
| 外置 harness | 本协议的体系位置（§1.1）：不属于任何 Agent 应用，反过来把各应用抽象成执行资源池 |
| 唤醒源 wakeup source | 推动机器或用户在约定时刻醒来的机制（§6.4 L0–L3：电源守卫/RTC 计划任务/云侧通知/常在线中继）；永远不是权威，只能推不能裁决 |
| 不可信围栏 untrusted fence | 把执行者写的内容注入提示词时的可验证数据边界（§14.2）：围栏 token 由正文摘要派生，正文里形似围栏的前缀被中和，闭合标记只可能由协议写出 |
| 输出清洗 output sanitization | 在适配器唯一输出出口做的结构性剥离（§14.2）：转义序列、C0/C1/DEL、方向覆写类 `Cf`；幂等，不做语义判断 |
| 进程树终止 process-tree termination | 取消 attempt 时以整棵进程为单位的终止（§14.3）：POSIX 走进程组信号（仅当进程自己是组长），Windows 走 `taskkill /T`；不是「只杀直接子进程」 |
