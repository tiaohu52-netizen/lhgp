"""`lhgp doctor` 自检测试（DESIGN §15.2）。

doctor 是 README 推荐的**排障第一步**，所以它自己不能说谎。本文件钉住两类谎：
1. 对一个不存在/拼错的目录，doctor 会一边**新建空库**一边报「healthy」；
2. 注册表文件缺失时返回空注册表，报「0 enabled」且 ok=True，看不出是
   「文件没了」还是「都关着」——两者后果都是派不了工。

之前 doctor 只有 `test_cli.py` 里的间接覆盖，没有专门测试。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from longtask.cli.doctor import run_doctor
from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.store import StoreConfig, connect, ensure_schema, save_contract

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)


def _draft() -> ContractDraft:
    return ContractDraft(
        title="doctor probe",
        objective="check storage health",
        deadline_at=NOW.replace(year=2030),
        hard_constraints={"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="s", checks=("c",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=3,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1048576,
        ),
    )


def _check(report: object, name: str) -> object:
    for c in report.checks:  # type: ignore[attr-defined]
        if c.name == name:
            return c
    raise AssertionError(f"check {name!r} not present in report")


class TestDatabaseCheckDoesNotManufactureHealth:
    def test_missing_db_is_reported_as_created_empty(self, tmp_path: Path) -> None:
        """回归：库不存在时旧实现报「state.db healthy」，等于为拼错的路径背书。"""
        report = run_doctor(tmp_path)
        db = _check(report, "database_integrity")
        assert db.ok is True  # 空库可用，不是错误
        assert "did not exist" in db.message
        assert "healthy" not in db.message
        assert "no data" in db.details

    def test_missing_db_note_survives_formatting(self, tmp_path: Path) -> None:
        """说明必须出现在用户实际看到的文本里，而不是只留在字段里。"""
        text = run_doctor(tmp_path).format_text()
        assert "did not exist" in text

    def test_existing_db_reports_healthy_with_count(self, tmp_path: Path) -> None:
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            ensure_schema(conn)
            save_contract(conn, _draft(), contract_id="lt-doctor-1", now=NOW)
        finally:
            conn.close()
        db = _check(run_doctor(tmp_path), "database_integrity")
        assert db.message == "state.db healthy"
        assert "1 contract(s)" in db.details


class TestRegistryCheckDistinguishesMissingFromDisabled:
    def test_missing_registry_file_is_called_out(self, tmp_path: Path) -> None:
        """回归：文件不存在 → 空注册表 → 「0 enabled」ok=True，看不出文件没了。"""
        reg = _check(run_doctor(tmp_path), "executor_registry")
        assert "registry.json not found" in reg.details

    def test_zero_enabled_is_called_out(self, tmp_path: Path) -> None:
        (tmp_path / "registry.json").write_text('{"entries": []}', encoding="utf-8")
        reg = _check(run_doctor(tmp_path), "executor_registry")
        assert "none enabled" in reg.details


class TestReportShape:
    def test_expected_check_names_are_present(self, tmp_path: Path) -> None:
        names = {c.name for c in run_doctor(tmp_path).checks}
        assert names == {
            "python_runtime",
            "storage_directory",
            "database_integrity",
            "executor_registry",
            "kill_switch",
        }

    def test_fresh_dir_is_all_ok(self, tmp_path: Path) -> None:
        """全新目录不是故障——但每一项都得把「全新」说清楚。"""
        report = run_doctor(tmp_path)
        assert report.all_ok is True

    @pytest.mark.parametrize("name", ["database_integrity", "executor_registry"])
    def test_checks_carry_details(self, tmp_path: Path, name: str) -> None:
        assert _check(run_doctor(tmp_path), name).details
