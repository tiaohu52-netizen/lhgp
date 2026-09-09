# 发布准备检查：定位统一与首发阻断项（2026-09-05）

## 结论

**仍未放行，但 B1、B2 已修复。** 可以按 [发布计划](../RELEASE-PLAN.md)
继续准备 Developer Preview；当前剩余阻断是最终候选验收、安全检查、claims 重新锚定和发布目标确认。
自动规划器、跨主机和跨网络不是本次放行条件。

本轮完成产品口径与计划落盘，并修复 B1/B2；未提交、打 tag、推送或发布。
历史报告／claims 的全绿结果不替代本轮修复后的候选验证。

## 检查对象与环境

- 实现基线：`b4b301d965532b10e7c14c256fee3fa5d9539ab7`。
- 文档状态：上述提交基础上的本轮工作树修改，不是一个已经发布或打 tag 的候选。
- 环境：Windows、PowerShell、CPython 3.13.13；依赖由仓库 uv 环境提供。
- claims 当前锚点：`bfa14086cd007e7482f186cfa797b4cf22c08454`，不等于本轮所有检查均已重新锚定。
- 故障注入仅使用临时 SQLite 和 mock adapter，不连接实际 Agent、模型或用户合同库。

## 已执行检查

| 检查 | 结果 | 证明范围 |
|---|---|---|
| 七道质量门（修复后） | 7/7；636 测试；82.24% 覆盖率；108.91 秒 | 本轮修复后的工作树；claims 尚未重新锚定 |
| 修复后质量门复验 | 7/7；636 测试；82.24% 覆盖率；108.91 秒 | 运行时修复与新增回归测试后的工作树；claims 尚未重新锚定 |
| 文档静态检查 | 11 份文档的本地 Markdown 链接存在；git diff --check 通过 | 引用可达与空白格式，不代表每一项发布检查已执行 |
| 新 README 控制面示例 | doctor 五项通过；prepare/get 得到 drafted revision 1；cancel/get 得到 cancelled revision 2 | 独立数据目录、无获授权执行者、无模型调用；不证明真实执行／验收闭环 |
| wheel + sdist 构建 | 成功；`scripts/check_artifacts.py` 对两项均报告 companion resources OK | 本轮工作树制品的构建、内嵌资源／metadata 和入口静态检查，不是最终候选隔离安装 |
| B1 回归测试 | 取消失败时 attempt=`orphaned`、租约保留，定向测试通过 | mock adapter；真实外部进程仍需 R3/R5 验证 |
| B2 回归测试 | executor 配额仍可 dispatch，定向测试通过 | 事件级计数与调度集成；不是完整 CLI dogfood |

控制面示例数据位于忽略目录 `runtime/quickstart`，合同 ID 为
`lt-20260905-quickstart`；未执行 approve 或启动 daemon。
首次试写的 `lt-quickstart-001` 被格式校验拒绝；README 已修正为实际验证通过的 ID。
这属于本轮文档校验中修正的错误，不作为运行时缺陷计数。

构建命令：

```text
uv build --out-dir runtime/release-readiness/dist
uv run python scripts/check_artifacts.py runtime/release-readiness/dist
```

制品仅留在上述忽略目录，未上传。构建发生在本轮文档整理期间，不是 R5 所要求的最终冻结候选。
旧 [P6 安装记录](P6-fresh-machine-smoke-2026-09-05.md) 对应 `c5d23fa` 后的构建，
不能直接当作当前版本已完成干净安装的证据。

## B1：超时取消失败后的控制权保护（已修复）

位置：`src/longtask/cli/runner.py` 的 `AttemptRunner.poll_attempts` 与 `_fail_attempt`。

输入：合同 attempt 时限 1 分钟；已有运行 attempt 和有效租约。
adapter 的 cancel 抛出 OSError，observe 若被调用则会报告仍在运行；
在开始后 2 分钟调用 poll。实际结果：

```text
回归结果：state=orphaned，lease_retained=true，attempt/orphaned 已记录。
```

修复：取消结果不确定时将 attempt 标记为 `orphaned`，保留当前租约并记录
`attempt/orphaned`；Runner 停止把它当作已终止，后续由 reconcile 的宽限／fencing
路径确认外部状态或安全让位。回归测试
`test_timeout_cancel_failure_orphans_attempt_and_retains_lease` 已通过。

## B2：验证事件挤占执行预算（已修复）

位置：`src/longtask/cli/tick.py` 的 `run_daemon_tick`。

输入：`max_dispatches=2`，当前合同历史各有一条带角色的
executor `attempt/started` 和 verifier `attempt/started`，没有活动租约。
按执行／验证预算独立语义，仍应有一次 executor 机会。实际结果：

修复前结果为 `dispatched=0`、`state=blocked(need-user)`；修复后同一场景为
`dispatched=1`，合同保持 `active`，并由新增回归测试固定。

修复：tick 只把非 verifier 的 `attempt/started` 计入 executor
`max_dispatches`；verifier 使用合同独立的 `verification_attempts_reserved`。
回归测试 `test_verifier_started_does_not_consume_executor_dispatch_budget` 已通过。

## 可复现脚本

从基线仓库根目录，在已安装 dev 依赖的 Python 环境执行以下脚本；它记录的是
故障注入场景，当前工作树预期输出已由正式回归测试断言。
它复用仓库测试 fixture，并在退出时清理自己创建的临时目录。
fixture 的合同 ID 是内部诊断标识，直接写测试存储，不经过公共 CLI 的 ID 校验。
以下程序打印观察结果；移入正式回归测试时，应断言期望的安全行为，而不是断言错误结果。

```python
import json
import sys
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from tests.integration.test_attempt_runner import NOW, make_contract, make_registry, open_store
from longtask.cli.daemon import run_daemon_tick
from longtask.cli.runner import AttemptRunner
from longtask.persistence.attempts import set_attempt_state
from longtask.persistence.events import EventType
from longtask.persistence.store import append_event, get_contract, get_lease

with TemporaryDirectory(prefix='lhgp-audit-') as temporary:
    root = Path(temporary)
    registry = make_registry(argv=(sys.executable, '-c', 'pass'))
    make_contract(root, 'lt-audit-timeout', max_attempt_minutes=1)
    conn = open_store(root)
    try:
        first = run_daemon_tick(root, conn, registry, now=NOW)['attempts_started'][0]
        attempt_id = first['attempt_id']
        set_attempt_state(conn, attempt_id=attempt_id, state='running', now=NOW)
        adapter = Mock()
        adapter.cancel.side_effect = OSError('injected cancellation failure')
        adapter.observe.return_value = {'state': 'running'}
        runner = AttemptRunner(root, conn, registry)
        runner._adapters['exec-1'] = adapter
        runner._running[attempt_id] = {'contract_id': first['contract_id'], 'executor_id': 'exec-1', 'model': '*', 'role': 'executor', 'contract_revision': 1, 'session_ref': 'audit-only', 'generation': get_lease(conn, first['contract_id']).generation}
        runner.poll_attempts(NOW + timedelta(minutes=2))
        row = conn.execute('SELECT state, error_class FROM attempts WHERE attempt_id = ?', (attempt_id,)).fetchone()
        print(json.dumps({'probe': 'cancel-failure', 'state': row[0], 'error_class': row[1], 'lease_retained': get_lease(conn, first['contract_id']) is not None, 'still_tracked': attempt_id in runner._running, 'observe_calls': adapter.observe.call_count, 'cancel_calls': adapter.cancel.call_count}))
    finally:
        conn.close()

with TemporaryDirectory(prefix='lhgp-audit-') as temporary:
    root = Path(temporary)
    registry = make_registry(argv=(sys.executable, '-c', 'pass'))
    cid = 'lt-audit-budgets'
    make_contract(root, cid, max_dispatches=2)
    conn = open_store(root)
    try:
        for role in ('executor', 'verifier'):
            append_event(conn, contract_id=cid, attempt_id='audit-' + role, event_type=EventType.ATTEMPT_STARTED, payload={'role': role}, role=role, now=NOW, actor='daemon')
        result = run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=1))
        contract = get_contract(conn, cid)
        print(json.dumps({'probe': 'role-budget', 'max_dispatches': 2, 'executor_started': 1, 'verifier_started': 1, 'dispatched': result.get('dispatched'), 'state': str(contract.state), 'blocked_reason': str(contract.blocked_reason), 'events': result.get('events')}))
    finally:
        conn.close()
```

## 待核实风险，不冒充已复现缺陷

- 超时分支在 observe 前运行：进程已结束但晚轮询时如何裁决，需要明确可信结束时间和 Deadline 的关系。
- 通用失败事件缺少 verifier role／contract revision：检查是否导致裁决器忽略失败、缺失修复或升级记录。
- `contract/request-verification` 的进行中 verifier 查询仍按 goal_id 拦截：
  必须区分阶段依赖限制与本应独立的合同验收，不能未经场景测试就断言全部跨合同调用都错误。

这些均纳入 R3 的核实测试，不增加“已复现缺陷”数量。

## 本轮尚未完成的发布检查

最终候选的干净 wheel 安装、完整无 LLM 双合同运行、多平台 CI 结果、
当前插件／Skill validator 复验、Git 历史与分发内容脱敏扫描、
漏洞数据库审计和实际 GitHub 发布目标确认尚未完成。
仓库的 deps 门验证依赖策略，不查询漏洞数据库；不能据此报告“无高危漏洞”。

dogfood v5 展示了一个三阶段目标完成，不等于三个独立真实目标的全部断裂考验，
也不证明全局多合同调度、任意共享工作区并行安全或自主规划能力。
发布门槛与最小后续动作统一在 [RELEASE-PLAN.md](../RELEASE-PLAN.md)，避免多份清单各自宣布完成。

## 修复后验证

- 定向集成测试：`test_attempt_runner.py`、`test_p5_repair_loop.py`、
  `test_request_verification.py`、`test_subprocess_reattach.py`、`test_reconcile.py` 全部通过。
- 全量质量门：7/7 通过，636 tests，82.24% coverage。
- 本地候选提交已创建；claims 正在重新锚定到该候选，远端仍未配置。

## 最终候选制品验证（2026-09-05）

- `scripts/claims_check.py`：43 条声明通过，锚定本地候选提交 `872cb325e6690cdcb10adeffd6b4b1aa8aac5052`。
- `uv build --out-dir runtime/release-readiness/final-dist`：wheel 与 sdist 均成功；
  `scripts/check_artifacts.py` 两项均报告 companion resources OK。
- 全新 `runtime/release-readiness/final-venv` 安装 wheel 成功。
- 隔离安装中 `lhgp --version`、兼容入口 `longtask --version` 和 `lhgp doctor` 全部通过；
  doctor 结果为 `ALL SYSTEMS GO`。
- 隔离安装中的 MCP `initialize` 和 `tools/list` 成功，发现 34 个工具，
  包含 `lhgp_doctor`、`lhgp_request_verification`、`longtask_get_contract`。
- 敏感模式扫描未发现实际凭证；命中内容仅为归档示例中声明的环境变量名，发布前仍需人工复核公开归档是否应保留。
- 仓库当前无 Git remote；本记录因此不宣称已推送或已发布 GitHub。

## 追记：remote 配置与推送尝试（2026-09-05，总控）

- 上述「无 Git remote」记录之后，用户提供了发布仓库地址并配置 remote：
  `origin = https://github.com/tiaohu52-netizen/lhgp.git`。
- 总控于 2026-09-05 执行推送前复查：跟踪文件与全历史新增文件名无凭证模式；
  归档示例内容无密钥值命中（与上方敏感扫描结论一致）。
- 推送 `main → origin/main` **失败**：GitHub 拒绝密码认证（不支持 password auth），
  用户提供的凭据为账号密码格式；本机 `~/.ssh/id_rsa.pub` 未绑定 GitHub 账号。
  需要用户提供 PAT（repo 写权限）或绑定 SSH 公钥后重试。
- 本记录只代表源码推送尝试，不构成发布、打 tag 或 Release。

### 追记二：SSH 通道打通并完成推送（2026-09-05，总控）

- 远端改为 SSH（`git@github.com:tiaohu52-netizen/-.git`）。本机新生成
  ed25519 密钥（`~/.ssh/gh_lhgp_key`，指纹见 `ssh-keygen -lf`），由用户在
  GitHub → Settings → SSH keys 手动上传后认证通过。
- 远端仅有 GitHub 自动初始化提交 `903988a`（README.md + LICENSE），
  经检查无可保留内容后 force push 覆盖：`903988a...6606197 (forced update)`。
- `main` 现跟踪 `origin/main`，两端一致于 `6606197`。仍不构成发布、打 tag 或 Release。

## v0.1.0a0 公开发布记录（2026-09-05，总控）

**发布已完成**：https://github.com/tiaohu52-netizen/lhgp/releases/tag/v0.1.0a0

- **产品定名**：限期合同中枢 / Deadline Contract Hub（ADR-004 修订记录），
  双语 README、SPEC 首屏、发布计划、一页纸统一；协议名 LHGP 与包标识不变。
- **发布提交与 tag**：`v0.1.0a0` → `f2af524`（与 main 一致）；tag 初建于
  `aad8fdd`，CI 修复后移动到最终提交，Release 对象重新发布绑定。
- **CI 首跑暴露并修复的真实缺陷**（此前 CI 从未真正运行过）：
  1. `--help` 中文文本在 cp1252 控制台 UnicodeEncodeError 崩溃（新入口
     `longtask.console.harden_stdio` + cp1252 回归测试；默认 Windows 用户可复现）。
  2. `executor/health` 集成测试依赖机器安装的 codex CLI（fixture 改用解释器）。
  3. Linux mypy 把 `ctypes.windll` 分支报错（mypy 平台钉死 win32，POSIX 行为由三平台测试矩阵覆盖）。
  4. Linux 僵尸进程被 `kill(pid,0)` 误判存活：分离 run 永远 running、
     daemon 停止总被升级强杀（`/proc` 状态字段 + `waitpid(WNOHANG)` 收尸判定）。
  5. `/proc` st_ctime 在进程退出时变化导致身份比对失败（改用 stat 字段 22 稳定启动时间）。
  6. 已收尸的死 run 被 reattach 拒绝绑定，违反文档声明的分支 2 语义
     （pid 必然已终止时按终止绑定；活的 pid 复用者仍拒绝）。
  7. CI 中无用 Rust cache 步骤（删除）。
- **平台口径（诚实声明）**：Windows 全面验证；Linux CI 全量门+测试绿；
  macOS 为已知缺口平台（无 /proc 身份等价物、AF_UNIX 路径限制），
  CI `continue-on-error` 非阻断跟踪，发布说明与 CHANGELOG 同步标注。
- **最终 CI**：`ci=success`、`quality=success`（`f2af524`；windows/ubuntu 阻断绿，
  macos 非阻断）。
- **制品**（由 `f2af524` 构建，`scripts/check_artifacts.py` 两项 OK）：
  - `longtask_protocol-0.1.0a0-py3-none-any.whl`
    SHA-256 `f9b0aa3be5bfc256f386c397f8516d4e6a0c2dfd3b1cff5b5eb238daa8b2fb27`
  - `longtask_protocol-0.1.0a0.tar.gz`
    SHA-256 `69e893b1c0b19aa88fb0eda85e501a9bda5fec4b577bc4e89c83261824a0f27a`
- **仓库状态**：公开（发布时由总控经用户授权的 API 调用转 public）；
  未经外部漏洞数据库审计（运行时依赖为 0、开发依赖全锁定白名单），
  此项按 R6 口径保留为后续工作。
- 本地七道门：7/7（637 tests / 82.15% coverage）多次复验通过。

## 追记：历史脱敏后，本记录内的提交号已失效（2026-09-10）

本仓库在 2026-09-05 做过一次脱敏历史重建（reflog 见
`chore: repair identifiers after history sanitization`）。重建只改写了提交对象，
没有同步任何文档里的提交号，因此本记录与同目录下其它证据文件中出现的
`b4b301d` / `bfa1408` / `872cb32` / `f2af524` / `c5d23fa` / `280d788` /
`903988a` / `6606197` 等 SHA **在当前历史中已不存在**（`git cat-file -t` 报
bad object）。它们是当时的真实观察结果，但不再可被后人 checkout 复核。

处理方式：

- 不再逐个改写历史证据中的 SHA——那等于篡改当次观察记录。
- **机器可校验的锚点只有一个**：`quality/claims.json` 根级 `pinned_sha`。
  自 2026-09-10 起，claims 门用 `git merge-base --is-ancestor <sha> HEAD`
  校验它必须存在且从 HEAD 可达，回归见
  `tests/unit/test_claims_anchor_check.py`；CI 的两个 workflow 相应改为
  `fetch-depth: 0`，否则浅克隆会让诚实的锚点看起来是坏的。
- 需要复核某次结论时，以当前 `pinned_sha` 指向的候选为准重跑命令，
  不要试图 checkout 本记录里的旧 SHA。
