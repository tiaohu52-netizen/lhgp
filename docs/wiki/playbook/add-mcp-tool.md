---
title: Adding an MCP Tool
type: playbook
status: permanent
tags: [topic/mcp, topic/cli, type/howto, audience/ai]
audience: [human, ai]
requires: [python, longtask.mcp_server]
related: [[add-cli-subcommand], [add-event-type]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Adding an MCP Tool

新 MCP 工具必须三件事一起做:**签名 + 注册 + 测试**。少任何一件,客户端拿到
工具列表后调用会拿到无意义错误,或者更糟,工具被静默丢弃。

## 签名(强制)

```python
def tool_xxx(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """一句话描述做什么。"""
    ...
```

**约束**:
- 参数 `args` 是 `dict[str, Any]`,必须 `.get()` 拿值,不要 `args.foo`
- 第二个参数 `ctx` 包含 `conn` / `root` / `data_dir` —— 从 ctx 拿,不要 import
- 返回 `dict[str, Any]`,JSON 可序列化(不要返回 datetime/Path,转 ISO/str)
- 抛 `ValueError` 让 MCP server 转 -32602,业务错才抛

## 注册三处必填

```python
# 1. 工具面:TOOL_CATALOG 或 _TOOL_DISPATCHERS
TOOL_CATALOG["tool_xxx"] = (tool_xxx, "what it does", read_only=True|False)

# 2. 注解:READ_ONLY_TOOLS 或 DESTRUCTIVE_TOOLS 二选一
DESTRUCTIVE_TOOLS.add("tool_xxx")
# 或
READ_ONLY_TOOLS.add("tool_xxx")

# 3. CHANGELOG / 文档
```

## 测试

- 集成测试:`tests/integration/test_mcp_server.py::TestMCPDiscovery` 验工具在 list 里
- 单测:每个 tool 至少一个 happy path + 一个 error path

## 反模式

- **`args.foo`** 假定:工具被调用时不一定所有键都有,`.get()` + 类型校验
- **返回 `Path` / `datetime`**:MCP 是 JSON-RPC,datetime 走 ISO string
- **忘加 `DESTRUCTIVE_TOOLS`**:客户端可能不加确认就调,用户视角"按钮"消失但**实际还生效**

## 验证清单

- [ ] 签名 `(args: dict, ctx: dict) -> dict`
- [ ] `TOOL_CATALOG` 注册
- [ ] `READ_ONLY_TOOLS` / `DESTRUCTIVE_TOOLS` 二选一
- [ ] 集成测试 + 单测
- [ ] CHANGELOG / docs/wiki 同步
