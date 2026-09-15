"""longtask CLI 与调度驱动单元/集成测试（DESIGN §11.1、§11.2、§15.2）。

测试覆盖：
1. --version 打印版本信息；
2. doctor 诊断检查（Python 版本、存储、数据库、注册表、熔断开关）；
3. 合同生命周期命令端到端流；
4. --dry-run 演练模式（仅打印参数，不写库）；
5. kill-switch 命令行激活、查询与解除；
6. rebuild 命令行从数据库物化文件投影；
7. daemon 调度 tick 运行闭环（Kill Switch 熔断、过期仲裁、升级阶梯驱动、执行器匹配与租约占领）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask import PROTOCOL_VERSION, __version__
from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.cli.daemon import (
    DAEMON_STOP_FILE,
    is_kill_switch_active,
    run_daemon_loop,
    run_daemon_tick,
    set_kill_switch,
)
from longtask.cli.doctor import run_doctor
from longtask.cli.main import main
from longtask.contracts.schema import Acceptance, Budget, ContractDraft, ContractState, Enforcement
from longtask.persistence.notifications import enqueue_notification
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    get_lease,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)


def get_events_in(data_dir: Path) -> list[str]:
    """读取数据目录下唯一合同的全部事件类型（按顺序）。"""
    import sqlite3

    conn = sqlite3.connect(data_dir / "state.db")
    try:
        rows = conn.execute("SELECT event_type FROM events ORDER BY event_id").fetchall()
        return [str(r[0]) for r in rows]
    finally:
        conn.close()


def make_test_registry() -> ExecutorRegistry:
    reg = ExecutorRegistry()
    caps = Capabilities(
        spawn=True,
        observe=True,
        cancel=True,
        notify=False,
        followup=False,
        steer=False,
        interrupt=False,
        context="optional",
        sandbox=SandboxCapability(
            file_effects="workspace-write",
            network="unsupported",
            process="unsupported",
            enforcement=Enforcement.PARTIAL,
        ),
        acceptance_evidence=True,
    )
    reg.register(
        RegistryEntry(
            id="test-executor",
            kind="subprocess",
            launch=LaunchSpec(argv=("codex", "exec")),
            capabilities=caps,
            limits={"max_concurrent_attempts": 2},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    return reg


class TestCliBasics:
    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--version"]) == 0
        out = capsys.readouterr().out
        assert __version__ in out
        assert f"protocol v{PROTOCOL_VERSION}" in out

    def test_lhgp_version_uses_new_entrypoint_name(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["lhgp"])
        assert main(["--version"]) == 0
        assert capsys.readouterr().out.startswith("lhgp ")

    def test_no_args_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "longtask" in out
        assert "prepare" in out
        assert "doctor" in out

    def test_notifications_read_only_command(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--data-dir", str(tmp_path), "notifications"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output == {"notifications": []}

    def test_notifications_rejects_out_of_range_limit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--data-dir", str(tmp_path), "notifications", "--limit", "201"]) == 1
        assert "between 1 and 200" in capsys.readouterr().err

    def test_notifications_filters_and_redacts_payload_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            ensure_schema(conn)
            enqueue_notification(
                conn,
                idempotency_key="cli-goal-a",
                goal_id="goal-a",
                event_type="need_user",
                channel="local",
                payload={"secret": "do-not-leak"},
                now=NOW,
            )
            enqueue_notification(
                conn,
                idempotency_key="cli-goal-b",
                goal_id="goal-b",
                event_type="satisfied",
                channel="local",
                payload={"other": True},
                now=NOW,
            )
        finally:
            conn.close()

        assert main(["--data-dir", str(tmp_path), "notifications", "--goal-id", "goal-a"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert len(output["notifications"]) == 1
        assert output["notifications"][0]["goal_id"] == "goal-a"
        assert "payload" not in output["notifications"][0]

    def test_doctor_report(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        report = run_doctor(tmp_path)
        assert report.all_ok
        assert report.protocol_version == PROTOCOL_VERSION

        assert main(["--data-dir", str(tmp_path), "doctor"]) == 0
        out = capsys.readouterr().out
        assert "ALL SYSTEMS GO" in out
        assert "python_runtime" in out

    def test_doctor_reports_missing_enabled_executable(self, tmp_path: Path) -> None:
        from longtask.adapters.fake_executor import FAKE_MANIFEST
        from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry

        registry = ExecutorRegistry()
        registry.register(
            RegistryEntry(
                id="missing-cli",
                kind="subprocess",
                launch=LaunchSpec(argv=("definitely-missing-lhgp-cli",)),
                capabilities=FAKE_MANIFEST.capabilities,
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
        registry.save_to_file(tmp_path / "registry.json")
        report = run_doctor(tmp_path)
        check = next(item for item in report.checks if item.name == "executor_registry")
        assert not check.ok
        assert "executable not found" in check.details


class TestCliDryRun:
    def test_dry_run_does_not_touch_db(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        assert (
            main(
                [
                    "--data-dir",
                    str(data_dir),
                    "--dry-run",
                    "prepare",
                    "--title",
                    "演练任务",
                    "--objective",
                    "演练完成目标",
                    "--deadline",
                    "2026-09-05T23:59:59+00:00",
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "contract/prepare" in out
        # 未创建 state.db
        assert not (data_dir / "state.db").exists()


class TestCliLifecycleCommands:
    def test_full_cli_lifecycle(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data_dir = tmp_path / "data"
        cid = "lt-20260901-001"

        # 1. prepare
        assert (
            main(
                [
                    "--data-dir",
                    str(data_dir),
                    "prepare",
                    "--contract-id",
                    cid,
                    "--title",
                    "CLI 测试合同",
                    "--objective",
                    "测试 CLI 完整流程",
                    "--deadline",
                    LATER.isoformat(),
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        prep_res = json.loads(out)
        assert prep_res["contract_id"] == cid
        assert prep_res["state"] == "drafted"

        # 2. get
        assert main(["--data-dir", str(data_dir), "get", cid]) == 0
        get_res = json.loads(capsys.readouterr().out)
        assert get_res["contract_id"] == cid

        # 3. approve
        assert main(["--data-dir", str(data_dir), "approve", cid, "--revision", "1"]) == 0
        app_res = json.loads(capsys.readouterr().out)
        assert app_res["state"] == "active"
        assert app_res["revision"] == 2

        # 4. list
        assert main(["--data-dir", str(data_dir), "list", "--state", "active"]) == 0
        list_res = json.loads(capsys.readouterr().out)
        assert len(list_res["contracts"]) == 1

        # 5. patch
        assert (
            main(
                [
                    "--data-dir",
                    str(data_dir),
                    "patch",
                    cid,
                    "--revision",
                    "2",
                    "--workload-hours",
                    "3.0",
                ]
            )
            == 0
        )
        patch_res = json.loads(capsys.readouterr().out)
        assert patch_res["workload_initial_hours"] == 3.0
        assert patch_res["revision"] == 3

        # 6. pause
        assert main(["--data-dir", str(data_dir), "pause", cid]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "paused"

        # 7. resume
        assert main(["--data-dir", str(data_dir), "resume", cid]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "active"

        # 8. cancel
        assert main(["--data-dir", str(data_dir), "cancel", cid, "--reason", "用户测试取消"]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "cancelled"

    def test_kill_switch_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data_dir = tmp_path / "data"

        # 查询（初始未激活）
        assert main(["--data-dir", str(data_dir), "kill-switch", "--check"]) == 0
        assert "inactive" in capsys.readouterr().out

        # 激活
        assert main(["--data-dir", str(data_dir), "kill-switch", "--activate"]) == 0
        assert "ACTIVE" in capsys.readouterr().out
        assert is_kill_switch_active(data_dir)

        # 解除
        assert main(["--data-dir", str(data_dir), "kill-switch", "--deactivate"]) == 0
        assert "inactive" in capsys.readouterr().out
        assert not is_kill_switch_active(data_dir)

    def test_rebuild_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data_dir = tmp_path / "data"
        cid = "lt-20260901-002"
        main(
            [
                "--data-dir",
                str(data_dir),
                "prepare",
                "--contract-id",
                cid,
                "--title",
                "重建测试",
                "--objective",
                "测试 rebuild CLI",
                "--deadline",
                LATER.isoformat(),
            ]
        )
        capsys.readouterr()

        assert main(["--data-dir", str(data_dir), "rebuild", cid]) == 0
        out = capsys.readouterr().out
        assert "projections materialized" in out
        assert (data_dir / "contracts" / cid / "contract.yaml").is_file()


class TestDaemonSchedulerRunner:
    def test_daemon_tick_dispatch_and_kill_switch(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)

        reg = make_test_registry()
        reg.save_to_file(data_dir / "registry.json")

        cid = "lt-20260901-003"
        draft = ContractDraft(
            title="调度测试合同",
            objective="测试 daemon tick 推进闭环",
            deadline_at=NOW + timedelta(hours=2),
            # 声明 workspace_root：dispatch 的 prepare 探针要求可绑定的工作区（DESIGN §9）
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(data_dir / "ws"),
                }
            },
            acceptance=Acceptance(standard="测试通过", checks=("通过",)),
            workload_initial_hours=5.0,  # 5小时工作 / 2小时剩余 = u=2.5 (RESPAWN)
            budget=Budget(
                max_dispatches=5,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=60,
                max_output_bytes=1048576,
            ),
        )
        save_contract(conn, draft, contract_id=cid, now=NOW)
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)

        # 1. 运行单次调度 tick：紧迫度 u=2.5 触发 RESPAWN 派工
        res = run_daemon_tick(data_dir, conn, reg, now=NOW)
        assert res["ok"] is True
        assert res["dispatched"] == 1
        assert any("dispatched" in ev for ev in res["events"])

        # 验证租约已被占用
        lease = get_lease(conn, cid)
        assert lease is not None
        assert lease.generation == 1
        assert lease.is_alive(NOW)

        # 验证投影已物化
        assert (data_dir / "contracts" / cid / "contract.yaml").is_file()
        assert (data_dir / "contracts" / cid / "lease.json").is_file()

        # 2. 激活 Kill Switch，再次调度：熔断拦截
        set_kill_switch(data_dir, True)
        res_ks = run_daemon_tick(data_dir, conn, reg, now=NOW + timedelta(minutes=1))
        assert res_ks["status"] == "halted_by_kill_switch"
        assert res_ks["processed"] == 0

        # 3. 越过 Deadline：解除 Kill Switch 后应仲裁为 EXPIRED
        set_kill_switch(data_dir, False)
        future_now = NOW + timedelta(hours=10)
        res_exp = run_daemon_tick(data_dir, conn, reg, now=future_now)
        assert res_exp["expired"] == 1

        c_after = get_contract(conn, cid)
        assert c_after is not None
        assert c_after.state == ContractState.EXPIRED
        conn.close()


class TestDaemonLoop:
    """常驻主循环（DESIGN §3.3、§15.2）：时间与睡眠注入，无真实墙钟依赖。"""

    @staticmethod
    def _make_active_contract(tmp_path: Path) -> Path:
        """建库 + 注册表 + 一个紧迫 active 合同，返回数据目录。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        make_test_registry().save_to_file(data_dir / "registry.json")
        cid = "lt-20260901-loop"
        draft = ContractDraft(
            title="主循环调度合同",
            objective="验证 run_daemon_loop 调度闭环",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(data_dir / "ws"),
                }
            },
            acceptance=Acceptance(standard="测试通过", checks=("通过",)),
            workload_initial_hours=5.0,  # u=2.5 -> RESPAWN 档
            budget=Budget(
                max_dispatches=5,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=60,
                max_output_bytes=1048576,
            ),
        )
        save_contract(conn, draft, contract_id=cid, now=NOW)
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
        conn.close()
        return data_dir

    def test_loop_runs_cycles_with_injected_clock(self, tmp_path: Path) -> None:
        data_dir = self._make_active_contract(tmp_path)
        # submit-and-leave (2026-09-08): the daemon's startup
        # scan now calls ``now_fn`` once before the per-tick
        # loop begins, so provide an extra timestamp for the
        # three subsequent cycles.
        times = iter(
            [
                NOW,
                NOW + timedelta(minutes=1),
                NOW + timedelta(minutes=2),
                NOW + timedelta(minutes=3),
            ]
        )
        sleeps: list[float] = []

        res = run_daemon_loop(
            data_dir,
            interval_seconds=60.0,
            max_cycles=3,
            now_fn=lambda: next(times),
            sleep_fn=sleeps.append,
        )

        assert res["ok"] is True
        assert res["cycles"] == 3
        assert res["stopped_by_stop_file"] is False
        # 轮与轮之间各睡一次间隔；最后一轮后不睡
        assert sleeps == [60.0, 60.0]
        # 每轮都派工并真实走 spawn：registry 的 argv 指向不存在的可执行文件
        # （codex），spawn OSError -> attempt/failed + 租约释放 -> 下轮再派。
        # 三轮共 3 次派工、3 次拉起失败收尾（预算 5 未耗尽）。
        assert res["dispatched"] == 3
        assert res["spawned"] == 0  # spawn 全部失败，无成功拉起
        assert res["finished"] == 3  # 失败也是一次收尾（attempt/failed）

        # 事件流验证：每轮 attempt/started 后紧跟 attempt/failed（spawn OSError）
        events = get_events_in(data_dir)
        started = [e for e in events if e == "attempt/started"]
        failed = [e for e in events if e == "attempt/failed"]
        assert len(started) == 3
        assert len(failed) == 3

    def test_stop_file_exits_gracefully_and_cleans_up(self, tmp_path: Path) -> None:
        data_dir = self._make_active_contract(tmp_path)
        (data_dir / DAEMON_STOP_FILE).write_text("stop requested\n", encoding="utf-8")
        called = {"n": 0}

        def fake_sleep(_seconds: float) -> None:
            called["n"] += 1

        res = run_daemon_loop(
            data_dir,
            interval_seconds=60.0,
            max_cycles=5,
            now_fn=lambda: NOW,
            sleep_fn=fake_sleep,
        )

        assert res["ok"] is True
        assert res["cycles"] == 0
        assert res["stopped_by_stop_file"] is True
        assert called["n"] == 0  # 从未进入循环体
        # 退出时清理停止标记，下次 start 不会被残留标记立刻杀掉
        assert not (data_dir / DAEMON_STOP_FILE).exists()


def test_request_verification_cli_dry_run(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """用户可从 CLI 发起仅验收请求，且 dry-run 不写入状态。"""
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "--dry-run",
                "request-verification",
                "lt-20260904-verify",
                "--reason",
                "检查现有交付物",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "contract/request-verification" in output
    assert "检查现有交付物" in output


@pytest.mark.real_entry
class TestCliFacadeDirection:
    """`lhgp/cli/` 是 `longtask/cli/` 的门面——方向必须与地图一致。

    pyproject 的入口注释曾写「旧名保留一个次版本作为兼容 shim」，方向正好说反：
    canonical 的 `lhgp`/`lhgpd`/`lhgp-mcp` 才是指向 longtask 实现的 shim。这个
    方向被读错就会重演审计 B2 的失败模式：在 3 行门面里找真实现（或改错那一
    侧）。此处把实测方向钉住，改反了会红。

    带 `real_entry` 标记：本类确实会导入真实入口模块（`lhgp.cli.main` /
    `longtask.cli.main`）做对象同一性核对，不是替身。
    """

    def test_canonical_cli_modules_are_thin_facades(self) -> None:
        canonical = REPO_ROOT / "src" / "lhgp" / "cli"
        implementation = REPO_ROOT / "src" / "longtask" / "cli"
        assert canonical.is_dir() and implementation.is_dir()
        for path in sorted(canonical.glob("*.py")):
            if path.name == "__init__.py":
                continue
            source = path.read_text(encoding="utf-8")
            lines = len([line for line in source.splitlines() if line.strip()])
            assert lines <= 20, (
                f"{path.name} 不再是薄门面（{lines} 行）——CLI 真身方向可能变了，"
                "请同步 ARCHITECTURE「真身位置地图」"
            )
            assert f"from longtask.cli.{path.stem} import" in source, (
                f"{path.name} 不再转发 longtask.cli.{path.stem}"
            )

    def test_facade_re_exports_the_same_objects(self) -> None:
        """门面必须完整转发真身的**公开**面，且是同一批对象（不是复制品）。

        只比公开名：真身未定义 ``__all__``，门面走 ``import *``，下划线私有辅助
        函数（``_dispatch_rpc`` 等 4 个）按 Python 语义本就不进 ``import *``。
        实测确认这是既定边界而非缺口——仓库内无任何代码经 canonical 命名空间
        访问私有名（``lhgp.cli.*._private`` 零引用），且真身的 40 个公开名门面
        一个不少。把私有名也要求转发会造出一条永远红的假断言。
        """
        import importlib

        for name in ("main", "runner", "tick"):
            canonical = importlib.import_module(f"lhgp.cli.{name}")
            implementation = importlib.import_module(f"longtask.cli.{name}")
            expected = {n for n in dir(implementation) if not n.startswith("_")}
            missing = {n for n in expected if not hasattr(canonical, n)}
            assert missing == set(), f"lhgp.cli.{name} 漏了公开名 {sorted(missing)}"
            for attr in ("main", "entrypoint"):
                if hasattr(implementation, attr):
                    assert getattr(canonical, attr) is getattr(implementation, attr)


@pytest.mark.real_entry
class TestCliJsonErrorHandling:
    """CLI 的 JSON 输入必须给友好错误，不是裸 traceback。

    这些用例真的走 ``longtask.cli.main.main``，按项目棘轮（``quality/
    real-entry-baseline.json``，只许下调）必须打 ``real_entry`` 标记，
    否则未标记数会超过 114 让整个会话以 UsageError 中止。

    回归：``prepare --file`` 的 ``read_text``/``json.loads`` 与
    ``patch --guidance`` 的 ``json.loads`` 都未捕获，坏输入直接抛栈；
    而同文件的 ``plan submit`` 早就有 ``Error: invalid JSON`` + exit 2
    ——同一个 CLI 里两种待遇。
    """

    def test_prepare_file_bad_json_returns_2(self, tmp_path: Path, capsys) -> None:
        from longtask.cli.main import main

        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code = main(["--data-dir", str(tmp_path / "data"), "prepare", "--file", str(bad)])
        assert code == 2
        assert "invalid JSON" in capsys.readouterr().err

    def test_prepare_file_missing_returns_2(self, tmp_path: Path, capsys) -> None:
        from longtask.cli.main import main

        code = main(
            ["--data-dir", str(tmp_path / "data"), "prepare", "--file", str(tmp_path / "nope.json")]
        )
        assert code == 2
        assert "cannot read" in capsys.readouterr().err

    def test_prepare_file_non_object_returns_2(self, tmp_path: Path, capsys) -> None:
        from longtask.cli.main import main

        arr = tmp_path / "arr.json"
        arr.write_text("[1, 2]", encoding="utf-8")
        code = main(["--data-dir", str(tmp_path / "data"), "prepare", "--file", str(arr)])
        assert code == 2
        assert "must be a JSON object" in capsys.readouterr().err

    def test_patch_bad_guidance_json_returns_2(self, tmp_path: Path, capsys) -> None:
        from longtask.cli.main import main

        code = main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "patch",
                "lt-x",
                "--revision",
                "1",
                "--guidance",
                "{oops",
            ]
        )
        assert code == 2
        assert "invalid JSON" in capsys.readouterr().err


@pytest.mark.real_entry
class TestDryRunCoversDirectWrites:
    """``--dry-run`` 必须真的不写。

    回归：``--help`` 声明「不写库」，RPC 路径也确实拦了，但 plan submit /
    signoff、contract user-confirm、feedback、memory add/expire、
    proposal-apply、prune-events、start/stop/kill-switch 全部**绕过 RPC
    直连 DB**（或起停进程），旧实现照写不误。
    """

    def _db(self, tmp_path: Path):
        from longtask.persistence.store import StoreConfig, connect, ensure_schema

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        return data_dir, conn

    def test_plan_submit_is_skipped(self, tmp_path: Path, capsys) -> None:
        from longtask.cli.main import main

        data_dir, conn = self._db(tmp_path)
        conn.close()
        code = main(["--data-dir", str(data_dir), "--dry-run", "plan", "submit", "lt-dry-1"])
        assert code == 0
        assert "would plan submit" in capsys.readouterr().out

    def test_plan_submit_writes_nothing(self, tmp_path: Path) -> None:
        from longtask.cli.main import main
        from longtask.persistence.store import StoreConfig, connect, get_events

        data_dir, conn = self._db(tmp_path)
        conn.close()
        main(["--data-dir", str(data_dir), "--dry-run", "plan", "submit", "lt-dry-1"])
        c = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            assert get_events(c, contract_id="lt-dry-1") == []
        finally:
            c.close()

    @pytest.mark.parametrize(
        "argv",
        [
            ["start"],
            ["stop"],
            ["kill-switch", "--activate"],
            # proposal-apply 用位置参数：<goal_id> <event_id>
            ["proposal-apply", "goal-1", "1"],
            ["prune-events", "--keep-days", "30"],
            # contract_id 是子命令的位置参数，不是父命令的
            ["contract", "user-confirm", "lt-x"],
            ["memory", "add", "--title", "t"],
        ],
    )
    def test_direct_write_commands_are_declared(self, tmp_path: Path, capsys, argv: list) -> None:
        from longtask.cli.main import main

        data_dir, conn = self._db(tmp_path)
        conn.close()
        code = main(["--data-dir", str(data_dir), "--dry-run", *argv])
        assert code == 0
        assert "would " in capsys.readouterr().out

    def test_read_only_command_is_not_declared(self, tmp_path: Path, capsys) -> None:
        """只读子命令不得被守卫误伤——``--dry-run`` 不该挡住读。"""
        from longtask.cli.main import main

        data_dir, conn = self._db(tmp_path)
        conn.close()
        code = main(["--data-dir", str(data_dir), "--dry-run", "memory", "list"])
        assert "would " not in capsys.readouterr().out
        assert code == 0

    def test_without_dry_run_submit_still_writes(self, tmp_path: Path) -> None:
        """不加 --dry-run 时行为不变（守卫只在 dry-run 下生效）。"""
        from longtask.cli.main import main
        from longtask.persistence.store import StoreConfig, connect, get_events

        data_dir, conn = self._db(tmp_path)
        from longtask.contracts.schema import Acceptance, Budget, ContractDraft
        from longtask.persistence.store import save_contract

        save_contract(
            conn,
            ContractDraft(
                title="t",
                objective="o",
                deadline_at=__import__("datetime").datetime(
                    2030, 1, 1, tzinfo=__import__("datetime").UTC
                ),
                hard_constraints={},
                acceptance=Acceptance(standard="s", checks=("c",)),
                workload_initial_hours=1.0,
                budget=Budget(
                    max_dispatches=3,
                    max_escalations=1,
                    max_concurrent_attempts=1,
                    max_attempt_minutes=30,
                    max_output_bytes=1024,
                ),
            ),
            contract_id="lt-real-1",
            now=__import__("datetime").datetime(2026, 9, 12, tzinfo=__import__("datetime").UTC),
        )
        conn.close()
        # 不带 --from-file 时 plan submit 读 stdin（pytest 下为空 → 解析失败），
        # 所以这里显式给文件。
        plan_file = tmp_path / "plan.json"
        plan_file.write_text('{"steps": [{"id": "s1", "title": "t"}]}', encoding="utf-8")
        code = main(
            [
                "--data-dir",
                str(data_dir),
                "plan",
                "submit",
                "lt-real-1",
                "--from",
                str(plan_file),
            ]
        )
        # 退出码取决于合同能否接受该计划（这里合同是 DRAFTED，可能返回 1）；
        # 本用例要钉的是「**真的写了**」，不是退出码。
        assert code != 2, "plan submit 不该被当成参数错误拒绝"
        c = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            assert get_events(c, contract_id="lt-real-1")
        finally:
            c.close()


@pytest.mark.real_entry
class TestRebuildHonoursHandEdits:
    """``lhgp rebuild`` 不得静默覆盖人手改过的投影。

    DESIGN §3.1 的「人类编辑门」此前只存在于注释里：``check_projection_dirty``
    全仓无调用，``PROJECTION_DIRTY`` 事件从未写出，而 ``rebuild_projection``
    会直接把盘上的 contract.yaml 覆盖成权威库序列化结果——用户手改的内容
    无声消失。
    """

    @staticmethod
    def _rb_draft(data_dir: Path) -> ContractDraft:
        return ContractDraft(
            title="重建测试合同",
            objective="验证 rebuild 的手改保护",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(data_dir / "ws"),
                }
            },
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

    @staticmethod
    def _rb_draft(data_dir: Path) -> ContractDraft:
        return ContractDraft(
            title="重建测试合同",
            objective="验证 rebuild 的手改保护",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(data_dir / "ws"),
                }
            },
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

    def _materialize(self, tmp_path: Path) -> Path:
        from longtask.persistence.store import (
            StoreConfig,
            connect,
            ensure_schema,
            save_contract,
            update_contract_state,
        )

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            ensure_schema(conn)
            save_contract(conn, self._rb_draft(data_dir), contract_id="lt-rb-1", now=NOW)
            update_contract_state(
                conn,
                contract_id="lt-rb-1",
                new_state=ContractState.ACTIVE,
                now=NOW,
            )
        finally:
            conn.close()
        return data_dir

    def test_clean_projection_rebuilds_normally(self, tmp_path: Path, capsys) -> None:
        from longtask.cli.main import main

        data_dir = self._materialize(tmp_path)
        # 先生成一次投影，再重建（此时盘上与权威一致）
        assert main(["--data-dir", str(data_dir), "rebuild", "lt-rb-1", "--revert"]) == 0
        code = main(["--data-dir", str(data_dir), "rebuild", "lt-rb-1"])
        assert code == 0
        assert "materialized" in capsys.readouterr().out

    def test_dirty_projection_is_refused_without_revert(self, tmp_path: Path, capsys) -> None:
        from longtask.cli.main import main

        data_dir = self._materialize(tmp_path)
        assert main(["--data-dir", str(data_dir), "rebuild", "lt-rb-1", "--revert"]) == 0
        # 模拟用户手改：把投影写成与权威库不一致
        proj = data_dir / "contracts" / "lt-rb-1" / "contract.yaml"
        assert proj.is_file(), "投影文件应已生成"
        proj.write_text(proj.read_text(encoding="utf-8") + "\n# hand edited\n", encoding="utf-8")

        code = main(["--data-dir", str(data_dir), "rebuild", "lt-rb-1"])
        assert code == 1
        assert "edited on disk" in capsys.readouterr().err

    def test_revert_overwrites_by_design(self, tmp_path: Path, capsys) -> None:
        """--revert 的语义就是"以库为准"，脏了也要覆盖。"""
        from longtask.cli.main import main

        data_dir = self._materialize(tmp_path)
        assert main(["--data-dir", str(data_dir), "rebuild", "lt-rb-1", "--revert"]) == 0
        proj = data_dir / "contracts" / "lt-rb-1" / "contract.yaml"
        proj.write_text(proj.read_text(encoding="utf-8") + "\n# hand edited\n", encoding="utf-8")

        code = main(["--data-dir", str(data_dir), "rebuild", "lt-rb-1", "--revert"])
        assert code == 0
        assert "# hand edited" not in proj.read_text(encoding="utf-8")
