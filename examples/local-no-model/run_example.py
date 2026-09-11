#!/usr/bin/env python3
"""可复制的本地执行示例（RELEASE-PLAN R4a）：无模型、无密钥、无网络。

依次走完 doctor → prepare → approve → execute → verify → satisfied：

1. **doctor**：CLI 自检（解释器/存储/数据库/注册表/熔断）；
2. **prepare**：起草合同（冻结区：目标、截止、硬约束、验收、预算、授权）；
3. **approve**：用户批准（drafted → active）；
4. **execute + verify**：由**真实调度轮次**（run_daemon_tick）派工——执行者
   与核验者都是普通程序（worker.py / checker.py），通过 stdout 协议通道
   回报（完成事件行 + lhgp-verdict 判定块），不回调 RPC；
5. 收尾断言：交付物内容 + 合同终态。

**不使用人工补写成功事件的 harness**：所有状态变迁都由调度器与验收路径
产生，脚本只驱动时钟与打印过程。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# 源码检出即可运行：把仓库 src/ 放上 sys.path（安装版则无需）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from longtask.adapters.registry import (  # noqa: E402
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon_loop import run_daemon_loop  # noqa: E402
from longtask.cli.main import main as cli_main  # noqa: E402
from longtask.persistence.store import (  # noqa: E402
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
)

CONTRACT_ID = "lt-local-example"
DELIVERABLE = "result.txt"
MARKER = "LHGP-LOCAL-EXAMPLE-OK"
# 一个连续调度会话的轮次上限（不是每次重启——见 _drive 的说明）。
MAX_CYCLES = 60
# 每轮之间的真实让步（秒）：子进程需要真实时间退出并被 poll 回收，
# 模拟时钟推不动它。60 轮 × 0.25s ≈ 15 秒真实上限。
REAL_YIELD_SECONDS = 0.25
# 模拟时钟步长（秒）：只推动合同的决策时间。
SIMULATED_STEP_SECONDS = 5


def _banner(step: str, detail: str = "") -> None:
    print(f"\n=== {step} ===" + (f"  {detail}" if detail else ""))


def _write_registry(data_dir: Path, workspace: Path) -> None:
    """把两个普通程序注册成执行器（执行者 / 独立核验者，各自独立 argv）。"""
    registry = ExecutorRegistry()
    for entry_id, script in (("local-worker", "worker.py"), ("local-checker", "checker.py")):
        registry.register(
            RegistryEntry(
                id=entry_id,
                kind="subprocess",
                launch=LaunchSpec(
                    argv=(sys.executable, str(Path(__file__).parent / script)),
                    env_allowlist=(
                        "SYSTEMROOT",
                        "SYSTEMDRIVE",
                        "WINDIR",
                        "COMSPEC",
                        "PATH",
                        "TEMP",
                        "TMP",
                    ),
                ),
                capabilities=_capabilities(),
                limits={"max_concurrent_attempts": 1},
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
    registry.save_to_file(data_dir / "registry.json")
    print(f"注册表已写入 {data_dir / 'registry.json'}（两个 subprocess 执行器）")
    print(f"工作区（合同 workspace_root）：{workspace}")


def _capabilities():
    from lhgp.adapters.manifest import Capabilities, SandboxCapability

    return Capabilities(
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
            network="deny",
            process="unsupported",
            enforcement="partial",
        ),
        acceptance_evidence=True,
    )


def _draft(workspace: Path, now: datetime) -> dict:
    """合同草稿：验收是 typed check（机器可复现），核验走独立交叉验证。"""
    return {
        "title": "本地无模型示例",
        "objective": f"在 workspace 生成 {DELIVERABLE}，内容包含 {MARKER}",
        "deadline_at": (now + timedelta(minutes=45)).isoformat(),
        "hard_constraints": {"file_effects": {"mode": "workspace-write", "workspace_root": str(workspace)}},
        "acceptance": {
            "standard": f"{DELIVERABLE} 存在且内容包含 {MARKER}",
            "checks": [{"kind": "file-exists", "target": DELIVERABLE}],
            "verifier": "cross_check",
        },
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 4,
            "max_escalations": 2,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 5,
            "max_output_bytes": 262144,
        },
        # 授权：只有这两个本地程序可被派工（default-deny）
        "authority": {
            "executor_policy": "explicit_allow",
            "executors": [
                {"executor_id": "local-worker", "models": ["*"], "roles": ["executor"]},
                {"executor_id": "local-checker", "models": ["*"], "roles": ["verifier"]},
            ],
        },
    }


class _SteppingClock:
    """推进式时钟：每次被询问前进 step_seconds，让轮次在模拟时间上推进。"""

    def __init__(self, start: datetime, step_seconds: int = SIMULATED_STEP_SECONDS) -> None:
        self._now = start
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        self._now = self._now + self._step
        return self._now


def _drive(data_dir: Path, now: datetime) -> str:
    """由**真实调度主循环**推进（run_daemon_loop）——示例不自己拼 tick，
    不伪造任何事件：派工、回收、交叉验收全部由产品路径产生。

    关键约束（实测教训）：**必须跑一个连续会话**。每轮新建 runner 等价于
    守护进程重启——新 runner 不持有原 Popen，reconcile 会按 §11.3 把在飞
    attempt 判为 detached（退出码不可回收）→ failed。那是崩溃恢复的诚实
    边界而非缺陷，但它不是本示例要演示的东西。
    """
    emitted: list[str] = []
    run_daemon_loop(
        data_dir,
        max_cycles=MAX_CYCLES,
        interval_seconds=REAL_YIELD_SECONDS,
        now_fn=_SteppingClock(now),
        emit_fn=emitted.append,
    )
    for line in emitted:
        print(f"  {line}")

    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        rows = conn.execute(
            "SELECT role, state, executor_id FROM attempts WHERE contract_id = ?"
            " ORDER BY admitted_at",
            (CONTRACT_ID,),
        ).fetchall()
        for role, a_state, executor_id in rows:
            print(f"  attempt: role={role:<9} state={a_state:<10} executor={executor_id}")
        view = get_contract(conn, CONTRACT_ID)
        return view.state.value if view else "?"
    finally:
        conn.close()


def _troubleshoot(data_dir: Path) -> None:
    print("\n排查出口：")
    print(f"  1) 合同详情: lhgp --data-dir {data_dir} get {CONTRACT_ID}")
    print(f"  2) 事件时间轴: lhgp --data-dir {data_dir} stats {CONTRACT_ID}")
    print(f"  3) 逐条看事件: python -c \"import sqlite3;db=sqlite3.connect(r'{data_dir / 'state.db'}');"
          "print(*db.execute('select event_type,actor,payload_json from events order by event_id'))\"")
    print("  4) 拒接原因在事件流的 dispatch/refused 与 contract/blocked payload 里")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).parent / "example-data"),
        help="示例数据目录（默认 examples/local-no-model/example-data）",
    )
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace = data_dir / "ws"
    workspace.mkdir(exist_ok=True)
    now = datetime.now(UTC)

    _banner("doctor", "CLI 自检")
    rc = cli_main(["--data-dir", str(data_dir), "doctor"])
    if rc != 0:
        print("doctor 未通过——按上面 FAIL 项处理后重跑", file=sys.stderr)
        return rc

    _banner("registry", "注册两个本地程序（执行者 / 独立核验者）")
    _write_registry(data_dir, workspace)

    _banner("prepare", "起草合同")
    draft_path = data_dir / "draft.json"
    draft_path.write_text(json.dumps(_draft(workspace, now), ensure_ascii=False, indent=2), encoding="utf-8")
    rc = cli_main(
        ["--data-dir", str(data_dir), "prepare", "--file", str(draft_path), "--contract-id", CONTRACT_ID]
    )
    if rc != 0:
        print("prepare 失败——草稿在 " + str(draft_path), file=sys.stderr)
        return rc

    _banner("approve", "用户批准（drafted → active）")
    rc = cli_main(["--data-dir", str(data_dir), "approve", CONTRACT_ID])
    if rc != 0:
        print("approve 失败", file=sys.stderr)
        return rc

    _banner("execute / verify", "真实调度轮次派工：worker.py → checker.py")
    final_state = _drive(data_dir, now)

    _banner("result", f"终态 {final_state}")
    deliverable = workspace / DELIVERABLE
    if final_state in ("satisfied", "complete") and deliverable.is_file():
        print(f"交付物 {deliverable} 内容：")
        print("  " + deliverable.read_text(encoding="utf-8").strip().replace("\n", "\n  "))
        print("\n完成：doctor → prepare → approve → execute → verify → satisfied 全链路通过。")
        return 0
    print(f"未达终态（{final_state}）。", file=sys.stderr)
    _troubleshoot(data_dir)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
