---
title: Adding a New Event Type
type: playbook
status: permanent
tags: [topic/persistence, type/howto, audience/ai]
audience: [human, ai]
requires: [python, lhgp.persistence.events]
related: [[add-mcp-tool], [schema-migration], [sql-binding]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Adding a New Event Type

新增事件必须四件事一起做:**枚举 + 触发点 + 测试 + 文档**。少任何一件,
下游 consumer(extract_template_signals / _enforce_deadlines / portfolio)会
**静默失配**(cursor 返回 tuple 时 `r["event_type"]` TypeError 被 except 吞,
或 SQL `WHERE event_type = 'literal'` 写死字符串改名后零命中)。

## 步骤

### 1. 在 `lhgp.persistence.events.EventType` 加枚举

```python
class EventType(StrEnum):
    # ... 既有 ...
    YOUR_NEW_EVENT = "domain/verb"  # 必须 lhgp 协议事件词汇,见 SPEC
```

- 命名格式 `domain/verb`,与 `deadline/level-escalated` 同款
- 用 `StrEnum`,JSON 序列化为字符串不丢类型

### 2. 触发点:`append_event` 用枚举值,**不写字符串**

```python
append_event(
    conn,
    contract_id=cid,
    event_type=EventType.YOUR_NEW_EVENT,  # ← 必须用枚举
    payload={"key": "value"},
    now=now,
    actor="daemon",
)
```

**反例**(会被 sql-binding playbook 拦下):
```python
# WRONG: 写死字符串,改名后静默失配
append_event(conn, event_type="your/new-event", ...)
# WRONG: f-string 拼 SQL
"WHERE event_type = 'your/new-event'"
```

### 3. 测试:事件能落 + 能被 consumer 读到

```python
def test_your_event_emitted_and_consumed(tmp_path):
    conn = _setup_db(tmp_path)
    try:
        # 触发事件
        append_event(conn, event_type=EventType.YOUR_NEW_EVENT, ...)
        # consumer 端验证
        events = [e for e in get_events(conn, contract_id=cid)
                  if e.event_type == EventType.YOUR_NEW_EVENT]
        assert len(events) == 1
    finally:
        conn.close()
```

**集成测试** 必跑(端到端 consumer 链路),**单测** 在 consumer 模块再加 tuple/Row 兼容
(参考 `test_enforcement_levels.py` 和 `test_learning_extractor.py`)。

### 4. 文档同步

- `CHANGELOG.md` 的 Added 段登记新事件
- `docs/wiki/glossary.md` 加新术语
- `ARCHITECTURE.md` 表格如有"事件"行,补上

## 反模式

- **写死字符串进 SQL**:`AND event_type = 'literal'` —— 改名后 consumer 静默失配
- **silent except Exception 吞掉**:Tuple/Row 不兼容时 `r["event_type"]` TypeError
  被吞,cursor 返回 tuple 时只返 0 条,生产无报警
- **新事件不写单测**:P6 一轮抓出 3 个这类 regression(全靠集成测试揪)

## 验证清单

- [ ] `EventType.YOUR_NEW_EVENT` 加进枚举,字符串符合 `domain/verb`
- [ ] 触发点用枚举,不用字面量
- [ ] SQL 用 `?` 参数 + `EventType.X.value`,不用 f-string
- [ ] 集成测试验证事件被 emit + 被 consumer 读到
- [ ] CHANGELOG / glossary / ARCHITECTURE 同步

## 相关

- `[[sql-binding]]` — tuple/Row 兼容 + 不要写死字符串
- `[[schema-migration]]` — 事件带新字段时,是否要 `_migrate`
- `[[daemon-tick-hook]]` — 如果是 daemon 触发的事件,放在哪个 tick 钩子
