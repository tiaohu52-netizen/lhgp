"""合同可见性与 workspace 排他（SPEC §11.2、共同维护风险防护）测试。

用户要求的三件事对应：
1. 模型能得知合同：task_prompt 带冻结区摘要（objective + 验收判据 +
   硬约束 + deadline），不是只有 objective 的盲干 prompt；
2. 用户能得知合同：contract.yaml 投影 + active.md 冻结区锚点（既有能力回归）；
3. 共同维护风险防护：同 workspace_root 有其他合同的活租约时本轮不派工
   （dispatch/deferred），workspace 每合同串行化。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_events,
    get_lease,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def make_draft(
    *,
    objective: str = "验证合同可见性",
    workspace: str | None = None,
    deadline: datetime | None = None,
) -> ContractDraft:
    hard: dict[str, object] = {}
    if workspace is not None:
        hard["file_effects"] = {"mode": "workspace-write", "workspace_root": workspace}
    return ContractDraft(
        title="可见性测试合同",
        objective=objective,
        deadline_at=deadline or NOW + timedelta(hours=2),
        hard_constraints=hard,
        acceptance=Acceptance(standard="验收标准文本", checks=("检查项甲", "检查项乙")),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
        ),
    )


def save_and_activate(data_dir: Path, cid: str, draft: ContractDraft) -> None:
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        ensure_schema(conn)
        save_contract(conn, draft, contract_id=cid, now=NOW)
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    finally:
        conn.close()


class TestExecutorPromptContractVisibility:
    """① 模型侧：被唤起的执行者必须能得知合同内容。"""

    def test_task_prompt_carries_full_frozen_zone(self, tmp_path: Path) -> None:
        from longtask.cli.runner import build_attempt_input
        from longtask.persistence.store import get_contract

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "ws").mkdir()
        cid = "lt-vis01"
        ws = str(data_dir / "ws")
        save_and_activate(data_dir, cid, make_draft(objective="把报告写完", workspace=ws))

        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            contract = get_contract(conn, cid)
            input_, _ = build_attempt_input(data_dir, conn, contract, "att-v1", NOW)
            prompt = input_.task_prompt
            # objective：干什么
            assert "把报告写完" in prompt
            # 验收判据：做到什么算完成（逐条可见 + 标准）
            assert "acceptance.checks" in prompt
            assert "检查项甲" in prompt and "检查项乙" in prompt
            assert "验收标准文本" in prompt
            # 硬约束：写权限边界（含 workspace_root；json 序列化路径为 \\）
            assert "hard_constraints" in prompt
            assert "workspace_root" in prompt
            assert ws.replace("\\", "\\\\") in prompt
            # deadline：时间边界
            assert "deadline_at" in prompt
            # 合同身份与修订号：模型知道自己在哪份合同下干活
            assert cid in prompt
        finally:
            conn.close()

    def test_no_hard_constraints_prompt_still_has_acceptance(self, tmp_path: Path) -> None:
        """无硬约束合同：验收判据仍必须在场（不能退化成裸 objective）。"""
        from longtask.cli.runner import build_attempt_input
        from longtask.persistence.store import get_contract

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-vis02"
        save_and_activate(data_dir, cid, make_draft())
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            contract = get_contract(conn, cid)
            input_, _ = build_attempt_input(data_dir, conn, contract, "att-v2", NOW)
            assert "检查项甲" in input_.task_prompt
            assert "hard_constraints" not in input_.task_prompt
        finally:
            conn.close()

    def test_spawned_child_receives_contract_in_prompt(self, tmp_path: Path) -> None:
        """端到端：真实子进程拿到的 argv 尾元素就是带冻结区的合同 prompt。"""
        from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
        from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
        from longtask.contracts.schema import Enforcement

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        manifest = ExecutorManifest(
            executor_id="exec-vis",
            adapter_version="0.1.0a0",
            transport="subprocess",
            capabilities=Capabilities(
                spawn=True,
                observe=True,
                cancel=True,
                notify=False,
                followup=False,
                steer=False,
                interrupt=True,
                context="optional",
                sandbox=SandboxCapability(
                    file_effects="workspace-write",
                    network="unsupported",
                    process="unsupported",
                    enforcement=Enforcement.PARTIAL,
                ),
                acceptance_evidence=True,
            ),
        )
        adapter = SubprocessAdapter(
            manifest,
            launch=LaunchSpec(
                argv=(sys.executable, "-c", ARGV_DUMPER), env_allowlist=("PATH", "SYSTEMROOT")
            ),
        )
        from longtask.adapters.base import AttemptInput
        from longtask.contracts.schema import AttemptRole

        draft = make_draft(objective="写入收条", workspace=str(ws))
        full_prompt = build_prompt_from(draft)
        input_ = AttemptInput(
            attempt_id="att-v3",
            contract_id="lt-vis03",
            revision=1,
            lease_generation=1,
            role=AttemptRole.EXECUTOR,
            contract_snapshot=draft.to_dict(),
            handover_path="handover.md",
            workspace_root=str(ws),
            budget_remaining={},
            task_prompt=full_prompt,
        )
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        try:
            import time

            deadline = time.time() + 15.0
            while time.time() < deadline:
                if (ws / "received.txt").is_file():
                    break
                time.sleep(0.1)
            received = (ws / "received.txt").read_text(encoding="utf-8")
            # 合同冻结区原文到达被唤起的执行者（单 argv 尾元素，无 shell 拼接）
            assert "检查项甲" in received
            assert "检查项乙" in received
            assert "验收标准文本" in received
        finally:
            try:
                adapter.cancel("att-v3", "测试收尾")
                adapter.collect("att-v3")
            except Exception:  # noqa: S110 —— 清理性收尾
                pass


# 子进程把 argv 原文落盘：argv 尾元素即 task_prompt（DESIGN §12.1）
ARGV_DUMPER = (
    "import sys, pathlib; pathlib.Path('received.txt').write_text("
    "sys.argv[1] if len(sys.argv) > 1 else '', encoding='utf-8')"
)


def build_prompt_from(draft: ContractDraft) -> str:
    """与 runner._executor_prompt 同构的 prompt（验证子进程收到合同）。"""
    sections = [
        "## objective",
        draft.objective,
        "## acceptance.checks",
        *(f"- {c}" for c in draft.acceptance.checks),
        f"- 标准：{draft.acceptance.standard}",
    ]
    return "\n".join(sections)


class TestWorkspaceExclusivity:
    """③ 共同维护风险：同 workspace 的跨合同并发写必须被挡下。"""

    def _setup_two_contracts(self, tmp_path: Path) -> Path:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        shared_ws = str(tmp_path / "shared-ws")
        # 两份合同声明同一个 workspace_root
        save_and_activate(data_dir, "lt-wsA", make_draft(objective="A 的目标", workspace=shared_ws))
        save_and_activate(data_dir, "lt-wsB", make_draft(objective="B 的目标", workspace=shared_ws))
        return data_dir

    def test_deferred_when_other_contract_holds_live_lease(self, tmp_path: Path) -> None:
        """A 持活租约 → B 的 RESPAWN 派工被延后（dispatch/deferred），不并发写。"""
        from longtask.cli.tick import _workspace_holder_other_than
        from longtask.persistence.store import acquire_lease, get_contract

        data_dir = self._setup_two_contracts(tmp_path)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            # A 拿到活租约（心跳新鲜）
            acquire_lease(
                conn,
                contract_id="lt-wsA",
                holder_attempt_id="att-a1",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
                actor="daemon",
            )
            contract_b = get_contract(conn, "lt-wsB")
            holder = _workspace_holder_other_than(conn, contract_b, NOW)
            assert holder is not None
            assert holder["contract_id"] == "lt-wsA"
        finally:
            conn.close()

    def test_no_conflict_when_lease_dead(self, tmp_path: Path) -> None:
        """A 的租约心跳已断（死租约）→ B 不受阻：回收路径会接管，不算占用。"""
        from longtask.cli.tick import _workspace_holder_other_than
        from longtask.persistence.store import acquire_lease, get_contract

        data_dir = self._setup_two_contracts(tmp_path)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            acquire_lease(
                conn,
                contract_id="lt-wsA",
                holder_attempt_id="att-a1",
                expected_generation=0,
                heartbeat_at=NOW - timedelta(hours=1),  # 早已过期
                timeout=timedelta(minutes=10),
                actor="daemon",
            )
            contract_b = get_contract(conn, "lt-wsB")
            assert _workspace_holder_other_than(conn, contract_b, NOW) is None
        finally:
            conn.close()

    def test_no_conflict_when_workspaces_differ(self, tmp_path: Path) -> None:
        """不同 workspace 的合同互不阻塞（排他只按目录，不按全局）。"""
        from longtask.cli.tick import _workspace_holder_other_than
        from longtask.persistence.store import acquire_lease, get_contract

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        save_and_activate(
            data_dir, "lt-wsC", make_draft(objective="C", workspace=str(tmp_path / "ws-c"))
        )
        save_and_activate(
            data_dir, "lt-wsD", make_draft(objective="D", workspace=str(tmp_path / "ws-d"))
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            acquire_lease(
                conn,
                contract_id="lt-wsC",
                holder_attempt_id="att-c1",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
                actor="daemon",
            )
            contract_d = get_contract(conn, "lt-wsD")
            assert _workspace_holder_other_than(conn, contract_d, NOW) is None
        finally:
            conn.close()

    def test_own_lease_does_not_block_self(self, tmp_path: Path) -> None:
        """自己的租约不算冲突（同合同重派由租约 fencing 兜底）。"""
        from longtask.cli.tick import _workspace_holder_other_than
        from longtask.persistence.store import acquire_lease, get_contract

        data_dir = self._setup_two_contracts(tmp_path)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            acquire_lease(
                conn,
                contract_id="lt-wsB",
                holder_attempt_id="att-b1",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
                actor="daemon",
            )
            contract_b = get_contract(conn, "lt-wsB")
            assert _workspace_holder_other_than(conn, contract_b, NOW) is None
        finally:
            conn.close()

    def test_workspace_path_normalization_matches_case_and_separators(self, tmp_path: Path) -> None:
        """D:/a 与 d:\\a\\ 是同一 workspace：盘符大小写与分隔符差异不能绕过排他。"""
        from longtask.cli.tick import _workspace_holder_other_than
        from longtask.persistence.store import acquire_lease, get_contract

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ws = tmp_path / "shared"
        ws.mkdir()
        save_and_activate(data_dir, "lt-wsE", make_draft(objective="E", workspace=str(ws)))
        # 同一路径的另一种写法：反斜杠 + 尾分隔符 + 盘符大小写不同
        ws_str = str(ws)
        variant = ws_str.replace("/", "\\")
        if len(variant) >= 2 and variant[1] == ":":
            variant = variant[0].swapcase() + variant[1:]
        variant += "\\"
        save_and_activate(data_dir, "lt-wsF", make_draft(objective="F", workspace=variant))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            acquire_lease(
                conn,
                contract_id="lt-wsE",
                holder_attempt_id="att-e1",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
                actor="daemon",
            )
            contract_f = get_contract(conn, "lt-wsF")
            holder = _workspace_holder_other_than(conn, contract_f, NOW)
            assert holder is not None and holder["contract_id"] == "lt-wsE"
        finally:
            conn.close()

    def test_workspace_case_only_variant_is_same_directory(self, tmp_path: Path) -> None:
        """安全审查 调度-C3：全路径大小写不同的同一物理目录不得绕过排他。

        旧归一化只小写盘符，D:/Data/WS 与 d:/data/ws 归一化后仍不相等。
        """
        from longtask.cli.tick import _norm_workspace

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ws = tmp_path / "SharedWs"
        ws.mkdir()
        ws_str = str(ws)
        # 大小写翻转变体（目录真实存在，realpath 可解析）
        variant = "".join(c.swapcase() if not c.isspace() else c for c in ws_str)
        assert variant != ws_str
        assert _norm_workspace(ws_str) == _norm_workspace(variant)

    def test_workspace_junction_like_alias_normalizes_to_target(self, tmp_path: Path) -> None:
        """符号链接/别名路径应归一化到真实目标（realpath）。"""
        import os as _os

        from longtask.cli.tick import _norm_workspace

        real = tmp_path / "real-ws"
        real.mkdir()
        link = tmp_path / "alias-ws"
        try:
            _os.symlink(real, link, target_is_directory=True)
        except OSError:
            pytest.skip("symlink not permitted on this platform")
        assert _norm_workspace(str(link)) == _norm_workspace(str(real))

    def test_tick_defers_second_contract_and_emits_event(self, tmp_path: Path) -> None:
        """run_daemon_tick 集成：A 活租约 + B 紧迫 → B 被记 dispatch/deferred，
        本轮 attempts_started 不含 B；A 结束（租约消失）后 B 可派。"""
        from longtask.adapters.manifest import Capabilities, SandboxCapability
        from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
        from longtask.cli.tick import run_daemon_tick
        from longtask.contracts.schema import Enforcement
        from longtask.persistence.store import acquire_lease

        data_dir = self._setup_two_contracts(tmp_path)
        (tmp_path / "shared-ws").mkdir(exist_ok=True)
        # 简单 subprocess 注册表（argv 用 -c 直接成功退出即可）
        reg = ExecutorRegistry()
        caps = Capabilities(
            spawn=True,
            observe=True,
            cancel=True,
            notify=False,
            followup=False,
            steer=False,
            interrupt=True,
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
                id="exec-ws",
                kind="subprocess",
                launch=LaunchSpec(
                    argv=(sys.executable, "-c"), env_allowlist=("PATH", "SYSTEMROOT")
                ),
                capabilities=caps,
                limits={"max_concurrent_attempts": 4},
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            # A 持活租约
            acquire_lease(
                conn,
                contract_id="lt-wsA",
                holder_attempt_id="att-a1",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
                actor="daemon",
            )
            res = run_daemon_tick(data_dir, conn, reg, now=NOW)
            # B 被延后：不派工
            started_ids = [s["contract_id"] for s in res["attempts_started"]]
            assert "lt-wsB" not in started_ids
            deferred_types = [
                str(e.event_type)
                for e in get_events(conn, contract_id="lt-wsB")
                if str(e.event_type) == EventType.DISPATCH_DEFERRED.value
            ]
            assert deferred_types, "B 必须留下 dispatch/deferred 审计事件"

            # A 暂停（不再推进）且租约释放后（下一轮）：B 恢复可派。
            # 不暂停的话 A 会继续按紧迫度抢回 workspace——排他本身会
            # 让两者严格串行，这正是设计语义。
            from longtask.persistence.store import release_lease, update_contract_state

            release_lease(
                conn,
                contract_id="lt-wsA",
                holder_attempt_id="att-a1",
                lease_generation=1,
                now=NOW + timedelta(minutes=1),
                actor="daemon",
            )
            update_contract_state(
                conn,
                contract_id="lt-wsA",
                new_state=ContractState.PAUSED,
                now=NOW + timedelta(minutes=1),
                event_type=EventType.CONTRACT_PAUSED,
                actor="daemon",
            )
            res2 = run_daemon_tick(data_dir, conn, reg, now=NOW + timedelta(minutes=1))
            started_ids2 = [s["contract_id"] for s in res2["attempts_started"]]
            assert "lt-wsB" in started_ids2
            assert get_lease(conn, "lt-wsB") is not None
        finally:
            conn.close()


class TestUserVisibilityRegression:
    """② 用户侧：contract.yaml 投影与冻结区锚点（回归保护）。"""

    def test_projection_writes_human_readable_contract_yaml(self, tmp_path: Path) -> None:
        from longtask.persistence.projections import format_contract_yaml
        from longtask.persistence.store import get_contract

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        save_and_activate(data_dir, "lt-vis04", make_draft())
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            contract = get_contract(conn, "lt-vis04")
            yaml_text = format_contract_yaml(contract)
            # 用户可读：目标、验收、deadline 都在投影里
            assert "验证合同可见性" in yaml_text
            assert "检查项甲" in yaml_text
            assert "deadline" in yaml_text.lower()
        finally:
            conn.close()

    def test_active_md_contains_frozen_zone_anchor(self, tmp_path: Path) -> None:
        """§4.1 active.md：冻结区锚点含验收与硬约束（模型侧第二通道）。"""
        from longtask.cli.runner import build_attempt_input
        from longtask.persistence.store import get_contract

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "ws").mkdir()
        cid = "lt-vis05"
        ws = str(data_dir / "ws")
        save_and_activate(data_dir, cid, make_draft(objective="快照锚点", workspace=ws))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            contract = get_contract(conn, cid)
            input_, _ = build_attempt_input(data_dir, conn, contract, "att-v5", NOW)
            assert input_.context_snapshot_path is not None
            active = Path(input_.context_snapshot_path).read_text(encoding="utf-8")
            assert "冻结区" in active
            assert "检查项甲" in active
            # hard_constraints 的 repr 含 workspace_root 键（路径分隔符形态不锁）
            assert "workspace_root" in active
        finally:
            conn.close()
