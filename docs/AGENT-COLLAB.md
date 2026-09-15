# 多 Agent 协作开发协议（以 LHGP 管理 LHGP 的开发）

> 本仓库就是这份协议的第一个案例。下述每一条失败模式都在本仓库的开发史上
> 真实发生过，每一条对策都是已经实现的 LHGP 机制——不是愿景。

## 1. 真实失败模式（本仓库自己的历史）

| # | 失败模式 | 本仓库的真实案例 |
|---|---|---|
| F1 | **改动无验收标准**：agent 直接改实现、零测试，报告「完成」 | P3 阶段外部 AI 交付连续性闭环，实现存在但测试为零，核心模块覆盖率 26% |
| F2 | **谎报完成**：agent 声称全绿，实际多门红 | 2026-09-03 阶段验收绑定交付：报告称「ruff/mypy/claims 通过」，实测 format 与 mypy 两门红、绑定功能崩溃（TypeError） |
| F3 | **环境盲区**：agent 只在本地验证，跨环境即坏 | 「CI 覆盖三平台」写了数周，v0.1.0a0 发布日 CI 首跑三平台全红（cp1252 崩溃、僵尸进程误判、reattach 拒绑等 7 个真实缺陷） |
| F4 | **无证据链**：说不清哪个 agent 改了什么、为什么 | 数百条提交混杂多个 agent，spec 与实现漂移无人察觉 |
| F5 | **破坏半径失控**：一次错误操作波及全局 | 仓库 .git 历史被外部 agent 重建两次，此前提交证据全部丢失 |

## 2. LHGP 机制 → 对策映射

| 失败模式 | LHGP 机制 | 为什么有效 |
|---|---|---|
| F1 无验收标准 | **合同 acceptance 即可执行命令**（SPEC §12.1 typed check） | 改动前必须先定义「怎样算完成」，且是机器可判定的命令，不是自然语言感觉 |
| F2 谎报完成 | **独立 verifier 交叉验收**（§5.2）：验收命令由 daemon 派生的独立 verifier 重跑 | 执行者说 succeeded ≠ 验收通过；verifier 只认命令退出码与证据 |
| F3 环境盲区 | **daemon 环境独立执行** + CI 矩阵作为验收命令之一 | 检查跑在 daemon 的环境里，不是 agent 的会话里；跨平台问题由 CI check 暴露 |
| F4 无证据链 | **events 事件链 + contract.yaml/handover.md 投影**（§11.3） | `lhgp get` 与工作区投影文件完整记录：谁被派工、产出是什么、verifier 判了什么、为什么 block |
| F5 半径失控 | **authority 绑定（default-deny）+ budget 硬预算 + max_attempt_minutes 硬超时** | agent 只能在合同授权内被唤起；重试次数、时长、输出体量都有硬上限，触顶自动转 blocked(need-user) |

一句话：**改动的授权书（合同）+ 机器可判定的验收（typed check）+ 独立复核（verifier）+ 全程留痕（events/投影）+ 硬止损（预算/超时）**。

## 3. 操作手册（命令级）

### 人类：立一份改动合同

```bash
cp templates/code-change-contract.json my-change.json
# 编辑 my-change.json：填 objective、workspace_root、venv 路径、验收命令
lhgp prepare --file my-change.json --contract-id lt-20260905-change01
lhgp approve lt-20260905-change01
lhgpd start   # daemon 接管：到决策点自动唤起被授权的 agent CLI
```

- `acceptance.checks` 至少包含：新增/受影响测试的 pytest 命令 + `scripts/quality_gate.py`。
- `budget.max_dispatches` 建议 2-3：给 agent 修复机会，但不给无限重试。
- 未写 authority 绑定的合同默认不限制执行者池（兼容语义）；要收紧就显式绑定 `{executor: role}`。

### Agent：被唤起后的义务

1. 只改 `hard_constraints.file_effects.workspace_root` 内的文件；
2. 让 acceptance checks 全部通过——这是合同，不是建议；
3. 通过 `attempt/write-back` 写回进度与终态（不得自报合同完成态——verifier 负责判定）；
4. 把「改了什么/为什么/风险」写进交接材料（handover）——这是下一个 agent 的输入。

### 任何 agent / 人类：知道其他 AI 改了什么

```bash
lhgp get <contract_id>                       # 合同全貌：状态/修订/决策/attempt 历史
lhgp watch <contract_id>                     # 事件流实时滚动（全程可审计）
```

- `contracts/<id>/contract.yaml`：合同当前快照（含冻结区原文）；
- `contracts/<id>/handover.md`：上一个执行者留下的交接（改了什么、剩余风险）；
- `contracts/<id>/log.jsonl`：完整事件链（prepared→approved→started→succeeded→verified→completed）。

### 出问题：自动止损与人工接管

- verifier 连续判失败 → repair brief 自动带上失败证据交下一轮；
- 验证预算耗尽 → 合同自动转 `blocked(need-user)`，daemon 停止派工；
- 需要人工裁决（延期/采纳/作废）：`lhgp arbitrate <contract_id> ...`。

## 4. 对 agent 的三条硬约定（模型侧 Skill 同款纪律）

1. **不得自批准、自修订**：`approve/patch` 是用户（Principal）动作，模型调用返回 AUTH_FAILED 是协议在工作，不是故障；
2. **不得自报完成**：`attempt/write-back` 不能把合同写成完成态——完成只能由 verifier 裁决；
3. **不得越界写文件**：workspace 之外的一切写操作在 prepare 阶段就会被拒接。

## 5. 与本仓库开发流程的关系

- 本仓库的 CHANGELOG / docs/evidence/ 就是 F1-F5 对策在「文档层」的手工形态；
- 本协议的目标是让这套纪律由运行时强制执行，而非依赖每个 agent 的自觉；
- 后续增强（RELEASE-PLAN E3）：模型规划器只能提交「计划提案」事件，经校验与审批后才落入权威状态。
