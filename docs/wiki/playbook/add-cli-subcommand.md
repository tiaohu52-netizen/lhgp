---
title: Adding a CLI Subcommand
type: playbook
status: permanent
tags: [topic/cli, type/howto, audience/ai]
audience: [human, ai]
requires: [python, longtask.cli.main]
related: [[add-mcp-tool]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Adding a CLI Subcommand

新 `lhgp xxx` 子命令三处必填:**sub-parser + dispatch + 测试**。CLI 跟 MCP
是同一业务的两面,但 dispatch 模式不同(CLI 走 `if args.command == "xxx":` 链,
MCP 走 `TOOL_CATALOG` 字典)。

## 三处必填

### 1. 在 main() 里加 sub-parser

```python
# 找到合适的位置(按字母或逻辑顺序),加:
xxx_p = sub.add_parser("xxx", help="一句话说明")
xxx_p.add_argument("required_arg", type=str)
xxx_p.add_argument("--flag", action="store_true")
```

### 2. dispatch 链

```python
# 在 main() 末尾 if-else 链加:
if args.command == "xxx":
    # 读 args,调业务函数,print/返回
    result = do_xxx(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0
```

### 3. 测试

`tests/unit/test_cli.py` 加一个 `test_xxx_command`:
- 调 `main(["xxx", "--flag"])`,assert rc == 0
- 调 `main(["xxx", "--bad-arg"])`,assert rc != 0

## 反模式

- **sub-parser 用了但 dispatch 漏写**:运行时 `error: argument xxx: ...`,用户看不到提示
- **print 写中文提示而不是 exit code**:CI 看 exit code,提示容易被忽略
- **dispatch 抛 unhandled exception**:traceback 打一屏,user 不知道是配置错还是参数错
- **help 写得太长**:`add_parser(help=...)` 30 字以内;细节去 `--help` 后的 epilog

## 验证清单

- [ ] `sub.add_parser("xxx", ...)`
- [ ] dispatch 链 + `return 0`
- [ ] `test_xxx_command` happy + error path
- [ ] `lhgp xxx --help` 输出整洁
- [ ] CHANGELOG / docs/wiki 同步
