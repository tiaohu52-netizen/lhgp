# LHGP — 限期合同中枢

> [English](./README.md) · 协议名：Long-Horizon Goal Protocol（远期目标协议）

**独立于会话和模型的多 Agent 任务合同运行时，支持远期目标的持续推进与证据化验收。**

用户把结果、验收标准、Deadline、权限、预算和可用执行者写进合同。
LHGP 在本机持有多份合同，决定何时派工、是否重试或升级，保存交接证据，
并依据合同验收结果推进状态。执行工作可以交给不同 Agent CLI，也可以交给普通程序。

调度内核不需要 LLM。LLM 可以帮助起草计划、执行任务或提供语义验收；
它不是维持合同存续和执行调度的必要条件。“远期目标”是主要使用场景，
“限期合同中枢”是产品名称；它的实现形态是一个本地任务合同运行时。

当前为 **Developer Preview（开发者预览）**，不是生产稳定版。
[发布检查](docs/evidence/release-readiness-2026-09-05.md)记录了已复现的发布阻断项；
[可执行计划](docs/RELEASE-PLAN.md)规定修复及发布顺序。质量门通过不等于没有缺陷。

## 它负责什么

- **合同与授权**：持久化结果、预算和验收条件，限定 CLI、模型及执行角色。
- **调度与 Deadline**：计算风险和决策点，在授权内派工、重试或请求用户介入。
- **执行与接力**：跟踪 attempt、租约和外部句柄，给后继执行者提供交接上下文。
- **验收与记录**：运行结构化检查，接收独立 verifier 的证据，保存决策和验收历史。
- **多份合同**：同一本地运行时管理多个合同；Goal 可通过已提交的阶段计划关联多份合同。
- **多个入口**：人通过 CLI 操作；Agent 通过 MCP 使用；平台插件提供接入封装。

这些能力的覆盖范围见[声明注册表](quality/claims.json)及关联测试。
它们不等于已实现跨合同最优调度、任意并行写入隔离或自主语义重规划。

## Goal、Contract、Attempt

| 对象 | 保存什么 | 边界 |
|---|---|---|
| Goal（目标） | 持久化目标、计划修订、阶段及进度 | 一个目标可关联多份阶段合同；当前推进依赖外部提供的计划 |
| Contract（合同） | 一次承诺的结果、授权、预算、Deadline 和验收条款 | 合同修订保留历史；冻结条款不能被执行者随意更改 |
| Attempt（尝试） | 某个执行者或验收者的一次运行、租约、句柄和证据 | 可结束、失败或被替换，不是长期状态的所有者 |

SQLite 是权威存储。上下文摘要和文件投影帮助阅读与接力，
不是让多个模型共同维护一份自由文本作为唯一真相。

典型流程：批准合同 → 到决策点 → 选择获授权的执行者 → 保存产出和交接 →
独立验收 → 满足合同，或在预算内修复／向用户升级。
无需每个调度周期都调用模型；“模型退出码为 0”也不代表合同验收通过。

## 从源码开始

需要 Python 3.11+ 和 uv。在仓库根目录执行：

```text
uv sync --extra dev
uv run lhgp --version
uv run lhgp --data-dir runtime/quickstart doctor
```

下面只验证**无需 LLM 的合同控制面**。它不会批准合同、启动 Agent 或消耗模型额度；
使用的独立数据目录不会覆盖默认合同库。重复执行时请更换合同 ID。

```text
uv run lhgp --data-dir runtime/quickstart prepare --contract-id lt-20260905-quickstart --title "First contract" --objective "Inspect the contract lifecycle" --deadline 2030-01-01T00:00:00+00:00
uv run lhgp --data-dir runtime/quickstart get lt-20260905-quickstart
uv run lhgp --data-dir runtime/quickstart cancel lt-20260905-quickstart
uv run lhgp --data-dir runtime/quickstart get lt-20260905-quickstart
```

预期状态为 `drafted → cancelled`。示例日期仅供控制面演示，使用时须晚于当前时间。

### 真实执行示例（无模型）

上面的控制面演示不执行任何工作。完整链路——
`doctor → prepare → approve → execute → verify → satisfied`，执行者是普通程序、
核验者是**独立**的第二个 attempt，无需模型账号与网络——见
[`examples/local-no-model/`](examples/local-no-model/README.md)：

```text
uv run python examples/local-no-model/run_example.py
```

它返回一个终态合同，交付物由独立核验者判定通过；失败时给出排查出口而不是
只丢一句错误。集成测试 `tests/integration/test_local_no_model_example.py`
硬断言这次成功**不是被人工写进去的**。
简写 `prepare` 生成占位验收项，不能直接当作无人值守执行模板。

### 交给 Agent 执行之前

完整执行合同需要明确的工作区绝对路径、可核对的验收条件、真实工作量估计，
以及已配置且可启动的执行者／独立验收者。通过 `prepare --file` 读取完整 JSON 草稿，
核对权限和预算后再 `approve`，并启动同一数据目录下的 daemon。
批准并不保证立即派工；实际动作由合同状态、调度策略和预算决定。

typed check 的 `target` 相对于合同工作区解析。command check 使用 daemon 环境，
不是自动继承项目虚拟环境；执行命令应明确解释器路径。
执行预算耗尽但产出疑似就绪时，可使用 `lhgp request-verification <id>` 请求验收现状。
这仍需要可用 verifier 和验证预算；请求已入队不代表验收已经开始或通过。

参考[规范 §12](docs/LHGP-SPEC.md)、[本地子进程集成测试](tests/integration/test_attempt_runner.py)
和真实 CLI 执行者与独立 verifier 的端到端运行记录。
归档示例包含当时的环境约定，不应原样复制成本机配置。

### MCP 与插件

安装包提供 `lhgp-mcp`。接入时，让 MCP 宿主能找到该可执行文件；仅运行
`uv sync` 不会把虚拟环境里的命令加入所有应用的 PATH。
仓库的 `.mcp.json` 使用 `lhgp-mcp`，因此插件封装还需要已安装的本地 companion runtime。
CLI、MCP 和插件是同一运行时的不同入口，并非三套调度系统。

新入口为 `lhgp` / `lhgpd` / `lhgp-mcp`，兼容
`longtask` / `longtaskd` / `longtask-mcp`。
新安装默认数据目录为 `~/.lhgp`；旧目录兼容及迁移行为见
[路线图 P6](docs/LHGP-ROADMAP.md)。本轮定位调整不改变命令、包名或数据格式。

## 使用边界

- 单机、单用户；本机 RPC 有 token 认证，不提供面向不可信多租户的网络服务。
- Deadline 是调度和违约记录的边界，不是“保证某时刻做完”的结果担保。
- 本地进程无法在机器关机时执行。休眠唤醒受操作系统、硬件和权限影响；
  当前 Windows L1 适配路径有测试，其他平台会显式报告降级。
- 执行器声明了能力不代表 LHGP 自带完整沙箱；文件、网络和进程隔离须核对实际适配器保障。
- 多份合同可共存，不代表可以安全地让它们任意并行修改同一工作区。
- 当前不会自主理解模糊目标、持续重写阶段计划或做全局资源最优分配。
- 跨主机、跨网络中继不在本轮发布及完善范围；外部通知送达不作保证。

## 检查与排障

```text
uv run python scripts/quality_gate.py
uv build
uv run python scripts/check_artifacts.py
```

质量门依次检查格式、lint、架构、依赖策略、声明、类型和测试覆盖率。
依赖策略门不是漏洞数据库审计；发布还要执行[发布计划](docs/RELEASE-PLAN.md)中的安全检查。

合同卡住时先看 `lhgp doctor` 和 `lhgp get <id>`，再检查
`decision_history`、`attempt_history`、`verification_history`。
`lhgp kill-switch --activate` 阻止新派工，不能据此认定已有外部进程全部退出。
排障时不要删除状态库或把含 token、模型输出的完整数据目录上传到 issue。

## 文档导航

- [权威规范](docs/LHGP-SPEC.md)：协议语义；[ADR-004](docs/decisions/0004-contract-runtime-and-release-scope.md)：此次定位决策。
- [发布与完善计划](docs/RELEASE-PLAN.md)：当前执行顺序；[路线图](docs/LHGP-ROADMAP.md)：长期里程碑与历史。
- [发布检查证据](docs/evidence/release-readiness-2026-09-05.md)：当前检查结果和未完成项。
- [贡献规范](CONTRIBUTING.md)、[安全边界](SECURITY.md)、[变更记录](CHANGELOG.md)、[Apache-2.0 许可](LICENSE)。

源码分布于 `src/lhgp/` 和兼容命名空间 `src/longtask/`；测试位于 `tests/`，
线协议 schema 位于 `schemas/`。规范描述目标设计，不能单凭规范或测试数量宣称生产可用。
