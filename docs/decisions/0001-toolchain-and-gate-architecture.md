# ADR-001: 工具链与质量门架构

## 状态

Accepted

## 日期

2026-08-31

## 背景

远期任务协议需要一套代码规范与准入机制，在写实现之前先把门立起来
（用户明确要求「先搭建好规范和标准门才好进行下一步」）。参考了
world-agent-v6（TS/RN 项目）的准入机制，但本项目是 Python 本机守护进程 +
协议规范仓库，场景不同，需要取舍。

## 决定

1. **工具链**：uv 管理解释器与依赖（本机无全局 Python，uv 已就位）；
   ruff（format+lint）、mypy（strict 起步）、pytest、pre-commit。
   运行时零第三方依赖（DESIGN §13.1 精神：stdlib 足够 v0.1）。
2. **质量门**：单一编排入口 `scripts/quality_gate.py`，固定顺序
   format → lint → arch → deps → claims → typecheck → test+coverage，
   本地与 CI 同一命令，fail-closed，任一失败即停。
3. **三条铁律**：fail-closed；棘轮基线只紧不松；增量快检不声称全量通过。
4. **质量声明注册表**：沿用参考项目的 claims 理念——每条治理声明带
   证据、生命周期、锚定提交；未跑的检查记 `deferred` 绝不记 `verified`。
   与 DESIGN §14「保证必须可观察」同构。
5. **架构门**：用 ast 静态分析 import 方向（而非参考项目的 eslint 规则），
   因为 Python 生态没有等价 lint 规则集，且我们要管的是「谁 import 谁」
   这一件事，ast 直接、零依赖、fail-closed。

## 考虑过且拒绝的方案

### 照搬 world-agent-v6 的 npm/husky 体系

- 拒绝理由：语言生态不同；其核心思想（棘轮、fail-closed、claims）
  可取，工具全部替换为 Python 等价物。

### pylint / flake8 组合代替 ruff

- 拒绝理由：ruff 单一工具覆盖 format+lint 且快，规则集（含 bandit S 族）
  对本协议的子进程/路径处理场景开箱即用；多工具组合增加门编排复杂度。

### mypy 非 strict 起步、存量豁免

- 拒绝理由：骨架期没有存量，strict 零成本；「先松后紧」历史上从不发生，
  棘轮机制只允许反向走。

### 把架构规则写进 ruff 自定义规则

- 拒绝理由：ruff 不支持项目自定义规则插件；独立 ast 脚本约百行、
  无依赖、规则表可读（scripts/arch_check.py），够用。

## 后果

- 任何代码合入前必须过七道门；骨架期门全绿是仓库的最低状态。
- 新运行时依赖需要同时改 pyproject.toml 和 scripts/allowed-deps.json，
  否则 deps 门红——这是特性不是麻烦（DESIGN §13.1 依赖审计）。
- 棘轮基线文件在 `quality/` 下，调整必须随 commit 说明理由。
