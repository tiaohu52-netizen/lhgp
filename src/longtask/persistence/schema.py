"""SQLite schema 与连接/事务层（DESIGN §3.1、§7、§13.3）。

从 store.py 拆出。本模块管：
- connect() 打开 SQLite + WAL + foreign_keys
- _check_schema_version() 未来版本只读拒写
- ensure_schema() 建表 / 索引 / v1→v2 迁移
- _migrate_v1_to_v2() 原地升级列
- transaction() BEGIN IMMEDIATE 上下文
- partition / event_type 小 helper

业务表读写（contracts / leases / events / attempts / decisions /
contract_revisions）按主题拆到 contracts.py / leases.py / events.py / revisions.py
等模块，本模块不参与。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from longtask.persistence.errors import StoreError, StoreTamperedError
from longtask.persistence.types import StoreConfig

# P1=v2: goal/deadline/acceptance columns added
# P6=v3: user_evaluations + acceptance_diffs tables added
# memory-and-wiki Phase 2=v4: memories table added
STORE_SCHEMA_VERSION = 5


def connect(config: StoreConfig) -> sqlite3.Connection:
    """打开权威库并执行启动检查（DESIGN §13.3）。

    - WAL 模式：单用户本机下的崩溃恢复与跨进程并发。
    - schema 版本检查：高于本实现版本 → 只读并拒绝写入，不猜测迁移。
    - fail-closed：任何启动检查失败抛 StoreError，调用方不得忽略。
    """
    if not isinstance(config.db_path, Path):
        raise StoreError(f"db_path must be a Path, got {type(config.db_path).__name__}")
    conn = sqlite3.connect(config.db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _check_schema_version(conn, config.schema_version)
    return conn


def _check_schema_version(conn: sqlite3.Connection, expected: int) -> None:
    """未知未来版本只读拒写（DESIGN §13.3）。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    current: int = int(row[0]) if row else 0
    if current > expected:
        conn.close()
        raise StoreTamperedError(
            f"state.db schema version {current} is newer than supported {expected}; "
            "refusing to write (read-only mode required)"
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """初始化数据库 schema（DESIGN §3.1、§7、§13.3）。

    P1 v2：contracts / leases / events 三张 v1 既有表保留并加列；新增
    contract_revisions / attempts / decisions / idempotency 四张表。

    既有 state.db（v1）会通过 _migrate_v1_to_v2 平滑升级，不丢历史事件。
    """
    # ── v2 终态：建表 + 索引（IF NOT EXISTS 保证幂等）──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            goal_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL DEFAULT 1,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '{}',
            progress_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 2
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contracts (
            contract_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,  -- 稳定 Goal 身份（可跨多个阶段合同）
            revision INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL,  -- commitment lifecycle 轴
            deadline_status TEXT NOT NULL DEFAULT 'not_due',  -- P1：deadline 轴
            acceptance_status TEXT NOT NULL DEFAULT 'pending',  -- P1：acceptance 轴
            blocked_reason TEXT,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            hard_constraints_json TEXT NOT NULL,
            acceptance_json TEXT NOT NULL,
            workload_initial_hours REAL NOT NULL,
            budget_json TEXT NOT NULL,
            soft_guidance_json TEXT NOT NULL DEFAULT '{}',
            context_json TEXT NOT NULL DEFAULT '{}',
            execution_json TEXT NOT NULL DEFAULT '{}',
            client_meta_json TEXT NOT NULL DEFAULT '{}',
            authority_json TEXT NOT NULL DEFAULT '{}',  -- P2
            attention_json TEXT NOT NULL DEFAULT '{}',  -- P2
            continuity_json TEXT NOT NULL DEFAULT '{}',  -- P2
            auto_approve_json TEXT NOT NULL DEFAULT '{}',  -- 3rd-round review
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            next_wakeup_at TEXT,
            next_decision_at TEXT,  -- P4
            schema_version INTEGER NOT NULL DEFAULT 2
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leases (
            contract_id TEXT NOT NULL,
            partition_id TEXT NOT NULL DEFAULT '',
            holder_attempt_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            heartbeat_at TEXT NOT NULL,
            timeout_seconds REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (contract_id, partition_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id TEXT,
            goal_id TEXT,  -- 稳定 Goal 身份（可与 contract_id 不同）
            attempt_id TEXT,
            lease_generation INTEGER,
            contract_revision INTEGER,  -- P1：事件所属合同修订号
            role TEXT,  -- P1：executor / verifier / daemon / user / promoter / scheduler / system
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_schema_version INTEGER NOT NULL DEFAULT 2,  -- P1：payload schema 版本
            request_id TEXT,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        )
        """
    )
    # 注：v1→v2 迁移会 ALTER TABLE events 补 goal_id / request_id / role /
    # contract_revision 等列；引用这些列的索引（request_id、goal_id）必须
    # 在 _migrate_v1_to_v2 之后建（见下），否则真实 v1 库在 ensure_schema
    # 阶段直接 OperationalError（安全审查 持久化-C1）。contract_id 自建表
    # 起就存在，相关索引可以立即建。
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_contract_id
        ON events(contract_id, event_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_contract_type
        ON events(contract_id, event_type, event_id)
        """
    )

    # ── P1：合同修订不可变表（替换 v1 就地 CAS UPDATE）──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contract_revisions (
            contract_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            state TEXT NOT NULL,
            deadline_status TEXT NOT NULL,
            acceptance_status TEXT NOT NULL,
            blocked_reason TEXT,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            hard_constraints_json TEXT NOT NULL,
            acceptance_json TEXT NOT NULL,
            workload_initial_hours REAL NOT NULL,
            budget_json TEXT NOT NULL,
            soft_guidance_json TEXT NOT NULL DEFAULT '{}',
            context_json TEXT NOT NULL DEFAULT '{}',
            execution_json TEXT NOT NULL DEFAULT '{}',
            client_meta_json TEXT NOT NULL DEFAULT '{}',
            authority_json TEXT NOT NULL DEFAULT '{}',
            attention_json TEXT NOT NULL DEFAULT '{}',
            continuity_json TEXT NOT NULL DEFAULT '{}',
            auto_approve_json TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL,
            recorded_by TEXT NOT NULL,
            change_reason TEXT,
            PRIMARY KEY (contract_id, revision)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_contract_revisions_recorded
        ON contract_revisions(contract_id, revision DESC)
        """
    )

    # ── P1：attempts 实体表（§7 attempt 轴；C1 修复用：实际判断 verifier 是否派生）──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            contract_id TEXT,  -- 当前合同身份；旧库为空时兼容回退 goal_id
            contract_revision INTEGER NOT NULL,
            role TEXT NOT NULL,  -- executor | verifier
            executor_id TEXT,
            model_id TEXT,
            state TEXT NOT NULL,  -- attempt state axis (§7)
            lease_generation INTEGER,
            partition_id TEXT,
            admitted_at TEXT NOT NULL,
            started_at TEXT,
            terminal_at TEXT,
            return_code INTEGER,
            error_class TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            -- P3：持久外部句柄（SPEC §11.3）——外部 run 身份与恢复策略
            external_run_id TEXT,
            session_locator TEXT,
            recovery_strategy TEXT,  -- reattach | poll | nonrecoverable
            process_identity_json TEXT,  -- 提示，不得单独作为身份真相
            capability_snapshot_json TEXT,
            handle_registered_at TEXT,
            orphaned_at TEXT,  -- 进入 orphan grace 的起点（§11.3 分支 3）
            session_token_hash TEXT,  -- per-attempt 会话凭据哈希
            usage_json TEXT  -- 消耗台账：执行者写回自报的 token/成本（§11.3）
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attempts_goal
        ON attempts(goal_id, admitted_at)
        """
    )
    # ── P1：decisions 实体表（§6 escalation 轴历史）──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id TEXT NOT NULL,
            contract_id TEXT,  -- 当前决策所属合同（旧库兼容为空）
            contract_revision INTEGER NOT NULL,
            tier TEXT,  -- urgency tier；None 表示 deadline-arbiter
            decision_type TEXT NOT NULL,  -- see DESIGN §6
            reason TEXT NOT NULL,
            budget_dispatches_left INTEGER,
            budget_escalations_left INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL,
            actor TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decisions_goal_time
        ON decisions(goal_id, recorded_at)
        """
    )

    # ── P1：idempotency 实体表（§11.3 重放去重）──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency (
            request_id TEXT PRIMARY KEY,
            goal_id TEXT,
            response_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )

    # P4：通知 outbox（至少一次投递 + 幂等键；渠道本身不在存储层实现）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            goal_id TEXT,
            event_type TEXT NOT NULL,
            channel TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            lease_until TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_outbox_due "
        "ON notification_outbox(status, available_at, notification_id)"
    )

    # ── 迁移：v1 → v2 ──
    _migrate_v1_to_v2(conn)

    # ── 迁移：v2 → v3（P6 feedback/diff 反馈回路）──
    # 新表用 IF NOT EXISTS 幂等创建；旧库不需数据回填。
    _migrate_v2_to_v3(conn)

    # ── 迁移：v3 → v4（memory-and-wiki Phase 2 memories 表）──
    _migrate_v3_to_v4(conn)

    # ── 迁移：v4 → v5（attempt usage 台账列）──
    _migrate_v4_to_v5(conn)

    # events(goal_id / request_id) 列由上面的迁移物化（v1 库 ALTER TABLE
    # 后才存在），因此这两个 partial index 只能在迁移之后建——否则真实
    # v1 库在 ensure_schema 阶段直接 OperationalError（安全审查 持久化-C1）。
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_goal_id
        ON events(goal_id, event_id)
        WHERE goal_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_request_id
        ON events(request_id)
        WHERE request_id IS NOT NULL
        """
    )

    # contract_id is additive for old attempts tables, so build its index only
    # after the migration has materialized the column.
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attempts_contract
        ON attempts(contract_id, admitted_at)
        """
    )
    # attempts(role) / attempts(state) 列在 v1 库上由上面的迁移补齐，
    # 相关索引必须在迁移之后建（同 idx_events_goal_id 理由）。
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attempts_role_state
        ON attempts(role, state)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attempts_orphaned
        ON attempts(state)
        WHERE state = 'orphaned'
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decisions_contract_time
        ON decisions(contract_id, recorded_at)
        """
    )

    _add_goal_columns(conn)

    # Stable Goal identity migration: old contracts used contract_id as a
    # compatibility goal id; materialize those identities before new writes.
    conn.execute(
        """
        INSERT INTO goals (goal_id, title, objective, created_at, updated_at, schema_version)
        SELECT goal_id, MIN(title), MIN(objective), MIN(created_at), MAX(updated_at), ?
        FROM contracts
        WHERE goal_id IS NOT NULL AND goal_id <> ''
        GROUP BY goal_id
        ON CONFLICT(goal_id) DO NOTHING
        """,
        (STORE_SCHEMA_VERSION,),
    )

    conn.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3 原地迁移：新增 user_evaluations + acceptance_diffs 表（DESIGN §13.3）。"""
    # 新表都是 IF NOT EXISTS 幂等创建；不需要数据回填。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_evaluations (
            evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id TEXT NOT NULL,
            contract_revision INTEGER NOT NULL,
            attempt_id TEXT,
            evaluator TEXT NOT NULL,
            rating INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            comments TEXT,
            created_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 3
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_evaluations_contract
        ON user_evaluations(contract_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_evaluations_verdict
        ON user_evaluations(verdict, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS acceptance_diffs (
            diff_id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id TEXT NOT NULL,
            contract_revision INTEGER NOT NULL,
            attempt_id TEXT,
            snapshot_before_json TEXT NOT NULL DEFAULT '{}',
            snapshot_after_json TEXT NOT NULL DEFAULT '{}',
            files_changed_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT,
            computed_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 3
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_acceptance_diffs_contract
        ON acceptance_diffs(contract_id, computed_at)
        """
    )


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4 原地迁移：新增 memories 表（memory-and-wiki Phase 2）。

    memories 是协议级长期记忆,跨合同 / 跨项目;与 user_evaluations(per-contract
    human rating)、acceptance_diffs(per-attempt 产物 diff)、templates/(per-pattern
    auto-evolved 模板)正交。本表是 system-mined / human-curated 的混合,容量合同
    走 context.py.max_bytes(fail-closed)。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body_md TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_contract_id TEXT,
            source_event_id INTEGER,
            source_actor TEXT,
            score REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            schema_version INTEGER NOT NULL DEFAULT 4
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_scope_kind "
        "ON memories(scope, kind, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_expires "
        "ON memories(expires_at) WHERE expires_at IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_source "
        "ON memories(source_contract_id) WHERE source_contract_id IS NOT NULL"
    )


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5 原地迁移：attempts 增加 usage_json 列（attempt 消耗台账）。

    预算控制的是「派几次」，此前没有任何账记「烧了多少」——执行者写回时
    自报的 token/成本无处落，stats 只能报墙钟与退出码。usage 由写回链路
    写入（harness 最清楚自己的消耗），格式校验 fail-closed（见
    persistence/usage.py），本迁移只负责列存在且幂等。
    """

    def _add_column_if_missing(table: str, col_def: str) -> None:
        with suppress(sqlite3.OperationalError):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")

    _add_column_if_missing("attempts", "usage_json TEXT")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2 原地迁移（DESIGN §13.3 演化纪律）。

    - 给既有 contracts 表加 goal_id / deadline_status / acceptance_status / next_decision_at /
      authority_json / attention_json / continuity_json 列（缺省值兜底）。
    - 给既有 events 表加 goal_id / contract_revision / role / payload_schema_version 列。
    - 给既有 contracts 行回填 goal_id = contract_id（§7 命名迁移）。
    - 给既有 contracts 行回填 next_wakeup_at（v1 已有，直接保留）。
    - 不重建既有事件数据。

    一次性数据改写（枚举改名 complete→satisfied、on_track→not_due）只在
    user_version < 2 时执行：这些不是幂等结构调整，而是历史语义迁移；
    无条件执行会在每次 ensure_schema（即每个 CLI 命令/每个 RPC 连接）时
    静默改写 daemon 刚写入的合法终态，绕过事件流与 revision CAS
    （安全审查 持久化-C2）。
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    is_fresh_v1 = version < 2

    # ``user_version`` tracks the logical schema, but early Developer Preview
    # builds shipped P3 columns after setting it to 2.  Always reconcile the
    # additive columns so an interrupted or partially upgraded database can be
    # opened safely; every operation below is idempotent.

    # contracts：缺啥补啥（IF NOT EXISTS 风格靠 suppress(OperationalError)）
    def _add_column_if_missing(table: str, col_def: str) -> None:
        with suppress(sqlite3.OperationalError):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")

    _add_column_if_missing("contracts", "goal_id TEXT")
    _add_column_if_missing("contracts", "deadline_status TEXT NOT NULL DEFAULT 'not_due'")
    _add_column_if_missing("contracts", "acceptance_status TEXT NOT NULL DEFAULT 'pending'")
    _add_column_if_missing("contracts", "authority_json TEXT NOT NULL DEFAULT '{}'")
    _add_column_if_missing("contracts", "attention_json TEXT NOT NULL DEFAULT '{}'")
    _add_column_if_missing("contracts", "continuity_json TEXT NOT NULL DEFAULT '{}'")
    _add_column_if_missing("contracts", "auto_approve_json TEXT NOT NULL DEFAULT '{}'")
    _add_column_if_missing("contract_revisions", "auto_approve_json TEXT NOT NULL DEFAULT '{}'")
    _add_column_if_missing("contracts", "next_decision_at TEXT")

    _add_column_if_missing("events", "goal_id TEXT")
    _add_column_if_missing("events", "request_id TEXT")
    _add_column_if_missing("events", "lease_generation INTEGER")
    _add_column_if_missing("events", "contract_revision INTEGER")
    _add_column_if_missing("events", "role TEXT")
    _add_column_if_missing("events", "actor TEXT")
    _add_column_if_missing("events", "payload_schema_version INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing("events", "schema_version INTEGER NOT NULL DEFAULT 1")

    # P3：attempts 表外部句柄列（SPEC §11.3）——v2 早期 attempts 表无这些列
    _add_column_if_missing("attempts", "external_run_id TEXT")
    _add_column_if_missing("attempts", "contract_id TEXT")
    _add_column_if_missing("decisions", "contract_id TEXT")
    _add_column_if_missing("attempts", "model_id TEXT")
    _add_column_if_missing("attempts", "session_locator TEXT")
    _add_column_if_missing("attempts", "recovery_strategy TEXT")
    _add_column_if_missing("attempts", "process_identity_json TEXT")
    _add_column_if_missing("attempts", "capability_snapshot_json TEXT")
    _add_column_if_missing("attempts", "handle_registered_at TEXT")
    _add_column_if_missing("attempts", "orphaned_at TEXT")
    _add_column_if_missing("attempts", "session_token_hash TEXT")

    # 回填 goal_id（幂等：WHERE 条件本身防重复改写）
    conn.execute("UPDATE contracts SET goal_id = contract_id WHERE goal_id IS NULL OR goal_id = ''")
    # 旧库只有 goal_id；对历史默认的一合同一 Goal 身份做无歧义回填。
    # 若一个 Goal 曾绑定多个合同则不猜，保留 NULL，由 reconcile 的兼容
    # 回退路径按旧语义处理，避免把 attempt 错绑到错误阶段。
    conn.execute(
        """
        UPDATE attempts
        SET contract_id = goal_id
        WHERE (contract_id IS NULL OR contract_id = '')
          AND EXISTS (
              SELECT 1 FROM contracts c
              WHERE c.contract_id = attempts.goal_id
          )
        """
    )
    conn.execute("UPDATE events SET goal_id = contract_id WHERE goal_id IS NULL")
    conn.execute(
        "UPDATE events SET payload_schema_version = schema_version "
        "WHERE payload_schema_version IS NULL"
    )

    if is_fresh_v1:
        # 一次性历史语义迁移（只在真正的 v1 库上执行一次）。
        # 合同状态名迁移：complete → satisfied（acceptance_status 轴的终态），
        # 但只在 v1 行明确为 complete 时改；其它状态保留。
        conn.execute("UPDATE contracts SET state = 'satisfied' WHERE state = 'complete'")
        # 早期预发布版本曾写入不存在的 on_track 枚举；迁移时统一到协议
        # 当前语义的 not_due，避免旧库在读取 ContractView 时崩溃。
        conn.execute(
            "UPDATE contracts SET deadline_status = 'not_due' WHERE deadline_status = 'on_track'"
        )
    # expired 状态保留作 commitment lifecycle 中的非终态；deadline_status 由
    # 应用层据 deadline_at 派生为 past_deadline。


def _add_goal_columns(conn: sqlite3.Connection) -> None:
    """Reconcile Goal columns for pre-first-class Goal databases."""
    for definition in (
        "revision INTEGER NOT NULL DEFAULT 1",
        "plan_json TEXT NOT NULL DEFAULT '{}'",
        "progress_json TEXT NOT NULL DEFAULT '{}'",
    ):
        with suppress(sqlite3.OperationalError):
            conn.execute(f"ALTER TABLE goals ADD COLUMN {definition}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """进入 BEGIN IMMEDIATE 事务，异常回滚，退出提交（DESIGN §3.1、§13.3）。

    保证单事务原子性，防止并发写写冲突（SQLite WAL 下 BEGIN IMMEDIATE 立即获取写锁）。
    支持嵌套：如果连接已在事务中，内部上下文复用外层事务，不提前提交或回滚。
    """
    in_trans = conn.in_transaction
    if not in_trans:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if not in_trans:
            conn.commit()
    except Exception:
        if not in_trans:
            conn.rollback()
        raise


def partition_key(partition_id: str | None) -> str:
    """partition_id → DB 内存储键（DESIGN §7.1：'' 表示全局分区）。"""
    return partition_id or ""


def parse_partition_key(stored: str) -> str | None:
    """DB 内存储键 → 逻辑 partition_id（DESIGN §7.1）。"""
    return stored or None


def format_event_type(event_type: object) -> str:
    """EventType | str → 字符串值（DESIGN §11.3 事件词汇稳定）。"""
    return event_type.value if hasattr(event_type, "value") else str(event_type)


__all__ = [
    "STORE_SCHEMA_VERSION",
    "connect",
    "ensure_schema",
    "format_event_type",
    "parse_partition_key",
    "partition_key",
    "transaction",
]
