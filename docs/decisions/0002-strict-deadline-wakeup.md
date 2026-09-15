# ADR-002: 跨关机严格 Deadline 的分层唤醒

## 状态

Accepted

## 日期

2026-08-31

## 背景

v0.1 对 `strict_deadline` 只承诺 `best_effort_wakeup`：机器睡眠/关机期间 ticker
不存在，Deadline 过了也不会有任何东西醒来。现有兜底只有「仲裁时刻语义」
（DESIGN §6.4，开机后补裁决），它保住中间成果，但丢掉两样东西：

1. 机器睡着了但 Deadline 前还有剩余时间——本可以唤醒干活，白白浪费余量。
2. 机器关着过了 Deadline，用户毫无知觉——仲裁被无限期延迟，合同烂在盘上。

DESIGN §18 未决问题 #2 要求：若要机器关机仍严格触发，需定稿外部唤醒源的
信任与费用模型。本 ADR 与 DESIGN v0.7 §6.4 一并回答该问题。**本决定只定
设计，不实现**（实现排在 Developer Preview 之后，见 DESIGN §16）。

## 决定

采用**分层唤醒体系**（四层，各自独立可用，可渐进部署）。核心不变式：
**唤醒源永远不是权威**——外部唤醒只能「推」（通知 + 唤醒信号），不能读合同
内容、不能仲裁、不能写状态。仲裁仍只发生在 longtaskd 醒来后的首轮扫描
（DESIGN §6.4 仲裁时刻语义不变）。

1. **L0 防睡眠守卫**（本地，零成本）：active 租约存活或紧迫度 u ≥ 1.0 时，
   longtaskd 持有系统电源请求（Windows `SetThreadExecutionState`），
   阻止机器干活途中睡着。事件 `wakeup/sleep-guard` 记录持有/释放。
2. **L1 RTC/计划任务唤醒**（本地，覆盖「睡眠」场景）：每个 active 合同在
   `max(next_wakeup_at, deadline_at − safety_margin)` 注册带 wake 标志的
   Windows 计划任务（复用 §3.3 保活通道，只加 wake 位）。S3/现代待机可按时
   唤醒；S5 关机取决于 BIOS RTC alarm。事件 `wakeup/rtc-armed`、
   `wakeup/rtc-fired`。
3. **L2 准时通知**（云侧推送，覆盖「关机错过 Deadline」的知晓问题）：
   在线路径即时推送关键事件；离线路径**写前上传**——每次合同状态变更时把
   未来 deadline 清单同步给云侧定时器，到点由云侧代发推送。数据最小化：
   上行仅 `contract_id`、`deadline_at`、通知渠道、scoped token；objective、
   禁令、交接内容永不上行。本地是权威，云侧是投影（与 §3.1 投影哲学同构）。
4. **L3 常在线中继**（可选，覆盖「关机且想唤醒」）：`longtask-relay` 微型
   组件跑在用户已有的常在线设备（NAS/路由器/VPS），只做两个动作：到点推送
   通知、向目标机发 WoL magic packet。持 scoped token，仅授 notify + WoL。

配套修订：

- `strict_deadline` 从单一布尔细化为枚举 `none / notify_only / notify_and_wake`
  （DESIGN §11.4）：`notify_only` 依赖 L2；`notify_and_wake` 依赖 L0+L1
  （L3 可选）。分层声明、分层降级，不假装保证——strict ≠ 「工作一定在
  Deadline 前完成」，而是「notify 必达 + wake 尽力」。
- 信任与费用模型：L2 云侧免费额度（公共仓库 scheduled workflow / Workers
  cron）或 ~$3/月 VPS，只持有 deadline 时间戳，无合同语义，泄漏无害；L3 relay
  用用户自有硬件则零成本。唤醒信号至少一次、可能重复，复用现有 `request_id`
  幂等（DESIGN §11.3），重复通知无害。任一层离线/失效即记 `wakeup/degraded`
  事件并降级声明，绝不静默假装 strict。

## 考虑过且拒绝的方案

### 纯云端方案（仅 L2，不动本地唤醒）

- 拒绝理由：只解决「关机时用户准时知道」，睡着机器的剩余时间仍然浪费；
  且通知送达依赖外部服务在线，单一外部依赖撑不起 strict 的唤醒承诺。

### 纯本地方案（仅 L0+L1，零外部设施）

- 拒绝理由：零费用、零隐私顾虑，但关机跨 Deadline 时用户照样不知情——
  S5 关机唤醒取决于 BIOS 支持（笔记本多数不支持），严格性最弱。可作
  默认层保留，但不能作为 strict 的完整答案。

### 中继持合同副本做远程仲裁

- 拒绝理由：违反权威唯一性。权威只在本地 `state.db`（DESIGN §3.1、§11.1：
  外部节点不能写状态）；中继被攻陷即能伪造合同状态（伪造 expired/complete），
  整个协议的可信度随之崩塌。中继只能持 scoped token 做 notify + WoL 两个
  无状态动作，被攻陷的最坏后果 = 误唤醒/误通知，无法伪造合同状态。仲裁
  时刻语义保持在 longtaskd 首轮扫描，本决定不动摇。

## 后果

- DESIGN 升到 v0.7：§6.4 扩为「跨关机语义与分层唤醒」，§11.4 保证等级表
  `strict_deadline` 改枚举并注明依赖层，§16 移除「外部云唤醒源」条目
  （已设计、实现排后），§18 未决问题 #2 关闭，附录 A 增「唤醒源」条目。
- 线协议、schema、错误码均不变（纯设计层演进，`schema_version` 不动）。
- 实现推迟到 Developer Preview 之后；claims 注册表以 `deferred` 条目
  （`strict-deadline-wakeup-design`）跟踪，分层唤醒实现并通过 conformance
  场景后转 `verified`。
- L2/L3 引入外部节点后，威胁模型（DESIGN §14.1）的「明确不防」边界多了一类
  被攻陷的外部唤醒节点；靠 scoped token 最小授权 + 数据最小化约束其危害上限。
