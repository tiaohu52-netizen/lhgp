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
