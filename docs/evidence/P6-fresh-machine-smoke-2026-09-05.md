# P6 fresh-machine smoke evidence (2026-09-05)

本记录针对当前提交 `c5d23fa` 之后重新构建的发行产物，使用全新的
CPython 3.13 隔离虚拟环境执行，不复用仓库 `.venv` 或既有数据目录。

## 实测结果

1. `uv build --wheel --sdist` 成功生成 wheel 与 source distribution。
2. 在临时虚拟环境中使用 `uv pip install --no-deps` 安装 wheel 成功。
3. canonical 入口成功：`lhgp --version` 输出
   `lhgp 0.1.0a0 (protocol v1)`。
4. 兼容入口成功：`longtask --version` 输出
   `longtask 0.1.0a0 (protocol v1)`，并给出弃用提示。
5. 对全新数据目录运行 `lhgp --data-dir <empty-dir> doctor`，Python runtime、
   storage、database integrity、executor registry、kill-switch 五项均为 PASS，
   最终为 `ALL SYSTEMS GO`。
6. 启动隔离安装的 `lhgp-mcp`，发送 `tools/list` JSON-RPC 请求成功；响应包含
   `longtask_*` 遗留工具、`lhgp_*` 规范别名、验收请求、通知、attempt 审计、
   中断与写回控制工具，且 annotations 与输入 schema 正常返回。

## 边界

本次验证证明当前 wheel 的安装、canonical/legacy CLI、空目录初始化、doctor
和 MCP 发现链路可用；不替代三个 Alpha 非玩具 dogfood、外部 CLI 接力和严格
墙钟交付保证。这些仍按 `docs/LHGP-ROADMAP.md` 保持独立验收门槛。
