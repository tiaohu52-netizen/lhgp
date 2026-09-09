# Contributing

> By contributing you agree to the project license ([Apache-2.0](LICENSE))
> and the [Code of Conduct](CODE_OF_CONDUCT.md).
>
> English version of this file lives in `CONTRIBUTING.md`; the Chinese
> explanation below is kept for the original-author voice and local onboarding.

本仓库是协议规范 + 参考实现。**设计本体是 [DESIGN.md](DESIGN.md)**：代码与
文档冲突时，先改设计并过审批，再改代码。不要引入设计文档没有的概念。

## 环境准备

- Python ≥ 3.11，工具链统一走 [uv](https://docs.astral.sh/uv/)。
- `uv sync --extra dev` 一键建环境；不要往系统 Python 里装依赖。
- 独立成仓后：`uv run pre-commit install` 启用提交钩子。
- 不锁定解释器与依赖版本的 PR 不收（DESIGN §13.1）。

## 质量门

权威全量门，本地与 CI 同一命令，任何一门失败即停：

```bash
uv run python scripts/quality_gate.py
```

门的固定顺序与各自职责：

| # | 门 | 查什么 | 失败意味着什么 |
|---|----|--------|----------------|
| 1 | format | `ruff format --check` 全量 | 代码格式不合，跑 `uv run ruff format` |
| 2 | lint | `ruff check` 全量 | 规则违规，规则集在 pyproject.toml |
| 3 | arch | `scripts/arch_check.py` | 模块依赖方向违反四平面边界（见下） |
| 4 | deps | `scripts/deps_check.py` | 依赖不在白名单 / 版本未锁定 |
| 5 | claims | `scripts/claims_check.py` | 质量声明与证据不符 |
| 6 | typecheck | `mypy --strict` | 类型债（strict 起步，无存量豁免） |
| 7 | test + coverage | `pytest --cov`，覆盖率棘轮 | 测试失败或覆盖率低于基线 |

## 编辑器 / MCP 客户端接入

仓库根的 `.mcp.json` 用 `command: "lhgp-mcp"`，依赖 PATH 上能解析到
`lhgp-mcp.exe`（本仓库 `uv sync --extra dev` 后落在 `.venv\Scripts/lhgp-mcp.exe`）。
两种稳定做法：

- 在激活的 venv 里跑编辑器（VSCode/Cursor/Claude Code 自动从 venv 解析）；
- 或 `uv tool install .`（或把 `.venv/Scripts` 加到 `PATH`），让 `lhgp-mcp` 全局可解析。

若要走绝对路径，请同步改 `tests/unit/test_p6_plugin_package.py::TestMcpConfig`
对 `command` 字段的断言；该测试是 P6 范围的契约守门，单独动 `.mcp.json`
会被拦下。

## 发布前 P6 验收

发布候选除质量门外，还必须从仓库根目录执行：

```bash
# 插件清单（Codex 官方 validator；Windows 建议强制 UTF-8）
python -X utf8 <plugin-creator>/scripts/validate_plugin.py .

# wheel/sdist 必须携带插件、MCP 与 Skill companion 资源
uv build
uv run python scripts/check_artifacts.py dist

# 无密钥的多 CLI 授权探针
uv run lhgp doctor
```

真实外部 CLI/API 的 daemon dogfood 仍需单独准备凭据，并把结果记录在
`docs/evidence/`；没有这些证据不得把 Developer Preview 宣称为 Alpha。

门的行为准则（继承自参考实践，三条铁律）：

1. **fail-closed**：工具缺失、清单读不到、环境不对 → 门报错退出，
   绝不「假装通过」。骨架期也不允许。
2. **棘轮（ratchet）**：存量问题记录基线（`quality/` 下），只允许收紧
   不允许放松；修掉存量后必须下调基线，不得静默放宽。
3. **快路径不撒谎**：pre-commit 钩子只做增量快检，其通过不代表
   全量门通过；PR 以 `quality_gate.py` 全绿为准。

铁律 2 说的「`quality/` 下的基线」当前是三个可机器校验的文件，各自的读取方
也在那里：

| 基线 | 谁读它 | 收紧方向 |
|------|--------|----------|
| `quality/claims.json` | `scripts/claims_check.py` | `pinned_sha` 必须从当前历史可达，重验后重锚 |
| `quality/coverage-baseline.json` | `scripts/quality_gate.py` | `fail_under` 只许上调，且不得超过 `last_measured` |
| `quality/real-entry-baseline.json` | `tests/conftest.py` | 未标记的真实入口测试数只许下调 |

## 审查修复强制纪律

- 门红了先修根因，不许为消红而加豁免、注释规则、调阈值。
- 确需豁免（如某个 S 规则在特定文件不适用）：在 pyproject.toml
  per-file-ignores 里写明**原因注释**，并在 PR 描述里单独说明。
- 设计级分歧（门的要求与设计冲突）回 DESIGN.md 走审批，不改门迎合代码。

## 模块边界约束（arch 门强制）

四平面分层（DESIGN §3），依赖方向只允许自上而下：

```
cli → rpc → promoter / scheduler → adapters → persistence → contracts
```

- `contracts`：纯数据与校验，零业务依赖，不许 import 其他业务层。
- `persistence`：唯一碰 `state.db` 的层；其他层禁止直连数据库、
  禁止绕过它改合同状态（DESIGN §3.1 人类编辑门）。
- `adapters`：只依赖 `persistence` 的公开接口与 `contracts`；
  不得 import `promoter`/`scheduler`/`rpc`。
- `scheduler` 不做执行：不得 import `adapters`。
- `promoter` 调度执行器只经 `adapters` 的公开接口，不得 import 具体实现。
- 任何 `src/` 层（包括 `src/longtask` 与 canonical `src/lhgp` facade）不得 import `tests`。
- `src/lhgp` 当前是迁移 facade：只能转发到唯一的 `longtask` 实现，禁止复制状态或业务逻辑。
- 新增跨层依赖：arch 门会红；确认是设计演进就先改 DESIGN 与本节。

## 测试纪律

- 三层测试目录对应三个 marker：`unit`（纯逻辑、毫秒级）、
  `integration`（SQLite/文件系统/子进程）、`conformance`（协议一致性：
  拒接、fencing、崩溃恢复——DESIGN §14 每条保证至少一个场景）。
- 测试必须确定性：无随机数、无真实墙钟睡眠、无网络。时间一律注入。
- 假数据用 fake executor（`adapters/fake_executor.py`），不许 mock 掉
  被测对象本身。
- 「有测试」不算保证：每条 DESIGN §14 保证必须在 claims 注册表里有
  对应证据条目，证据未跑记 `deferred`，绝不记 `verified`（见下）。

## 质量声明注册表（quality/claims.json）

治理真相源。每条声明：生命周期（`verified` / `deferred` / `blocking` /
`accepted_debt`）、证据（仓库相对路径，验证时必须存在）、锚定提交。

- 没跑的检查写 `deferred`，写 `verified` 等于撒谎，claims 门会拦。
- 证据是追加式的：重跑后加新证据条目，保留历史，更新生命周期。
- `accepted_debt` 必须带完整 debt_policy（原因、owner、复审日期、
  再触发条件）；「不好拆」不是治理理由。

## 提交与版本

- commit message：`type: 中文摘要`，type ∈ feat/fix/chore/docs/gate/refactor。
  涉及基线调整（棘轮下移）的提交在正文说明理由。
- 协议语义变更必须先过 DESIGN.md 审批（文档头修订号 + §19 审批记录），
  再改 schema 与代码；schema 变更必须递增 `schema_version`。
- 版本号：pyproject.toml 的包版本与协议 `protocol_version` 独立演进，
  各自遵守 semver 语义。

## 文档分层

- `DESIGN.md`：协议规范本体（唯一权威）。
- `README.md`：给路过的人看的定位与快速开始。
- 本文件：给写代码的人看的准入标准。
- `docs/decisions/`：ADR，记录「为什么这么做」，不记录「做了什么」。
- 代码 docstring：引用 DESIGN 章节号，不复述设计。
