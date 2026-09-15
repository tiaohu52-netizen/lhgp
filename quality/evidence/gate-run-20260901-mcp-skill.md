# 质量门运行证据：§17 skill + MCP server 薄层（DESIGN §11.1、§17）

- 日期：2026-09-01
- 命令：`uv run python scripts/quality_gate.py`（本地，与 CI 同一命令）
- 环境：Windows 11（10.0.26200），uv 0.11.16 管理的 CPython 3.13
- 结果：**ALL PASS (7 gates)**

## 各门结果

| # | 门 | 结果 | 备注 |
|---|----|------|------|
| 1 | format | PASS | 67 文件全部已格式化 |
| 2 | lint | PASS | ruff check 零违规 |
| 3 | arch | PASS | 架构依赖方向违规 0 / 基线 0 |
| 4 | deps | PASS | 运行时依赖 0；MCP 薄层用标准库，未引入 mcp pypi |
| 5 | claims | PASS | 17 条声明（16 verified, 1 accepted_debt, 0 deferred） |
| 6 | typecheck | PASS | mypy --strict，35 个源文件零问题 |
| 7 | test+coverage | PASS | 250 passed，覆盖率 82.27%（MCP 薄层 250+ 行由 5 个 stdio 集成测试覆盖核心契约） |

## 本次提交序列（7405173 / 79c0f78 + 本次收尾）

1. **§17 longtask-contract skill**（`skills/longtask-contract/SKILL.md`）：
   DESIGN §17 决议"附带一个 skill，教会模型怎么用这套协议"——从"决定未定稿"
   进入可交付状态。10 节速通：协议定位、CLI 子集、起草合同的四个核心决定
   （objective 写验收不是方法 / checks 逐条可核对 / constraints 声明能力 /
   workload 如实填决定紧迫度）、交接文件、作为执行者被唤起时的协议、别做的事、
   故障速查。MANIFEST.json 索引让工具链（agentskills 等）可发现。
2. **MCP server 薄层**（`src/longtask/mcp_server.py`）：stdio JSON-RPC 2.0
   包装现有 HANDLERS。**设计选择**：暴露 7 个面向 AI 任务流的工具（不暴露
   24 个 RPC 方法——那是 RPC 隧道而非 AI 接口）。每个工具的 `inputSchema`
   包含 `required` 与字段说明，模型从 schema 自学入参；详细业务背景在
   `SKILL.md`。stdio 强制 `ensure_ascii=True` 输出避免 Windows pipe
   编码转换破坏非 ASCII 字节。

## 端到端真实复验

`examples/mcp-discovery/mcp-trace.example.log`：8 步走通完整合同生命周期：

```
1) initialize              → longtask-mcp/0.1.0a0
2) tools/list              → 7 个 longtask_* 工具
3) longtask_health         → protocol=1
4) longtask_list_executors → [exec-a, exec-b]
5) longtask_prepare_contract → cid=lt-20260901-mcpe2e state=drafted
6) longtask_approve_contract → ok=True
7) longtask_get_contract   → state=active
8) longtask_attach_to_executor → lease.holder=att-mcp-e2e
                              write_back.gen=1 events=[4, 5]
```

事件链：`contract/prepared → approved → started → context/snapshot →
attempt/succeeded`。各 MCP 客户端的 server 注册不在协议范围——每个
MCP 客户端需独立配置 longtask-mcp 路径（stdio 命令）。

## 门真实拦下的问题

- format：ensure_ascii 改回后 ruff 自动重排一处 import；
- mypy：mcp_server 初版有返回 Any 与 RpcError.code 类型推断两个问题（自动修）；
- claims：`integration_real_agent-cli` 不在 schema enum 中——改 `manual_review`（第三方
  归档是人工审阅态，与 schema 五种 enum 的语义一致）；
- 集成测试发现：mcp_server 跨平台中文 `ensure_ascii=False` 在 Windows stdio
  pipe 上被透明转 cp936 破坏字节——改为 `True` 解决，模型侧按 UTF-8
  解析 JSON 字符串 `\u` 转义即可。

## 治理增量

- claims 新增一条 verified（`mcp-server-and-skill`）：16 verified + 1 accepted_debt。
- README 发布分级同步：Developer Preview 已涵盖 §17 skill + MCP 薄层。
- examples 新增 `mcp-discovery/`：MCP 端到端复验 trace 归档。
- `runtime-mcp/` 加入 .gitignore：第三方运行数据不入库（与 runtime/ 同样治理）。