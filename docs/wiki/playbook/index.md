---
title: Playbook — Index
type: moc
status: permanent
tags: [moc, audience/ai, audience/human, type/howto]
audience: [human, ai]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Playbook

"怎么做事" 索引。改协议/加功能/修 bug 之前,先看对应 playbook;新增流程类
知识时,起一页新的 playbook 并在此登记。

## Persistence

- [[playbook/add-event-type]] — 怎么加新 EventType(枚举 + 事件触发点 + 测试)
- [[playbook/schema-migration]] — 怎么加表/索引,写 `_migrate_vN_to_vN+1`
- [[playbook/sql-binding]] — SQL 里别写死字符串、tuple/Row 双兼容

## MCP / CLI / 工具

- [[playbook/add-mcp-tool]] — 加 MCP 工具的契约(签名 + 读/写标记 + README 同步)
- [[playbook/add-cli-subcommand]] — 加 CLI 子命令的步骤(主入口 + dispatch + 测试)

## Quality

- [[playbook/write-test-first]] — 写测试的几个坑(集成 vs 单元、tmp DB 隔离、import 顺序)
- [[playbook/quality-gate]] — 7 门顺序与 fail-closed 语义

## Daemon

- [[playbook/daemon-tick-hook]] — 在 tick loop 里加新钩子的位置约定
