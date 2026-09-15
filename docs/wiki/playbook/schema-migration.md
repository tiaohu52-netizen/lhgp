---
title: Schema Migration
type: playbook
status: permanent
tags: [topic/persistence, type/howto, audience/ai]
audience: [human, ai]
requires: [python, sqlite3, lhgp.persistence]
related: [[add-event-type], [sql-binding]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Schema Migration

新增表 / 索引 / 列必须走 `STORE_SCHEMA_VERSION` 流水线。`v2 → v3` 是 P6 的
参考实现,可直接照搬。

## 步骤

### 1. Bump `STORE_SCHEMA_VERSION` + 加 `_migrate_vN_to_vN+1`

`src/longtask/persistence/schema.py`:
```python
STORE_SCHEMA_VERSION = 4  # was 3
```

在 `ensure_schema` 末尾的版本链加迁移函数:
```python
def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body_md TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_contract_id TEXT,
            source_event_id INTEGER,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            score REAL NOT NULL DEFAULT 0.0,
            schema_version INTEGER NOT NULL DEFAULT 4
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, kind)")
```

**关键点**:
- 用 `IF NOT EXISTS` —— 迁移可重入,重复跑不出错
- **不回填历史数据**(P6 v2→v3 决策):保护事件流 + revision CAS 干净
- 列名 + 索引名加前缀(`idx_memories_*`)防命名冲突

### 2. 加表 + 索引到 `ensure_schema` 的 v4 终态建表段

```python
def ensure_schema(conn):
    # ... 既有 v1/v2/v3 建表 ...
    # v4 终态
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (...)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ...""")
    # 版本升级
    _check_schema_version(conn, STORE_SCHEMA_VERSION)  # 用新值
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < 4:
        _migrate_v3_to_v4(conn)
        conn.execute(f"PRAGMA user_version = 4")
```

### 3. 写 `StoreConfig.schema_version` 默认值

`src/lhgp/persistence/types.py`:
```python
@dataclass(slots=True)
class StoreConfig:
    db_path: Path
    schema_version: int = 4  # 跟上 STORE_SCHEMA_VERSION
```

**测试**:`tests/integration/test_store_transactions.py` 会调 `StoreConfig()`
无参,默认要等于新版本,否则 `_check_schema_version` 报"schema newer than supported"。

### 4. 集成测试

- 旧 v3 state.db 文件 → 跑 `ensure_schema` → 自动升到 v4,新表存在
- 全新 state.db → 跑 `ensure_schema` → 直接 v4
- 旧 v4 state.db → 跑 `ensure_schema` → 不动(幂等)

## 反模式

- **手动 `PRAGMA user_version = N`**:必须在迁移函数里 bump,不要散落各处
- **破坏性迁移**:`DROP TABLE` / 列类型转换 —— 没有备份路径
- **回填历史数据**:P6 v2→v3 决策不补,理由是 revision CAS 和事件流要干净
- **大版本直接 `migrate` 一波**:分小版本逐步走,每个 v 加一份迁移函数

## 验证清单

- [ ] `STORE_SCHEMA_VERSION` bump
- [ ] `_migrate_vN_to_vN+1` 写完,`IF NOT EXISTS` 保护
- [ ] 新表 + 索引在 `ensure_schema` 终态段
- [ ] `StoreConfig.schema_version` 默认跟上
- [ ] 旧 DB 升级 + 全新 DB + 已升级 DB 三个 case 都跑过

## 相关

- `[[add-event-type]]` — 如果新表是事件流,需要新 EventType
- `[[sql-binding]]` — 迁移函数里也是 SQL,参数化
