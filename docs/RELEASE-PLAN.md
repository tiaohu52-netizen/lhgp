# LHGP 发布与完善执行计划

> 日期：2026-09-05；状态：R1/R2 已完成，仍在最终候选发布检查。
> 产品定义：[ADR-004](decisions/0004-contract-runtime-and-release-scope.md)。
> 语义权威：[LHGP-SPEC.md](LHGP-SPEC.md)。本轮执行顺序由 [ROADMAP](LHGP-ROADMAP.md) 委托本文管理。

## 1. 目标与发布截止线

先发布一个边界清楚、已承诺能力可靠的**单机限期合同中枢**（单机多 Agent 任务合同运行时）。
调度内核无需 LLM；远期目标是主要场景；人、Agent 和普通程序均可通过相应入口参与。

首发目标仍为 Developer Preview，不声明生产稳定、自动理解任意目标或必然按时完成。
包版本的唯一来源是 `pyproject.toml`，本文不重复记录它；本计划也不自动改版本、提交、推送或发布。
满足 R1–R6 后即可准备 GitHub 预发布，不等待后续增强 E1–E4；实际远端、版本及发布说明须在 R6 确定。
Alpha 仍须满足 ROADMAP §1.1 的独立门槛，不能用一次三阶段 dogfood 替代三个真实目标。

明确不做：跨主机、跨网络中继、多租户服务、严格墙钟结果担保、全仓机械改名。

## 2. 当前基线与已知问题

基线提交 `b4b301d` 曾有 634 个测试、82.17% 覆盖率；本轮修复后为 636 个测试、82.24%。
该提交号在 2026-09-05 的历史脱敏后已不可达，上述数字是当日快照口径，与当前
`quality/coverage-baseline.json` 的分支感知口径不可直接比较；当前地板与实测值只以该基线文件为准。
这仍是测试覆盖基线，**不是放行结论**；附加诊断先复现、再以回归测试覆盖了以下错误：

| 编号 | 已复现行为 | 发布影响 | 对应任务 |
|---|---|---|---|
| B1 | 已修复：取消抛异常时转为 `orphaned`，保留租约并交给 reconcile grace/fencing | 回归测试覆盖，待最终候选重新锚定 claims | R1 ✅ |
| B2 | 已修复：明确 `role=verifier` 的 started 事件不计入 executor dispatch 预算 | 回归测试覆盖，待最终候选重新锚定 claims | R2 ✅ |

复现方法、完整结果和尚未验证事项见[发布检查](evidence/release-readiness-2026-09-05.md)。
`quality/claims.json` 原有已验证标签不覆盖这些新增反例；不得以其全绿覆盖已知问题。

## 3. 本轮已经完成的文档切片

- [x] D0a：重写中英文 README，明确多合同、无 LLM 调度、模型可选参与及实际使用边界。
- [x] D0b：ADR-004 记录理由与兼容决策，更新 SPEC 首屏／README 叙事，保留旧 ADR 历史。
- [x] D0c：建立本计划与发布检查记录，让 ROADMAP、产品一页纸及 `tasks/plan.md` 指向当前口径。
- [x] R1：超时取消失败的控制权保护，定向回归与全量质量门通过。
- [x] R2：执行／验证预算隔离，定向回归与全量质量门通过。
- [ ] R3–R6：最终候选、隔离安装、安全扫描及发布决策尚未完成。

## 4. 首发任务卡

所有行为修改遵循：确认 SPEC → 添加能复现当前失败的回归测试 → 最小实现 → 定向测试 → 全量质量门 → 更新证据。
下列测试名／新增文件是计划目标，只有实际落地并通过后才可作为证据。
规模按独立切片估算：S 为 1–2 个主要文件，M 为 3–5 个；超过范围继续拆分。

### R1 · 超时取消失败时守住控制权（M，发布阻断）

依赖：无。涉及 `src/longtask/cli/runner.py`、实际 cancellation adapter、
`tests/integration/test_attempt_runner.py`、SPEC 超时／恢复相关条款。

验收：

- 取消抛异常或仅确认收到请求时，不得把“取消已请求”当作“进程已退出”。记录可审计失败，
  保留足够的外部句柄／跟踪及工作区占用信息；未确认安全前不能重派冲突工作。
- 在后续 poll 或 daemon 重启恢复后仍能确认退出或明确升级给用户；禁止永久静默等待和无限无退避重试。
- 超时处理先核对 attempt 身份／租约代次；不得释放新持有者的租约。测试覆盖正常取消、取消失败、重启与旧代次。

验证：`uv run pytest tests/integration/test_attempt_runner.py -q`，再运行七道质量门。
产物：回归测试与证据记录；不能只把 `suppress(Exception)` 换成日志后保留相同释放行为。

### R2 · 执行和验证预算按合同、按角色独立记账（M，发布阻断）

依赖：无；实际实施接在 R1 后，避免同时编辑 runner／tick。
涉及 `src/longtask/cli/tick.py`、预算计数 helper、
`tests/integration/test_request_verification.py`、SPEC §12.3–12.4。

验收：

- B2 改为仍有一次 executor 机会；verifier 只消耗该合同验证预算，不侵占 `max_dispatches`。
- 同一 Goal 的其他合同不得消耗本合同预算；验证失败后的 repair → reverify 能走完，不能只测一个计数函数。
- 对没有角色字段的历史事件明确保守兼容规则；重复消费、重启及回放不重复扣减、不创造额外预算。

验证：`uv run pytest tests/integration/test_request_verification.py tests/integration/test_attempt_runner.py -q`，再运行七道质量门。
产物：角色隔离回归与规范说明；不修改用户显式预算、不通过增大默认预算掩盖错误。

**检查点 A：R1、R2 的反例转绿且全量门通过，才进入安装候选验证。**

### R3a · 核实超时终态证据的完整性（M，风险核实）

依赖：R1。涉及 runner、tick 与 `tests/integration/test_attempt_runner.py`；需要语义选择时先补 SPEC。

验收：

- 对“进程已经结束，daemon 晚于时限才第一次观察”建立明确规则与测试；没有可信结束时间时不能宣称按时成功。
- verifier 超时／启动失败的终态事件携带角色、合同修订和 attempt 身份，裁决器不会因缺字段静默忽略。
- 失败或证据不足应形成可读结果／修复或升级路径，不能自动判通过，也不能无限派生 verifier。

验证：定向运行 attempt runner 和 request verification 集成测试。
此项目前是源码审查风险，不标成已经完整复现的第三个缺陷；若证实行为正确，保留测试即可关闭。

### R3b · 验证多合同隔离和接力边界（M，首发验收）

依赖：R1、R2。涉及 `tests/integration/test_request_verification.py`、
`tests/integration/test_goal_auto_advance.py`、`tests/conformance/test_goal_stage_binding.py`；
若发现缺陷再按最小切片修对应 handler。

验收：

- 同时管理两份不同工作区合同，一份等待／失败不能改写另一份状态、预算或证据；两份合同均可被检查和取消。
- 检查同 Goal 的 verifier 进行中查询仍按 `goal_id` 拦截的路径：区分合理的阶段依赖限制与不合理的跨合同拒绝。
  若需要共享资源锁，必须明确按资源拒接，不能把资源限制伪装为本合同已有 verifier。
- daemon 重启后接力保持已提交 checkpoint／证据；旧 attempt 写回被拒，不重复启动已消费的验收请求。

验证：上述三个测试文件；追加无需 LLM 的真实双子进程运行记录。
共享工作区任意并行写入安全仍不在首发声明中，不能用不同工作区测试推导该能力。

### R4a · 完成一个可复制的本地执行示例（M，首次使用）

依赖：R1–R3。涉及一个新的通用示例目录、对应集成测试及中英文 README 链接。

验收：

- 保留当前已隔离的 `drafted → cancelled` 控制面示例，另提供明确区分的真实执行示例。
- 示例使用普通程序与独立 verifier，无模型账号；工作区、角色能力、解释器、验收、预算和调度条件完整。
- 新目录可依次完成 doctor → prepare → approve → execute → verify → satisfied；失败时给出排查出口。

验证：在独立数据目录从头执行，验证产物内容及终态，不使用人工补写成功事件的 harness。

**状态（2026-09-11）**：交付物已就位——`examples/local-no-model/`
（`run_example.py` + `worker.py` + `checker.py` + 中英 README）与
`tests/integration/test_local_no_model_example.py`（断言终态、交付物内容、
executor+verifier 两条真实 attempt、以及 `attempt/*`/`verification/*` 事件里
**不存在 `actor=user` 的成功记录**——成功不是被写进去的）。中英文入口链接已加进
两个 README。R4b（模型侧说明清理）与 R3/R5/R6 仍未完成，本项不改变整体放行状态。

### R4b · 清理模型侧接入说明（S，首次使用）

依赖：D0、R4a。涉及 `skills/long-horizon-goals/SKILL.md` 和 `skills/longtask-contract/SKILL.md`。

验收：

- 统一产品口径并保留新旧工具兼容；明确 Goal 已持久化、调度不依赖模型。
- 删除旧“立即批准”“handover 是唯一真相”“DESIGN 是唯一权威”“succeeded 即 complete”等与现行规范冲突的指引；
  请求入队、消费（含拒接）、实际派发、验收通过分别描述。
- 模型不能以用户身份自授权，也不能通过伪造工作量或缩短 Deadline 强行触发派工。

验证：Skill 校验器、`tests/unit/test_p6_plugin_package.py` 以及 MCP 集成测试。

### R5 · 最终候选安装与兼容冒烟（M，发布验收）

依赖：R1–R4。涉及构建制品检查、P6 安装证据及必要的 CI 配置修正。

验收：

- 对最终候选重新运行 `uv build`、`scripts/check_artifacts.py`；在干净虚拟环境安装 wheel，
  从仓库之外验证 CLI、MCP initialize/tools/list 和 R4a 示例。不以源码环境成功代替制品成功。
- 验证 companion 的安装前提、manifest、两个 Skill 和新旧入口；运行时版本与插件 SemVer 差异明确记录。
- 获取所声明平台的 CI 证据；审查当前 Python CI 中的 Rust cache 步骤是否合理，未实测平台明确标“未验证”。
  本轮 Windows 测试不能替代 Linux/macOS 或全部 Python 3.11+ 版本验证。

验证命令：`uv run python scripts/quality_gate.py`、`uv build`、
`uv run python scripts/check_artifacts.py`，再执行隔离 wheel 冒烟。
产物：候选提交、环境、制品 SHA-256、完整命令／结果；版本变化后不得沿用旧候选证据。

### R6 · 安全与发布决策（M，发布门）

依赖：R5。涉及 SECURITY、CHANGELOG、发布说明、claims／证据和远端配置核对。

验收：

- 扫描将公开的工作树和 Git 历史中的凭证、个人会话记录与未脱敏运行产物；核实分发文件清单。
  核对 `.lhgp` 等状态目录的忽略规则。运行依赖漏洞审计并记录数据日期；七道门中的依赖白名单检查不能代替漏洞审计。
- 检查 default-deny、用户批准、旧写回拒绝、停止／取消和状态恢复的证据。已知控制权、预算、数据完整性或高危安全缺陷为零；
  无法验证的高风险项不能直接打勾，须补证据或缩小并明确发布范围。
- 发布说明完整列出 Developer Preview 边界、安装步骤、已知非阻断限制、回退办法和支持环境。
  确定实际 GitHub 仓库、版本／tag 及发布内容后再发布；不推断同时发布 PyPI 或插件市场。

**放行规则：** R1–R6 验收全部满足，候选代码冻结，七道门及制品冒烟全绿，证据锚定一致，即可发布。
不再以 E1–E4 尚未实现为由拖延。若仍有阻断项，结论必须是 HOLD，并写出最小剩余动作。

## 5. 发布后增强，不阻塞本次首发

| 顺序 | 能力及实现方向 | 可验证结果 | 依赖 |
|---|---|---|---|
| E1 | Deadline 校准与解释：在现有 snapshot 上收集预测、实际工时、等待和验收耗时，保留低样本降级 | 风险变化可解释；无变化时不重复通知或调用模型；不输出未经校准的精确概率 | 首发、真实运行样本 |
| E2 | 多合同调度：先定义容量、公平性、优先级和工作区冲突规则，再修改候选选择 | 饱和场景无饥饿、无越权抢占、无重复持有同一排他资源 | R3b，独立 SPEC 与测试 |
| E3 | 可选规划器：目标／证据输入 → 结构化计划建议 → 校验／必要审批 → 计划修订事件 | 拒绝越权建议；关闭或失败时已有合同继续工作；重放建议不重复创建合同 | 现有 Goal 计划入口、预算边界 |
| E4 | 合同模板与上手体验：从真实用户失败案例提炼少量模板，提供预算／验收预检 | 新用户能独立准备可执行合同并解释拒接原因 | R4a、使用反馈 |

每项开始前单独拆出 M 或更小任务卡，不在发布前一次性补完。
E2 不引入跨主机；E3 不引入常驻总控模型作为核心依赖。

## 6. 发布后验证与回退

1. 从已公开制品完成一次干净安装和示例执行；核实链接、CLI/MCP 发现、状态读取及停止行为。
2. 出现重复派工、越权、状态损坏或预算越界时停止推广该版本，先阻止新派工并确认外部进程状态。
   Kill switch 不等价于所有进程已退出；按 adapter 提供的句柄和取消机制确认。
3. 停止 daemon 后备份完整数据目录，包括 SQLite/WAL、外部句柄、证据和版本信息。不要直接删除或覆盖旧库。
4. 有已验证兼容版本时回退运行时；schema 不兼容则使用备份副本或保持暂停等待修复，不盲目用旧版本打开新库。
5. 记录撤回／修复原因和替代版本。以上是操作清单，不是本轮已执行或已安排的监控自动化。
