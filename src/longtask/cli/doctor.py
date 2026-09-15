"""LHGP doctor 系统自检（DESIGN §15.2）。

自检项目：
1. Python 解释器大版本（>= 3.11）；
2. 存储目录（默认 ~/.lhgp，可用 --data-dir 覆盖）读写权限；
3. SQLite 权威状态库 state.db 完整性与版本；
4. 执行器注册表 registry.json/yaml 解析与可用执行器数量；
5. 全局 Emergency Kill Switch 状态。

注意第 3 项会**新建**一个空库（``connect`` + ``ensure_schema`` 的既有行为），
所以「healthy」必须区分「本来就有的库」与「本次刚建的库」——否则对一个
拼错或全新的目录，doctor 会一边建库一边报健康，用户据此认为数据没问题
（README 推荐 doctor 作为排障第一步，这条误导代价最高）。
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from longtask import PROTOCOL_VERSION, __version__
from longtask.adapters.registry import ExecutorRegistry
from longtask.cli.paths import default_data_root
from longtask.persistence.store import STORE_SCHEMA_VERSION, StoreConfig, connect, ensure_schema


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    details: str = ""


@dataclass(frozen=True, slots=True)
class DoctorReport:
    protocol_version: int
    package_version: str
    checks: tuple[CheckResult, ...] = ()

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def format_text(self) -> str:
        lines: list[str] = [
            f"=== LHGP doctor (v{self.package_version}, protocol v{self.protocol_version}) ===",
        ]
        for c in self.checks:
            mark = "[PASS]" if c.ok else "[FAIL]"
            detail_str = f" ({c.details})" if c.details else ""
            lines.append(f"{mark:6s} {c.name}: {c.message}{detail_str}")
        lines.append("-----------------------------------------------------")
        summary = "ALL SYSTEMS GO" if self.all_ok else "DIAGNOSTIC ISSUES DETECTED"
        lines.append(f"Result: {summary}")
        return "\n".join(lines)


def run_doctor(root: Path | None = None) -> DoctorReport:
    """运行全套自检并产出报告（DESIGN §15.2）。"""
    data_dir = root or default_data_root()
    checks: list[CheckResult] = []

    # 1. 检查 Python 版本
    py_ok = sys.version_info >= (3, 11)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(
        CheckResult(
            name="python_runtime",
            ok=py_ok,
            message=f"Python {py_ver}" if py_ok else f"Python {py_ver} < 3.11 required",
        )
    )

    # 2. 检查数据目录读写
    dir_ok = True
    dir_msg = f"directory {data_dir} accessible"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        test_file = data_dir / ".doctor_probe"
        test_file.write_text("probe", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except OSError as exc:
        dir_ok = False
        dir_msg = f"cannot write to {data_dir}: {exc}"
    checks.append(
        CheckResult(
            name="storage_directory",
            ok=dir_ok,
            message=dir_msg,
        )
    )

    # 3. 检查数据库连接与 schema 版本
    db_ok = True
    db_path = data_dir / "state.db"
    # 连接前先记下库是否已存在：connect/ensure_schema 会**创建**空库，
    # 不记就看不出「本来就有」和「本次刚建」的区别。
    db_existed = db_path.is_file()
    db_details = ""
    try:
        conn = connect(StoreConfig(db_path=db_path))
        try:
            ensure_schema(conn)
            row = conn.execute("PRAGMA user_version").fetchone()
            cur_ver = int(row[0]) if row else 0
            if cur_ver != STORE_SCHEMA_VERSION:
                db_ok = False
                db_msg = f"schema version mismatch: got {cur_ver}, expected {STORE_SCHEMA_VERSION}"
            else:
                # 审计持久化-R5：版本对了不等于页没坏——quick_check 才是
                # 损坏探测；小库开销可忽略，失败 fail-closed。
                quick = conn.execute("PRAGMA quick_check").fetchone()
                quick_result = str(quick[0]) if quick else "error"
                if quick_result != "ok":
                    db_ok = False
                    db_msg = f"quick_check failed: {quick_result}"
                else:
                    contracts = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()
                    n_contracts = int(contracts[0]) if contracts else 0
                    if db_existed:
                        db_msg = "state.db healthy"
                        db_details = f"{n_contracts} contract(s)"
                        if n_contracts == 0:
                            # 库在、但一份合同都没有——最常见的成因是
                            # ``--data-dir`` 指错了地方，或者某个只读命令
                            # 顺手建了个空库（``_open_read_conn`` 会创建）。
                            # 「healthy」在这种情形下会让人误以为数据没问题。
                            db_details += " — empty; is --data-dir pointing at the right place?"
                    else:
                        # 库是本次才建的：这不代表数据没问题，必须说清楚
                        db_msg = "state.db did not exist; created an empty one"
                        db_details = "no data — wrong --data-dir, or a fresh install"
        finally:
            conn.close()
    except Exception as exc:
        db_ok = False
        db_msg = f"database error: {exc}"
    checks.append(
        CheckResult(
            name="database_integrity",
            ok=db_ok,
            message=db_msg,
            details=db_details,
        )
    )

    # 4. 检查执行器注册表
    reg_path = data_dir / "registry.json"
    reg_ok = True
    reg_msg = "registry accessible"
    reg_details = ""
    try:
        reg = ExecutorRegistry.load_from_file(reg_path)
        all_executors = reg.list_entries(enabled_only=False)
        enabled_executors = reg.list_entries(enabled_only=True)
        reg_details = f"{len(enabled_executors)} enabled / {len(all_executors)} registered"
        # 与数据库同理：文件不存在时 load_from_file 返回空注册表，若只报
        # 「0 enabled」且 ok=True，用户看不出是「注册表缺失」还是「都关着」。
        # 两者后果一样（派不了工），所以必须写进 details。
        if not reg_path.is_file():
            reg_details += "; registry.json not found"
        elif not enabled_executors:
            reg_details += "; none enabled — no executor can be dispatched"
        missing: list[str] = []
        for entry in enabled_executors:
            argv = entry.launch.argv
            if not argv:
                missing.append(f"{entry.id}: launch argv is empty")
                continue
            executable = Path(argv[0])
            if not executable.is_file() and shutil.which(argv[0]) is None:
                missing.append(f"{entry.id}: executable not found ({argv[0]})")
        if missing:
            reg_ok = False
            reg_msg = "enabled executor launch checks failed"
            reg_details += "; " + "; ".join(missing)
    except Exception as exc:
        reg_ok = False
        reg_msg = f"cannot parse registry: {exc}"
    checks.append(
        CheckResult(
            name="executor_registry",
            ok=reg_ok,
            message=reg_msg,
            details=reg_details,
        )
    )

    # 5. 检查全局 Kill Switch 状态
    ks_path = data_dir / "KILL_SWITCH"
    ks_active = ks_path.is_file()
    ks_msg = "ACTIVE (emergency halt engaged)" if ks_active else "inactive (normal operation)"
    checks.append(
        CheckResult(
            name="kill_switch",
            ok=not ks_active,
            message=ks_msg,
        )
    )

    return DoctorReport(
        protocol_version=PROTOCOL_VERSION,
        package_version=__version__,
        checks=tuple(checks),
    )
