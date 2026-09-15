---
title: SQL Binding & No Hardcoded Event Names
type: playbook
status: permanent
tags: [topic/persistence, type/howto, audience/ai, type/pitfall]
audience: [human, ai]
requires: [python, sqlite3]
related: [[add-event-type], [schema-migration]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# SQL Binding & No Hardcoded Event Names

P6 抓出 **2 个真实 bug**,都来自同一个反模式:
1. `extract_template_signals` 写死 `'acceptance/status-changed'`,SQL 静默零命中 → vocab 空
2. `cursor.execute("SELECT ...")` 返回 **plain tuple**,不是 `sqlite3.Row`,
   `r["event_type"]` 抛 TypeError,被 `except Exception: continue` 吞掉

## 规则

### 1. SQL 字符串里**不写死**事件名 / 表名 / 列名

```python
# WRONG: 改名后零命中
"AND event_type = 'acceptance/status-changed'"

# RIGHT: 枚举值 + 参数绑定
"AND event_type = ?"
params.append(EventType.ACCEPTANCE_STATUS_CHANGED.value)
```

为什么要 `?` 不用 f-string:
- `f"AND event_type = '{EventType.X.value}'"` 在多文档 lint 工具(S608)会被报警
- 拼字符串如果 `EventType` 改名,**没有测试会失败**(SQL 还是合法字符串,只是零命中)

### 2. Cursor 返回的可能是 `Row` 也可能是 `tuple` —— 双兼容

```python
# WRONG: 假设是 Row
r["event_type"]  # 1 列 SELECT 时是 tuple,TypeError

# RIGHT: 双兼容
try:
    et = r["event_type"]
except (KeyError, TypeError):
    try:
        et = r[0]  # tuple fallback
    except (KeyError, TypeError, IndexError):
        continue
```

测试:写一个 `test_*_tuple_compat` case,用 `conn.execute("SELECT <col> FROM ...")` 单列 SELECT,
确认 cursor 返回 tuple 也能正确读取。

### 3. **不要** `except Exception: continue` 吞掉所有错

只 catch 真正可恢复的:
```python
# WRONG
except Exception:
    continue  # 把 TypeError / KeyError / 编程 bug 全吞

# RIGHT
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    continue  # I/O 错、shape 错、JSON 错 → 可恢复
# 编程 bug(AttributeError/KeyError/NameError)仍能 raise → 立刻被测到
```

## 反模式

- **silent except 静默吞**:`pass` / `continue` / `diff_eff = 0.5` 这类
  fallback 把生产 bug 藏成"中性值",要花几小时排查才发现
- **f-string 拼 SQL**:`f"WHERE id = '{contract_id}'"` — SQL 注入 + 改名静默失配双重风险
- **column name vs index 混用**:`r["x"] if "x" in r else r[0]` 是好习惯
  (sqlite3.Row `__contains__` 走列名,检查 tuple `__contains__` 走值)
- **JSON parse `except Exception: continue`** — 包括 TypeError 也没问题(空字符串),
  但 AttributeError 应 raise

## 验证清单

- [ ] SQL 参数化 (`?` 绑定),不拼字符串
- [ ] 事件名用 `EventType.X.value`,不写字面量
- [ ] Cursor 双兼容(tuple / Row 都能读)
- [ ] `except` 列出具体类型,不是 `Exception`
- [ ] 至少一个 tuple 路径的集成测试

## 相关

- `[[add-event-type]]` — 新事件的端到端添加流程
- `[[schema-migration]]` — 改 schema 时 fts 索引怎么跟着走
