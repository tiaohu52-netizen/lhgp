---
title: Writing Tests for the Protocol
type: playbook
status: permanent
tags: [topic/quality, type/howto, audience/ai]
audience: [human, ai]
related: [[quality-gate], [add-event-type]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Writing Tests for the Protocol

测试的层级和 fixture 复用是 P6 收尾揪出 6 个 bug 的关键。本节是模板,
新增任何逻辑先看这里再写。

## 三个层级

| 层级 | 路径 | 速度 | 跑什么 |
|---|---|---|---|
| unit | `tests/unit/` | 几秒 | 纯函数 / dataclass 边界 / SQL 字符串 |
| integration | `tests/integration/` | 30-60 秒 | 真 SQLite (tmp_path) / 端到端 consumer 链路 |
| conformance | `tests/conformance/` | 慢 | 协议语义(spec vs impl 一致) |

**默认写 unit,真正跨表/跨模块才用 integration。**

## fixture 模式

```python
@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    yield conn
    conn.close()
```

`tmp_path` 隔离每个测试,无共享状态,无需 teardown。

## 写之前要问的三个问题

1. **这是 unit 还是 integration?** 真 DB 必走 integration(慢但准);纯函数走 unit
2. **失败时能告诉哪个具体边界错吗?** 一个 assert 一个具体事,不要 `assert all(x)`
3. **有没有相关 bug 类比?** 翻 git log 看最近的 fix,新测试要锁住同类问题

## 反模式(P6 一轮全遇过)

- **`tests/unit` 里写 `_open_xxx` 但忘了用 tmp DB**:状态污染,顺序依赖
- **`monkeypatch` 完不还原**:pytest 帮你还原,但要写对 `monkeypatch.setattr` 别用全局 set
- **import 顺序错**:`from foo import bar` 时如果模块先 import bar,触发 `import-outside-toplevel`
  lint 错。**测试文件第一行** `from __future__ import annotations` + import 顺序按字母
- **没在 `_setUp` 里建立 schema**:test 跑在空 DB 上,append_event 抛"no such table"
- **测试名模糊**:`test_x_works` 啥也没说。`test_extract_signals_excludes_reject_verdict`

## 集成测试标准模式

```python
def test_xxx_round_trip(tmp_path: Path) -> None:
    # 1. 建临时 DB
    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    try:
        # 2. setup 最小场景
        save_contract(conn, ..., now=NOW)
        record_evaluation(conn, ...)
        # 3. 触发
        result = do_xxx(conn, ...)
        # 4. 验证(具体边界)
        assert result.foo == expected
    finally:
        conn.close()
```

## 验证清单

- [ ] 测试文件 `from __future__ import annotations` 起头
- [ ] unit vs integration 选对
- [ ] tmp_path 隔离
- [ ] 失败时能看到具体边界
- [ ] 至少一个 happy + 一个 error / edge case
