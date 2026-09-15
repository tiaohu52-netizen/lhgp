"""P6 分发与改名（SPEC §19.3、命名迁移窗口）测试。

双轨语义：
- default_data_root：新安装只认 ~/.lhgp；旧安装未迁移时 ~/.longtask
  继续生效；迁移后新路径优先；
- migrate：dry-run 默认不动数据、备份必做、拷贝式可回滚、幂等拒绝
  覆盖已有目标、迁移后 state.db 完整性校验；
- 入口：lhgp = longtask 同一 entrypoint；lhgpd 前台跑主循环。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from longtask.cli.paths import (
    default_data_root,
    migrate_data_dir,
)

pytestmark = pytest.mark.unit


def _legacy(tmp_home: Path) -> Path:
    return tmp_home / ".longtask"


def _new(tmp_home: Path) -> Path:
    return tmp_home / ".lhgp"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 Path.home() 指到临时目录：不碰真实 ~/.longtask / ~/.lhgp。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


class TestDefaultDataRoot:
    def test_fresh_install_uses_new_dir_only(self) -> None:
        """全新安装：只有 ~/.lhgp（新用户不见旧名）。"""
        root = default_data_root()
        assert root == _new(Path.home())

    def test_legacy_install_keeps_old_dir(self) -> None:
        """旧安装未迁移：~/.longtask 继续生效（行为与改名前一致）。"""
        _legacy(Path.home()).mkdir(parents=True)
        assert default_data_root() == _legacy(Path.home())

    def test_migrated_install_prefers_new_dir(self) -> None:
        """迁移后：新路径优先（旧路径留作回滚窗口）。"""
        _legacy(Path.home()).mkdir(parents=True)
        _new(Path.home()).mkdir(parents=True)
        assert default_data_root() == _new(Path.home())


class TestMigrate:
    def _seed_legacy(self) -> None:
        """旧数据目录现场：state.db + registry.json + contracts 投影。"""
        home = Path.home()
        _legacy(home).mkdir(parents=True)
        (_legacy(home) / "registry.json").write_text("{}", encoding="utf-8")
        cdir = _legacy(home) / "contracts" / "lt-x1"
        cdir.mkdir(parents=True)
        (cdir / "contract.yaml").write_text("title: x", encoding="utf-8")
        conn = sqlite3.connect(_legacy(home) / "state.db")
        try:
            conn.execute("CREATE TABLE contracts (contract_id TEXT)")
            conn.execute("INSERT INTO contracts VALUES ('lt-x1')")
            conn.commit()
        finally:
            conn.close()

    def test_dry_run_is_default_and_touches_nothing(self) -> None:
        """默认 dry-run：只打印计划，目标目录不被创建。"""
        self._seed_legacy()
        plan = migrate_data_dir()  # 不传 dry_run → 默认 True
        assert plan.dry_run is True
        assert plan.copied_files == 3  # state.db + registry.json + contract.yaml
        assert plan.backup_path is None
        assert not _new(Path.home()).exists()

    def test_real_migration_copies_backs_up_and_validates(self) -> None:
        """真跑：备份 + 拷贝 + 完整性校验 + 旧路径保留（可回滚）。"""
        self._seed_legacy()
        plan = migrate_data_dir(dry_run=False)
        assert plan.dry_run is False
        assert plan.backup_path is not None and plan.backup_path.is_dir()
        assert plan.backup_path.parent == Path.home() / ".lhgp-migration-backups"
        # 新路径数据齐
        assert (_new(Path.home()) / "state.db").is_file()
        assert (_new(Path.home()) / "contracts" / "lt-x1" / "contract.yaml").is_file()
        # 旧路径原样保留（回滚 = 删新路径）
        assert (_legacy(Path.home()) / "state.db").is_file()
        # 校验通过：无 FAILED 备注
        assert not [s for s in plan.skipped if "FAILED" in s]
        # 迁移后读取切到新路径
        assert default_data_root() == _new(Path.home())

    def test_corrupt_db_reports_integrity_failure(self) -> None:
        """迁移后 quick_check 失败：如实报，不静默。"""
        _legacy(Path.home()).mkdir(parents=True)
        # 伪造损坏的 db 文件（非 SQLite 格式）
        (_legacy(Path.home()) / "state.db").write_bytes(b"not a sqlite file at all")
        plan = migrate_data_dir(dry_run=False)
        assert any("FAILED" in s for s in plan.skipped)

    def test_idempotent_no_overwrite_of_populated_target(self) -> None:
        """目标已有内容：拒绝覆盖（防吞掉别人数据）。"""
        self._seed_legacy()
        _new(Path.home()).mkdir(parents=True)
        (_new(Path.home()) / "marker.txt").write_text("existing", encoding="utf-8")
        plan = migrate_data_dir(dry_run=False)
        assert any("refusing to overwrite" in s for s in plan.skipped)
        # 已有内容没被动
        assert (_new(Path.home()) / "marker.txt").read_text(encoding="utf-8") == "existing"

    def test_missing_source_is_noop(self) -> None:
        """source 不存在（已迁移/全新机器）：no-op，退出码语义友好。"""
        plan = migrate_data_dir(dry_run=False)
        assert plan.copied_files == 0
        assert any("nothing to migrate" in s for s in plan.skipped)

    def test_empty_source_migrates_nothing(self) -> None:
        """空 source：0 文件，目标被创建但为空。"""
        _legacy(Path.home()).mkdir(parents=True)
        plan = migrate_data_dir(dry_run=False)
        assert plan.copied_files == 0


class TestEntrypoints:
    def test_lhgp_uses_canonical_entrypoint_and_legacy_shim_remains(self) -> None:
        """Canonical names use the new namespace; legacy names remain shims."""
        import tomllib

        pyproject = tomllib.loads(
            (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        )
        scripts = pyproject["project"]["scripts"]
        assert scripts["lhgp"] == "lhgp.cli.main:entrypoint"
        assert scripts["lhgpd"] == "lhgp.cli.daemon_proc:lhgpd_entrypoint"
        assert scripts["lhgp-mcp"] == "lhgp.mcp_server:main"
        assert scripts["longtask"] == "longtask.cli.main:entrypoint"
        assert scripts["longtaskd"] == "longtask.cli.daemon_proc:lhgpd_entrypoint"
        assert scripts["longtask-mcp"] == "longtask.mcp_server:main"
        assert "lhgpd" in scripts

    def test_lhgpd_data_dir_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归：``lhgpd --data-dir X`` 必须真的用 X。

        旧实现完全不解析 ``sys.argv``，``root`` 恒为 ``default_data_root()``。
        于是守护进程会跑在**另一份**数据上——用户以为自己在管这份合同，
        实际派工、事件、工作区改动都发生在 ``~/.lhgp`` 里。
        """
        from longtask.cli.daemon_proc import lhgpd_entrypoint

        calls: list[dict] = []
        default_dir = tmp_path / "default"
        passed_dir = tmp_path / "passed"

        def fake_loop(root, *, emit_fn=None, **kwargs):
            calls.append({"root": root, "kwargs": kwargs})
            return {"ok": True, "cycles": 0}

        monkeypatch.setattr("longtask.cli.daemon_loop.run_daemon_loop", fake_loop)
        monkeypatch.setattr("longtask.cli.paths.default_data_root", lambda: default_dir)
        assert lhgpd_entrypoint(["--data-dir", str(passed_dir)]) == 0
        assert calls[0]["root"] == passed_dir
        assert passed_dir.is_dir()

    def test_lhgpd_defaults_when_no_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不带参数仍走默认目录（不能因为加了解析就破坏既有行为）。"""
        from longtask.cli.daemon_proc import lhgpd_entrypoint

        calls: list[dict] = []

        def fake_loop(root, *, emit_fn=None, **kwargs):
            calls.append({"root": root})
            return {"ok": True, "cycles": 0}

        monkeypatch.setattr("longtask.cli.daemon_loop.run_daemon_loop", fake_loop)
        monkeypatch.setattr("longtask.cli.paths.default_data_root", lambda: tmp_path)
        assert lhgpd_entrypoint([]) == 0
        assert calls[0]["root"] == tmp_path

    def test_lhgpd_interval_is_passed_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from longtask.cli.daemon_proc import lhgpd_entrypoint

        calls: list[dict] = []

        def fake_loop(root, *, emit_fn=None, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "cycles": 0}

        monkeypatch.setattr("longtask.cli.daemon_loop.run_daemon_loop", fake_loop)
        monkeypatch.setattr("longtask.cli.paths.default_data_root", lambda: tmp_path)
        assert lhgpd_entrypoint(["--interval", "7.5"]) == 0
        assert calls[0]["interval_seconds"] == 7.5

    def test_lhgpd_entrypoint_runs_loop_foreground(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lhgpd 前台入口：跑主循环并返回 ok（注入假 loop + 临时 root）。"""
        from longtask.cli.daemon_proc import lhgpd_entrypoint

        calls: list[dict] = []

        def fake_loop(root, *, emit_fn=None, **kwargs):
            calls.append({"root": root})
            return {"ok": True, "cycles": 0}

        monkeypatch.setattr("longtask.cli.daemon_loop.run_daemon_loop", fake_loop)
        monkeypatch.setattr("longtask.cli.paths.default_data_root", lambda: tmp_path)
        # 必须显式传 ``[]``：``argv=None`` 时 argparse 会去读 ``sys.argv[1:]``，
        # 而在 pytest 里那是 pytest 自己的参数（会报 unrecognized arguments）。
        code = lhgpd_entrypoint([])
        assert code == 0
        assert calls and calls[0]["root"] == tmp_path

    def test_migrate_cli_command_wires_dry_run_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """CLI：lhgp migrate 走 dry-run（不动数据），输出计划文本。"""
        from longtask.cli.main import main

        code = main(["migrate"])
        out = capsys.readouterr().out
        assert code == 0
        assert "dry-run" in out
        assert "nothing to migrate" in out  # source 不存在


class TestMigrateDataDirInteraction:
    """``migrate`` 与全局 ``--data-dir`` 的关系必须说清楚。

    回归两件事：
      1. ``--data-dir`` 对 migrate 无效却**静默**——用户以为在迁移指定目录；
      2. ``root.mkdir()`` 早于 migrate 分支，于是 dry-run 也会凭空建目录。
    """

    def test_migrate_dry_run_does_not_create_data_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from longtask.cli.main import main

        target = tmp_path / "should-not-exist"
        code = main(["--data-dir", str(target), "migrate"])
        assert code == 0
        assert not target.exists(), "dry-run 不得产生创建目录的副作用"
        err = capsys.readouterr().err
        assert "--data-dir does not apply" in err

    def test_migrate_without_data_dir_warns_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from longtask.cli.main import main

        code = main(["migrate"])
        assert code == 0
        assert "--data-dir does not apply" not in capsys.readouterr().err
