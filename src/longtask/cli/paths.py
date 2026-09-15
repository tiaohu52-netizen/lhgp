"""数据目录解析与迁移（P6：~/.longtask → ~/.lhgp，SPEC §19.3）。

改名纪律：先双轨（新名可用、旧名继续生效），后切换（迁移工具 +
默认读取顺序）。绝不强制搬迁——旧安装的数据在原路径继续被读到，
只有用户显式跑 migrate 才真正搬家。
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from longtask.persistence.paths import default_data_root

LEGACY_DIR_NAME = ".longtask"
NEW_DIR_NAME = ".lhgp"

# 迁移归档目录名：~/.lhgp-migration-backups/
BACKUP_DIR_NAME = ".lhgp-migration-backups"


def _legacy_dir() -> Path:
    """旧数据目录（调用时求值：测试可注入 Path.home）。"""
    return Path.home() / LEGACY_DIR_NAME


def _new_dir() -> Path:
    """新数据目录（调用时求值）。"""
    return Path.home() / NEW_DIR_NAME


def _backup_dir() -> Path:
    return Path.home() / BACKUP_DIR_NAME


# 兼容常量导出：模块级冻结值仅作展示（真实解析走上面三个函数）
LEGACY_DIR = _legacy_dir()
NEW_DIR = _new_dir()
BACKUP_DIR = _backup_dir()


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """migrate 的计划与结果（dry-run 与真跑共用一个结构）。"""

    dry_run: bool
    source: Path
    target: Path
    backup_path: Path | None
    copied_files: int
    copied_bytes: int
    skipped: tuple[str, ...] = ()

    def format_text(self) -> str:
        lines = [
            f"source: {self.source}",
            f"target: {self.target}",
            f"mode: {'dry-run (nothing changed)' if self.dry_run else 'executed'}",
        ]
        if self.backup_path is not None:
            lines.append(f"backup: {self.backup_path}")
        lines.append(f"files: {self.copied_files} ({self.copied_bytes} bytes)")
        for note in self.skipped:
            lines.append(f"skipped: {note}")
        return "\n".join(lines)


def _iter_copyable(source: Path) -> list[Path]:
    """要拷贝的文件清单：全部数据（state.db / contracts / registry.json / runtime*）。"""
    if not source.exists():
        return []
    return [p for p in source.rglob("*") if p.is_file()]


def migrate_data_dir(
    *,
    dry_run: bool = True,
    source: Path | None = None,
    target: Path | None = None,
    make_backup: bool = True,
) -> MigrationPlan:
    """把旧数据目录迁到新路径（P6 迁移硬约束全兑现）。

    硬约束（LHGP-ROADMAP 七·补充）：
    1. dry-run 必跑：默认 True，只打印计划不动数据；
    2. 备份必做：make_backup=True 时对 source 完整归档到
       ~/.lhgp-migration-backups/<timestamp>/，报告路径；
    3. 可回滚：拷贝（不是移动）——旧路径原样保留，回滚 = 删新路径；
    4. 幂等：target 已存在且 source 缺失 → no-op（已迁移过）。

    拷贝用 cp 语义逐文件复制（含 state.db WAL 旁文件）；迁移后对
    target/state.db 跑一次 integrity check，损坏立即如实报告。
    """
    src = source if source is not None else _legacy_dir()
    dst = target if target is not None else _new_dir()
    skipped: list[str] = []

    if not src.exists():
        return MigrationPlan(
            dry_run=dry_run,
            source=src,
            target=dst,
            backup_path=None,
            copied_files=0,
            copied_bytes=0,
            skipped=("source does not exist: nothing to migrate",),
        )
    if dst.exists() and any(dst.iterdir()):
        return MigrationPlan(
            dry_run=dry_run,
            source=src,
            target=dst,
            backup_path=None,
            copied_files=0,
            copied_bytes=0,
            skipped=("target already populated: refusing to overwrite (rollback by deleting it)",),
        )

    files = _iter_copyable(src)
    total_bytes = sum(f.stat().st_size for f in files)

    backup_path: Path | None = None
    if make_backup and not dry_run and files:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = _backup_dir() / stamp
        if backup_path.exists():
            backup_path = _backup_dir() / f"{stamp}-{id(backup_path) & 0xFFFF:x}"
        shutil.copytree(src, backup_path)

    if not dry_run:
        # 审计持久化-R6：拷贝 WAL 库前先 checkpoint 到主文件，否则逐文件
        # 复制会产生「旧 db + 新 wal」的撕裂副本（daemon 运行中同样危险）。
        source_db = src / "state.db"
        if source_db.is_file():
            try:
                wal_conn = sqlite3.connect(source_db)
                try:
                    wal_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    wal_conn.close()
            except sqlite3.Error:
                pass  # checkpoint 失败不阻断（可能本就不是 WAL 库），quick_check 兜底
        for f in files:
            rel = f.relative_to(src)
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
        # 迁移后完整性校验：库损坏如实报（quick_check 对非 SQLite 文件
        # 会抛 DatabaseError——同样按损坏处理），不静默
        db = dst / "state.db"
        if db.is_file():
            try:
                conn = sqlite3.connect(db)
                try:
                    result = conn.execute("PRAGMA quick_check").fetchone()
                    if not result or result[0] != "ok":
                        skipped.append(f"integrity check FAILED: {result}")
                finally:
                    conn.close()
            except sqlite3.DatabaseError as exc:
                skipped.append(f"integrity check FAILED: {exc}")

    return MigrationPlan(
        dry_run=dry_run,
        source=src,
        target=dst,
        backup_path=backup_path,
        copied_files=len(files),
        copied_bytes=total_bytes,
        skipped=tuple(skipped),
    )


__all__ = [
    "BACKUP_DIR",
    "LEGACY_DIR",
    "NEW_DIR",
    "MigrationPlan",
    "default_data_root",
    "migrate_data_dir",
]
