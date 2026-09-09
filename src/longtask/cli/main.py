"""longtask CLI 控制面入口（DESIGN §11.1、§11.2、§15.2 Developer Preview）。

提供：
1. `doctor`：系统自检与诊断；
2. 合同生命周期命令（prepare/approve/get/list/patch/pause/resume/cancel/
   arbitrate/request-verification）；
3. `executor`：执行器注册与框定控制；
4. `kill-switch`：全局 Emergency Stop 熔断控制；
5. `rebuild`：从数据库重建文件投影；
6. `status` / `start` / `stop`：守护进程起停控制；
7. 全局 `--dry-run` 模拟执行与 `--data-dir` 隔离测试。
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lhgp.contracts.plan import PlanStep
from longtask import PROTOCOL_VERSION, __version__
from longtask.adapters.registry import ExecutorRegistry
from longtask.cli.daemon import (
    DEFAULT_TICK_INTERVAL_SECONDS,
    get_daemon_status,
    halt_daemon,
    is_kill_switch_active,
    rpc_socket_path,
    run_daemon_loop,
    set_kill_switch,
    spawn_daemon,
)
from longtask.cli.doctor import run_doctor
from longtask.cli.paths import default_data_root, migrate_data_dir
from longtask.console import harden_stdio
from longtask.persistence.projections import rebuild_projection, revert_projection
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    list_contracts,
)
from longtask.rpc.client import call_unix_socket
from longtask.rpc.errors import RpcError
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope, route


def _open_read_conn(data_dir: str | None) -> sqlite3.Connection:
    """以只读意图打开默认/指定数据目录的权威库（insights 系命令共用）。"""

    root = Path(data_dir).expanduser().resolve() if data_dir else default_data_root()
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    return conn


def _read_plan_input(from_file: str | None) -> dict[str, Any]:
    """Read a plan JSON payload from --from path or stdin and decode it.

    Used by both ``lhgp plan submit`` and ``lhgp plan signoff`` so the
    file/stdin contract is consistent.
    """
    raw = Path(from_file).read_text(encoding="utf-8") if from_file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Error: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Error: plan payload must be a JSON object")
    return payload


def _steps_from_raw(raw_steps: list[Any]) -> list[PlanStep]:
    """Parse the ``steps`` array of a plan payload into a list of
    :class:`PlanStep` instances.  Mirrors the MCP-side validation —
    emits SystemExit on the first malformed entry so the CLI fails
    loud and fast.
    """
    from lhgp.contracts.plan import PlanStep

    if not isinstance(raw_steps, list) or not raw_steps:
        raise SystemExit("plan.steps must be a non-empty array")
    out: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise SystemExit(f"Error: steps[{index}] must be an object")
        try:
            out.append(
                PlanStep(
                    step_id=int(raw_step.get("step_id", index)),
                    action=str(raw_step.get("action") or ""),
                    target=str(raw_step.get("target") or ""),
                    rationale=str(raw_step.get("rationale") or ""),
                    expected_outcome=str(raw_step.get("expected_outcome") or ""),
                )
            )
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"Error: steps[{index}] is malformed: {exc}") from exc
    return out


def build_parser() -> argparse.ArgumentParser:
    executable = Path(sys.argv[0]).stem.lower()
    cli_name = "lhgp" if executable == "lhgp" else "longtask"
    parser = argparse.ArgumentParser(
        prog=cli_name,
        description=f"远期任务协议控制面 CLI (v{__version__}, protocol v{PROTOCOL_VERSION})",
    )
    parser.add_argument("--version", action="store_true", help="打印包与协议版本")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="覆盖默认数据存储目录 (~/.lhgp；未迁移旧安装回退到 ~/.longtask)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：仅打印 RPC 请求参数，不写库",
    )

    sub = parser.add_subparsers(dest="command")

    # doctor
    sub.add_parser("doctor", help="运行系统自检（解释器、存储、数据库、注册表、熔断开关）")

    # status / start / stop
    sub.add_parser("status", help="查看守护进程与全局熔断状态")
    start_p = sub.add_parser("start", help="启动 lhgpd 调度守护进程（兼容别名 longtaskd）")
    start_p.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_TICK_INTERVAL_SECONDS,
        help="调度扫描间隔秒数（默认 60）",
    )
    sub.add_parser("stop", help="停止 lhgpd 调度守护进程（兼容别名 longtaskd）")
    rpc_p = sub.add_parser("rpc-call", help="通过 daemon 本机 socket 调用 JSON-RPC 方法")
    rpc_p.add_argument("method", type=str, help="方法名，例如 attempt/status")
    rpc_p.add_argument("--params", default="{}", help="JSON 参数对象")
    rpc_p.add_argument(
        "--params-b64",
        default=None,
        help="URL-safe base64 编码的 JSON 参数（计划任务动作使用，避免 Windows quoting）",
    )
    rpc_p.add_argument("--request-id", default=None, help="幂等请求 ID（默认自动生成）")
    rpc_p.add_argument("--client-id", default="longtask-cli", help="客户端标识")
    # P6：数据目录迁移——安全默认：不带 --execute 只打印计划不动数据
    migrate_p = sub.add_parser(
        "migrate",
        help="迁移数据目录 ~/.longtask → ~/.lhgp（默认 dry-run；--execute 才真跑）",
    )
    migrate_p.add_argument(
        "--execute",
        action="store_true",
        help="真跑迁移（备份 + 拷贝式可回滚）；不带此标志只打印计划",
    )
    # 内部命令：常驻主循环入口，仅由 start 以分离进程方式调用
    daemonrun_p = sub.add_parser("_daemon-run", help=argparse.SUPPRESS)
    daemonrun_p.add_argument("--interval", type=float, default=DEFAULT_TICK_INTERVAL_SECONDS)

    # prepare
    prep_p = sub.add_parser("prepare", help="起草新远期任务合同（drafted）")
    prep_p.add_argument("--file", type=str, help="从 JSON/YAML 合同草稿文件读取")
    prep_p.add_argument(
        "--contract-id",
        type=str,
        default=None,
        help="自定义合同 ID（可选，格式：lt-YYYYMMDD-名称）",
    )
    prep_p.add_argument("--title", type=str, help="合同标题")
    prep_p.add_argument("--objective", type=str, help="完成标准描述（冻结区）")
    prep_p.add_argument("--deadline", type=str, help="截止墙钟时间 (ISO 8601)")
    prep_p.add_argument("--workload-hours", type=float, default=4.0, help="预估工时（小时）")

    # approve
    app_p = sub.add_parser("approve", help="批准合同进入激活状态（drafted -> active）")
    app_p.add_argument("contract_id", type=str, help="合同 ID")
    app_p.add_argument("--revision", type=int, default=None, help="期望版本号 (CAS)")

    # get
    get_p = sub.add_parser("get", help="查看指定合同当前状态与详情")
    get_p.add_argument("contract_id", type=str, help="合同 ID")
    get_p.add_argument("--decision-limit", type=int, default=50, help="决策历史返回上限（1-200）")
    get_p.add_argument(
        "--attempt-limit", type=int, default=20, help="attempt 历史返回上限（1-100）"
    )

    # list
    list_p = sub.add_parser("list", help="列出合同列表")
    list_p.add_argument(
        "--state",
        type=str,
        default=None,
        help="按状态过滤 (drafted/active/paused/blocked/complete/expired/cancelled)",
    )
    list_p.add_argument("--limit", type=int, default=20, help="返回数量上限")
    list_p.add_argument("--cursor", type=str, default=None, help="分页游标")
    list_p.add_argument(
        "--verbose",
        action="store_true",
        help="显示 u（紧迫度）、blocked_reason、ETA 等附加字段",
    )
    list_p.add_argument(
        "--min-u",
        type=float,
        default=None,
        help="按紧迫度下界过滤（仅 u>=min-u 显示）",
    )

    notif_p = sub.add_parser("notifications", help="查看通知 outbox（只读审计）")
    notif_p.add_argument(
        "--status", choices=["pending", "leased", "sent"], default=None, help="按投递状态过滤"
    )
    notif_p.add_argument("--goal-id", default=None, help="按目标/合同 ID 过滤")
    notif_p.add_argument("--limit", type=int, default=50, help="返回数量上限（1-200）")
    notif_p.add_argument(
        "--include-payload", action="store_true", help="显示通知 payload（默认隐藏敏感内容）"
    )

    # patch
    patch_p = sub.add_parser("patch", help="修订合同可变字段（soft_guidance/acceptance/workload）")
    patch_p.add_argument("contract_id", type=str, help="合同 ID")
    patch_p.add_argument("--revision", type=int, required=True, help="期望当前版本号 (CAS 强制)")
    patch_p.add_argument("--guidance", type=str, default=None, help="软指引内容（JSON 字符串）")
    patch_p.add_argument("--workload-hours", type=float, default=None, help="修正后的剩余工时")

    # pause / resume / cancel
    pause_p = sub.add_parser("pause", help="暂停运行中的合同 (active -> paused)")
    pause_p.add_argument("contract_id", type=str, help="合同 ID")

    resume_p = sub.add_parser("resume", help="恢复暂停或阻塞的合同 (paused/blocked -> active)")
    resume_p.add_argument("contract_id", type=str, help="合同 ID")

    cancel_p = sub.add_parser("cancel", help="终止合同 (-> cancelled)")
    cancel_p.add_argument("contract_id", type=str, help="合同 ID")
    cancel_p.add_argument("--reason", type=str, default="user cancelled via CLI", help="终止原因")

    # arbitrate
    arb_p = sub.add_parser("arbitrate", help="对 expired/blocked 合同执行人工裁决")
    arb_p.add_argument("contract_id", type=str, help="合同 ID")
    arb_p.add_argument(
        "--decision",
        type=str,
        required=True,
        choices=["complete", "archived", "active"],
        help="裁决目标状态",
    )
    arb_p.add_argument("--note", type=str, default=None, help="裁决附注说明")

    # request-verification
    verify_p = sub.add_parser(
        "request-verification",
        help="请求仅验收当前交付物（不再派 executor）",
    )
    verify_p.add_argument("contract_id", type=str, help="合同 ID")
    verify_p.add_argument(
        "--reason",
        type=str,
        default="user requested verification of the current delivery",
        help="请求原因（写入审计事件）",
    )

    # attempt resume: assemble a self-contained brief from active.md + handover.md
    attempt_p = sub.add_parser("attempt", help="attempt-level control surface")
    attempt_sub = attempt_p.add_subparsers(dest="attempt_cmd", required=True)
    attempt_resume = attempt_sub.add_parser(
        "resume",
        help="read active.md + handover.md, print a self-contained resume brief",
    )
    attempt_resume.add_argument("contract_id", type=str, help="合同 ID")
    attempt_resume.add_argument("attempt_id", type=str, help="正在恢复的 attempt ID")
    attempt_resume.add_argument(
        "--next-attempt-id",
        type=str,
        default=None,
        help="新 attempt 的 ID（默认从 attempt_id + 时间派生）",
    )

    # kill-switch
    ks_p = sub.add_parser("kill-switch", help="全局 Emergency Stop 熔断控制")
    ks_group = ks_p.add_mutually_exclusive_group(required=True)
    ks_group.add_argument("--activate", action="store_true", help="立即激活全局熔断，停止一切派工")
    ks_group.add_argument("--deactivate", action="store_true", help="解除全局熔断")
    ks_group.add_argument("--check", action="store_true", help="查看熔断开关状态")

    # rebuild
    reb_p = sub.add_parser("rebuild", help="从权威库事件与状态强制重建文件投影")
    reb_p.add_argument("contract_id", type=str, help="合同 ID")
    reb_p.add_argument("--revert", action="store_true", help="丢弃盘上草稿改动以库为准强制回滚")

    watch_p = sub.add_parser(
        "watch", help="事件流 tail（只读；可过滤 contract/executor/kinds，支持 --follow）"
    )
    watch_p.add_argument("--contract", type=str, default=None)
    watch_p.add_argument("--executor", type=str, default=None)
    watch_p.add_argument("--since", type=int, default=None)
    watch_p.add_argument("--kinds", type=str, default=None)
    watch_p.add_argument("--for", type=int, default=None, dest="duration")
    watch_p.add_argument("--follow", action="store_true")

    # messaging：agent 通信层
    msg_p = sub.add_parser("message", help="发送结构化消息给合同上的 agent")
    msg_p.add_argument("contract_id", type=str)
    msg_p.add_argument("text", type=str, help="消息内容")
    msg_p.add_argument("--kind", choices=["directive", "context", "question"], default="context")
    msg_p.add_argument("--to", type=str, default=None, help="指定接收 agent（可选）")
    inbox_p = sub.add_parser("inbox", help="查看合同的未读消息和用户决策")
    inbox_p.add_argument("contract_id", type=str)
    directive_p = sub.add_parser("direct", help="用户向 agent 发送指令（directive 消息）")
    directive_p.add_argument("contract_id", type=str)
    directive_p.add_argument("text", type=str, help="指令内容")

    # timeline：合同事件时间轴 HTML
    tl_p = sub.add_parser("timeline", help="渲染合同事件时间轴为自包含 HTML")
    tl_p.add_argument("contract_id", type=str)
    tl_p.add_argument(
        "--out", type=str, default=None, help="输出 HTML 路径（默认 stdout 不含 HTML）"
    )

    # validate：合同草稿预检
    val_p = sub.add_parser("validate", help="prepare 前预检合同草稿 JSON")
    val_p.add_argument("draft_file", type=str)
    val_p.add_argument(
        "--workspace", type=str, default=None, help="workspace_root（用于检查验收命令 target）"
    )

    # forecast：deadline 风险快照人话版
    fc_p = sub.add_parser("forecast", help="deadline 风险快照（人话版）")
    fc_p.add_argument("contract_id", type=str)

    # templates：内置合同模板
    tpl_p = sub.add_parser("template", help="内置合同模板（list/show/use）")
    tpl_sub = tpl_p.add_subparsers(dest="template_cmd")
    tpl_sub.add_parser("list", help="列出内置模板")
    tpl_show = tpl_sub.add_parser("show", help="打印模板内容")
    tpl_show.add_argument("name", type=str)
    tpl_use = tpl_sub.add_parser("use", help="导出模板为可编辑的 prepare 草稿")
    tpl_use.add_argument("name", type=str)
    tpl_use.add_argument("--out", type=str, required=True, help="输出 JSON 路径")

    # ── P6：反馈回路（submit-evaluation / compute-diff） ──
    fb_p = sub.add_parser("feedback", help="P6 反馈回路：记录用户评价、计算产物 diff")
    fb_sub = fb_p.add_subparsers(dest="feedback_cmd", required=True)
    fb_eval = fb_sub.add_parser("submit", help="对一份合同记录用户评价（1-5 + verdict）")
    fb_eval.add_argument("contract_id", type=str)
    fb_eval.add_argument("--rating", type=int, required=True, choices=[1, 2, 3, 4, 5])
    fb_eval.add_argument("--verdict", required=True, choices=["accept", "partial", "reject"])
    fb_eval.add_argument("--revision", type=int, default=1)
    fb_eval.add_argument("--attempt-id", type=str, default=None)
    fb_eval.add_argument("--evaluator", type=str, default="user")
    fb_eval.add_argument("--comments", type=str, default="")
    fb_diff = fb_sub.add_parser("diff", help="计算 attempt 报告产物 vs 当前 workspace 的 diff")
    fb_diff.add_argument("contract_id", type=str)
    fb_diff.add_argument("--revision", type=int, default=1)
    fb_diff.add_argument("--attempt-id", type=str, default=None)
    fb_diff.add_argument("--workspace", type=str, default=None)

    # ── P6：提升闭环（evolve-templates） ──
    ev_p = sub.add_parser(
        "evolve-templates",
        help="P6: mine user_evaluations, write to templates/",
    )
    ev_p.add_argument("--min-rating", type=int, default=4, choices=[1, 2, 3, 4, 5])
    ev_p.add_argument("--quality-threshold", type=float, default=0.7)
    ev_p.add_argument("--limit", type=int, default=50)

    # ── P6：多合同组合（portfolio） ──
    pf_p = sub.add_parser("portfolio", help="P6 多合同一屏：按 lifecycle/deadline/acceptance 聚合")
    pf_p.add_argument("--include-terminal", action="store_true", default=True)
    pf_p.add_argument("--exclude-terminal", dest="include_terminal", action="store_false")
    pf_p.add_argument("--limit", type=int, default=500)

    # ── P6：trace（单合同完整时间轴） ──
    tr_p = sub.add_parser(
        "trace",
        help="P6: contract timeline + eval + diff",
    )
    tr_p.add_argument("contract_id", type=str)
    tr_p.add_argument("--revision", type=int, default=None)
    tr_p.add_argument("--limit", type=int, default=500)

    # ── P6：deadline 多级升级报告 ──
    dl_p = sub.add_parser(
        "deadline-report",
        help="P6: scan active contracts, output actions",
    )
    dl_p.add_argument("--limit", type=int, default=500)

    # ── Plan-mode gate：合同 attempt 派工前先提交结构化计划 ──
    plan_p = sub.add_parser(
        "plan",
        help="plan-mode gate: submit a Plan for a contract attempt",
    )
    plan_sub = plan_p.add_subparsers(dest="plan_cmd", required=True)
    plan_submit = plan_sub.add_parser(
        "submit", help="read a Plan JSON from stdin, validate, write audit event"
    )
    plan_submit.add_argument("contract_id", type=str, help="target contract ID")
    plan_submit.add_argument(
        "--submitted-by",
        type=str,
        default="agent:cli",
        help="submitter identity (default 'agent:cli')",
    )
    plan_submit.add_argument(
        "--from",
        dest="from_file",
        type=str,
        default=None,
        help="read plan JSON from this file (default: stdin)",
    )

    # 3rd-round review: explicit user sign-off for plans that the
    # auto-approve scope rejected with ``requires_signoff=True``.
    # Re-runs the validator and writes PLAN_APPROVED on success.
    plan_signoff = plan_sub.add_parser(
        "signoff",
        help="user sign-off for an out-of-scope plan (reads plan JSON from stdin)",
    )
    plan_signoff.add_argument("contract_id", type=str, help="target contract ID")
    plan_signoff.add_argument(
        "--signoff-by",
        type=str,
        default="user:cli",
        help="signer identity (default 'user:cli')",
    )
    plan_signoff.add_argument("--note", type=str, default=None, help="optional audit note")
    plan_signoff.add_argument(
        "--from",
        dest="from_file",
        type=str,
        default=None,
        help="read plan JSON from this file (default: stdin)",
    )

    # ── P6 后续：wiki ── 工作方法 wiki
    wiki_p = sub.add_parser(
        "wiki",
        help="protocol-internal wiki: playbook / case-study / glossary",
    )
    wiki_sub = wiki_p.add_subparsers(dest="wiki_cmd", required=True)
    wiki_sub.add_parser("list", help="list all wiki pages")
    wiki_read = wiki_sub.add_parser("read", help="print a single page")
    wiki_read.add_argument("page", type=str, help="page path or stem (e.g. glossary)")
    wiki_search = wiki_sub.add_parser("search", help="search pages by keyword")
    wiki_search.add_argument("keyword", type=str)
    wiki_search.add_argument("--type", type=str, default=None)
    wiki_graph = wiki_sub.add_parser("show-graph", help="print outgoing + backlinks for one page")
    wiki_graph.add_argument("page", type=str)
    wiki_publish = wiki_sub.add_parser(
        "publish",
        help="render Markdown pages for active contracts under <wiki_root>/auto/",
    )
    wiki_publish.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="wiki root (default: <repo>/docs/wiki)",
    )

    # ── P6+1 / memory-and-wiki Phase 2：memory 长期记忆
    from lhgp.memory import MemoryKind, MemoryScope

    mem_p = sub.add_parser(
        "memory",
        help="long-term protocol memory (system-mined / human-curated)",
    )
    mem_sub = mem_p.add_subparsers(dest="memory_cmd", required=True)
    mem_add = mem_sub.add_parser("add", help="record a new memory")
    mem_add.add_argument("--title", required=True, type=str)
    mem_add.add_argument("--body", type=str, default=None, help="markdown; reads stdin if omitted")
    mem_add.add_argument(
        "--scope",
        choices=[s.value for s in MemoryScope],
        default=MemoryScope.PROJECT.value,
    )
    mem_add.add_argument(
        "--kind",
        choices=[k.value for k in MemoryKind],
        default=MemoryKind.PATTERN.value,
    )
    mem_add.add_argument("--tags", type=str, default="", help="comma-separated")
    mem_add.add_argument("--score", type=float, default=0.5)
    mem_add.add_argument("--source-contract", type=str, default=None)
    mem_add.add_argument("--source-event-id", type=int, default=None)
    mem_add.add_argument("--actor", type=str, default="user")
    mem_add.add_argument("--expires-in-days", type=int, default=180)
    mem_add.add_argument("--no-expire", action="store_true")
    mem_list = mem_sub.add_parser("list", help="list memories (score-desc)")
    mem_list.add_argument("--scope", type=str, default=None)
    mem_list.add_argument("--kind", type=str, default=None)
    mem_list.add_argument("--tags-any", type=str, default=None, help="comma-separated")
    mem_list.add_argument("--include-expired", action="store_true")
    mem_list.add_argument("--limit", type=int, default=50)
    mem_list.add_argument("--json", action="store_true")
    mem_search = mem_sub.add_parser("search", help="full-text search")
    mem_search.add_argument("keyword", type=str)
    mem_search.add_argument("--scope", type=str, default=None)
    mem_search.add_argument("--limit", type=int, default=20)
    mem_search.add_argument("--json", action="store_true")
    mem_show = mem_sub.add_parser("show", help="show one memory by id")
    mem_show.add_argument("--id", type=int, required=True)
    mem_sub.add_parser("expire", help="delete expired memories")

    # ── P6+1 / memory-and-wiki Phase 3：flowgen 代码/手写流程图
    flow_p = sub.add_parser(
        "flow",
        help="flow graphs: AST→Mermaid/Excalidraw, or read a wiki '## flow' section",
    )
    flow_sub = flow_p.add_subparsers(dest="flow_cmd", required=True)
    flow_ast = flow_sub.add_parser("ast", help="walk a Python file and emit a diagram")
    flow_ast.add_argument("file", type=str, help="path to a .py file")
    flow_ast.add_argument(
        "--module", type=str, default=None, help="module label (default: file stem)"
    )
    flow_ast.add_argument(
        "--format",
        choices=("mermaid", "excalidraw"),
        default="mermaid",
    )
    flow_wiki = flow_sub.add_parser("wiki", help="print the '## flow' section of a wiki page")
    flow_wiki.add_argument("page", type=str, help="page path or stem (e.g. topics/auto-mine)")
    flow_wiki.add_argument("--wiki-root", type=str, default=None)
    flow_lfp = flow_sub.add_parser(
        "list-flow-pages", help="list wiki pages that contain a '## flow' section"
    )
    flow_lfp.add_argument("--wiki-root", type=str, default=None)
    flow_contract = flow_sub.add_parser(
        "contract",
        help="walk the Python source files referenced by a contract",
    )
    flow_contract.add_argument("contract_id", type=str, help="contract id to walk")
    flow_contract.add_argument(
        "--state-db",
        type=str,
        default=None,
        help="override the state.db path (default: <data-dir>/state.db)",
    )
    flow_contract.add_argument(
        "--src-root",
        type=str,
        default=None,
        help="override the source root (default: <repo>/src)",
    )
    flow_contract.add_argument(
        "--format",
        choices=("mermaid", "excalidraw"),
        default="mermaid",
    )

    # insights：接手包 / 看板 / 成本台账
    brief_p = sub.add_parser("brief", help="接手包：一份合同的状态/风险/最近失败/下一步")
    brief_p.add_argument("contract_id", type=str)
    board_p = sub.add_parser("board", help="多合同一屏：按风险排序的状态表")
    board_p.add_argument("--include-terminal", action="store_true")
    board_p.add_argument("--limit", type=int, default=200)
    stats_p = sub.add_parser("stats", help="成本台账：attempt 分布与实际墙钟")
    stats_p.add_argument("contract_id", type=str, nargs="?")
    diff_p = sub.add_parser("diff", help="两个修订快照的字段级差异")
    diff_p.add_argument("contract_id", type=str)
    diff_p.add_argument("--from", dest="from_rev", type=int, required=True)
    diff_p.add_argument("--to", dest="to_rev", type=int, required=True)
    prop_p = sub.add_parser("proposals", help="列出 Goal 的计划修订提案（只读）")
    prop_p.add_argument("goal_id", type=str)
    prune_p = sub.add_parser("prune-events", help="清理终态合同的过期事件")
    prune_p.add_argument("--keep-days", type=int, default=90)
    prune_p.add_argument("--execute", action="store_true", help="默认 dry-run，加此参数才真删")

    # executor
    exec_p = sub.add_parser("executor", help="执行器资源池管理")
    exec_sub = exec_p.add_subparsers(dest="executor_cmd")
    exec_list = exec_sub.add_parser("list", help="列出已登记执行器")
    exec_list.add_argument("--enabled-only", action="store_true", help="仅显示已启用执行器")

    exec_en = exec_sub.add_parser("enable", help="启用指定执行器进入分发池")
    exec_en.add_argument("executor_id", type=str, help="执行器 ID")

    exec_dis = exec_sub.add_parser("disable", help="禁用指定执行器")
    exec_dis.add_argument("executor_id", type=str, help="执行器 ID")

    exec_h = exec_sub.add_parser("health", help="检查执行器健康与配置")
    exec_h.add_argument("executor_id", type=str, help="执行器 ID")

    return parser


def _dispatch_rpc(
    method: Method,
    params: dict[str, Any],
    *,
    data_dir: Path,
    dry_run: bool = False,
    now: datetime | None = None,
) -> int:
    """包装 CLI 向本机 RPC 服务端发送请求。"""
    envelope = RequestEnvelope(
        method=method,
        request_id=f"cli-req-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
        client_id="longtask-cli",
        protocol_version=PROTOCOL_VERSION,
        params=params,
    )

    if dry_run:
        print("[dry-run] simulated RPC request:")
        print(f"  method:     {envelope.method.value}")
        print(f"  request_id: {envelope.request_id}")
        print(f"  params:     {json.dumps(envelope.params, ensure_ascii=False, indent=2)}")
        return 0

    db_path = data_dir / "state.db"
    reg_path = data_dir / "registry.json"
    conn = connect(StoreConfig(db_path=db_path))
    try:
        ensure_schema(conn)
        registry = ExecutorRegistry.load_from_file(reg_path)
        resp = route(envelope, conn=conn, now=now, registry=registry)
        if resp.get("ok"):
            print(json.dumps(resp["result"], ensure_ascii=False, indent=2))
            return 0
        print(f"Error: {resp}", file=sys.stderr)
        return 1
    except RpcError as exc:
        print(f"Error [{exc.code.value}]: {exc.message}", file=sys.stderr)
        if exc.details:
            print(f"Details: {json.dumps(exc.details, ensure_ascii=False)}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    harden_stdio()
    if Path(sys.argv[0]).stem.lower() == "longtask":
        print(
            "warning: 'longtask' is deprecated; use 'lhgp' instead",
            file=sys.stderr,
        )
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        executable = Path(sys.argv[0]).stem.lower()
        cli_name = "lhgp" if executable == "lhgp" else "longtask"
        print(f"{cli_name} {__version__} (protocol v{PROTOCOL_VERSION})")
        return 0

    if not args.command:
        parser.print_help()
        return 0

    root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
    root.mkdir(parents=True, exist_ok=True)
    dry_run = bool(args.dry_run)

    # P6：数据目录迁移（安全默认：不带 --execute 只 dry-run）
    if args.command == "migrate":
        execute = bool(getattr(args, "execute", False))
        plan = migrate_data_dir(dry_run=not execute)
        print(plan.format_text())
        return 1 if any("FAILED" in s for s in plan.skipped) else 0

    # 1. doctor
    if args.command == "doctor":
        report = run_doctor(root)
        print(report.format_text())
        return 0 if report.all_ok else 1

    # 2. status / start / stop
    if args.command == "status":
        status = get_daemon_status(root)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    if args.command == "start":
        res = spawn_daemon(root, interval_seconds=args.interval)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if args.command == "stop":
        res = halt_daemon(root)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if args.command == "rpc-call":
        try:
            if args.params_b64 is not None:
                encoded = str(args.params_b64)
                encoded += "=" * (-len(encoded) % 4)
                rpc_params = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
            else:
                rpc_params = json.loads(args.params)
            if not isinstance(rpc_params, dict):
                raise ValueError("--params must be a JSON object")
            token_path = root / "daemon.token"
            token = token_path.read_text(encoding="utf-8").strip()
            response = call_unix_socket(
                rpc_socket_path(root),
                token=token,
                method=args.method,
                request_id=args.request_id
                or f"cli-req-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
                client_id=args.client_id,
                params=rpc_params,
            )
            print(json.dumps(response.get("result", response), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, json.JSONDecodeError, RpcError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "_daemon-run":
        from longtask.scheduler.wakeup import default_schedule_port

        res = run_daemon_loop(
            root,
            interval_seconds=args.interval,
            emit_fn=print,
            schedule_port=default_schedule_port(root),
        )
        print(json.dumps(res, ensure_ascii=False))
        return 0

    # 3. kill-switch
    if args.command == "kill-switch":
        if args.activate:
            set_kill_switch(root, True)
            print("[kill-switch] ACTIVE: all dispatches halted.")
            return 0
        if args.deactivate:
            set_kill_switch(root, False)
            print("[kill-switch] inactive: normal operations resumed.")
            return 0
        if args.check:
            active = is_kill_switch_active(root)
            print(f"[kill-switch] status: {'ACTIVE (halted)' if active else 'inactive'}")
            return 0

    # 4. rebuild
    if args.command == "rebuild":
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            ensure_schema(conn)
            if args.revert:
                paths = revert_projection(root, args.contract_id, conn)
                print(f"[rebuild] reverted from database ({len(paths)} files materialized).")
            else:
                paths = rebuild_projection(root, args.contract_id, conn)
                print(f"[rebuild] projections materialized: {list(paths.keys())}")
            return 0
        finally:
            conn.close()

    if args.command == "message":
        from lhgp.persistence.messages import send_message

        root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            event_id = send_message(
                conn,
                contract_id=args.contract_id,
                from_actor="user",
                kind=args.kind,
                text=args.text,
                now=datetime.now(UTC),
                to_agent=args.to,
            )
            print(f"message sent: event_id={event_id} kind={args.kind}")
        finally:
            conn.close()
        return 0

    if args.command == "direct":
        from lhgp.persistence.messages import send_message

        root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            event_id = send_message(
                conn,
                contract_id=args.contract_id,
                from_actor="user",
                kind="directive",
                text=args.text,
                now=datetime.now(UTC),
            )
            print(f"directive sent: event_id={event_id}")
            print("agent will see this on next context compile")
        finally:
            conn.close()
        return 0

    if args.command == "inbox":
        from lhgp.persistence.messages import get_messages

        root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            msgs = get_messages(conn, contract_id=args.contract_id)
            directives = [m for m in msgs if m["kind"] == "directive"]
            questions = [m for m in msgs if m["kind"] == "question"]
            context_msgs = [m for m in msgs if m["kind"] == "context"]
        finally:
            conn.close()
        if directives:
            print("── Directives (user → agent) ──")
            for m in directives:
                print(f"  [{m['at']}] {m['text']}")
        if questions:
            print("── Questions (agent → user) ──")
            for m in questions:
                print(f"  [{m['at']}] {m['text']}")
        if context_msgs:
            print("── Context notes ──")
            for m in context_msgs[-5:]:
                print(f"  [{m['at']}] {m['text']}")
        if not msgs:
            print("(no messages)")
        return 0

    if args.command == "timeline":
        from lhgp.persistence.timeline import build_timeline_html

        conn = _open_read_conn(args.data_dir)
        try:
            page, error = build_timeline_html(
                conn, contract_id=args.contract_id, now=datetime.now(UTC)
            )
        finally:
            conn.close()
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if args.out:
            Path(args.out).write_text(page, encoding="utf-8")
            print(f"timeline written: {args.out}")
        else:
            print(page)
        return 0

    if args.command == "validate":
        from lhgp.templates.validate import validate_draft_file

        workspace = Path(args.workspace).resolve() if args.workspace else None
        problems = validate_draft_file(Path(args.draft_file), workspace_root=workspace)
        if problems:
            print(f"FOUND {len(problems)} problem(s):")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("OK: no problems found")
        return 0

    if args.command == "forecast":
        from lhgp.persistence.insights import build_brief

        conn = _open_read_conn(args.data_dir)
        try:
            brief = build_brief(conn, contract_id=args.contract_id, now=datetime.now(UTC))
        finally:
            conn.close()
        if not brief.get("found"):
            print(f"error: contract {args.contract_id!r} not found", file=sys.stderr)
            return 1
        risk = brief.get("risk", {})
        risk_level = risk.get("risk", "unknown")
        slack = risk.get("slack_p50_minutes")
        deadline_status = brief.get("deadline_status", "unknown")
        state = brief.get("state", "unknown")
        lines = [
            f"contract:  {args.contract_id}",
            f"state:     {state}",
            f"deadline:  {brief.get('deadline_at')}  ({deadline_status})",
            f"risk:      {risk_level} (confidence: {risk.get('confidence', 'unknown')})",
        ]
        if slack is not None:
            hours = float(slack) / 60.0
            direction = "ahead" if hours >= 0 else "OVERDUE by"
            lines.append(f"slack:     {abs(hours):.1f}h {direction} deadline (p50)")
        blocked = brief.get("blocked_reason")
        if blocked:
            lines.append(f"blocked:   {blocked}")
        latest = brief.get("latest_attempt")
        if latest and latest.get("state") == "failed":
            lines.append(f"last failure: {latest.get('error_class', 'unknown')}")
        print("\n".join(lines))
        return 0

    if args.command == "template":
        from pathlib import Path as _Path

        from lhgp import templates as _tpl

        cmd = getattr(args, "template_cmd", "list")
        if cmd == "list":
            for name in _tpl.available():
                print(name)
            return 0
        if cmd == "show":
            print(_tpl.load(args.name))
            return 0
        if cmd == "use":
            out = _Path(args.out)
            if out.exists():
                print(f"refusing to overwrite existing file: {out}", file=sys.stderr)
                return 1
            out.write_text(_tpl.load(args.name), encoding="utf-8")
            print(f"wrote {out} - replace <placeholders>, then: lhgp prepare --file {out}")
            return 0
        return 1

    # ── P6：反馈 / 提升 / 多合同 / trace / deadline-report ──
    if args.command == "feedback":
        from pathlib import Path as _Path

        from lhgp.feedback import (
            EvaluationRating,
            EvaluationVerdict,
            UserEvaluation,
            compute_acceptance_diff,
            record_diff,
            record_evaluation,
        )
        from lhgp.persistence.events import EventType
        from lhgp.persistence.events_query import append_event
        from lhgp.persistence.schema import transaction as _tx

        conn = _open_read_conn(args.data_dir)
        try:
            if args.feedback_cmd == "submit":
                evaluation = UserEvaluation(
                    contract_id=args.contract_id,
                    contract_revision=int(args.revision or 1),
                    attempt_id=args.attempt_id,
                    evaluator=str(args.evaluator or "user"),
                    rating=EvaluationRating(str(args.rating)),
                    verdict=EvaluationVerdict(str(args.verdict)),
                    comments=str(args.comments or ""),
                )
                with _tx(conn):
                    eid = record_evaluation(conn, evaluation)
                    append_event(
                        conn,
                        contract_id=args.contract_id,
                        event_type=EventType.USER_EVALUATION_SUBMITTED,
                        payload={
                            "evaluation_id": eid,
                            "rating": int(args.rating),
                            "verdict": str(args.verdict),
                        },
                        now=datetime.now(UTC),
                        contract_revision=int(args.revision or 1),
                        attempt_id=evaluation.attempt_id,
                        actor=evaluation.evaluator,
                    )
                print(
                    json.dumps(
                        {
                            "evaluation_id": eid,
                            "contract_id": args.contract_id,
                            "rating": int(args.rating),
                            "verdict": str(args.verdict),
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            if args.feedback_cmd == "diff":
                feedback_root: _Path | None = (
                    _Path(args.data_dir).expanduser().resolve() if args.data_dir else None
                )
                workspace = (
                    _Path(args.workspace).expanduser().resolve()
                    if args.workspace
                    else (feedback_root or _Path.cwd()) / "contracts" / args.contract_id
                )
                workspace.mkdir(parents=True, exist_ok=True)
                diff = compute_acceptance_diff(
                    args.contract_id,
                    int(args.revision or 1),
                    workspace,
                    attempt_id=args.attempt_id,
                )
                with _tx(conn):
                    did = record_diff(conn, diff)
                    append_event(
                        conn,
                        contract_id=args.contract_id,
                        event_type=EventType.ACCEPTANCE_DIFF_COMPUTED,
                        payload={
                            "diff_id": did,
                            "files_changed_count": len(diff.files_changed),
                            "summary": diff.summary,
                        },
                        now=datetime.now(UTC),
                        contract_revision=int(args.revision or 1),
                        attempt_id=args.attempt_id,
                    )
                print(
                    json.dumps(
                        {
                            "diff_id": did,
                            "summary": diff.summary,
                            "files_changed": diff.files_changed,
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
                return 0
        finally:
            conn.close()
        return 1

    if args.command == "evolve-templates":
        from lhgp.feedback.types import EvaluationRating
        from lhgp.learning import TemplateEvolver

        conn = _open_read_conn(args.data_dir)
        try:
            evolver = TemplateEvolver(
                min_rating=EvaluationRating(str(args.min_rating)),
                quality_threshold=float(args.quality_threshold),
            )
            result = evolver.run(conn, limit=int(args.limit))
        finally:
            conn.close()
        print(
            json.dumps(
                {
                    "written": [str(p) for p in result.written],
                    "skipped_low_quality": result.skipped_low_quality,
                    "skipped_existing": result.skipped_existing,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "portfolio":
        from lhgp.portfolio import portfolio_summary

        conn = _open_read_conn(args.data_dir)
        try:
            snap = portfolio_summary(
                conn,
                include_terminal=bool(args.include_terminal),
                limit=int(args.limit),
            )
        finally:
            conn.close()
        print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "trace":
        from lhgp.portfolio import trace_contract

        conn = _open_read_conn(args.data_dir)
        try:
            data = trace_contract(
                conn,
                args.contract_id,
                contract_revision=args.revision,
                limit=int(args.limit),
            )
        finally:
            conn.close()
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "deadline-report":
        from lhgp.enforcement import (
            DeadlineEnforcer,
            compute_deadline_level,
            format_deadline_report,
        )

        conn = _open_read_conn(args.data_dir)
        try:
            rows = conn.execute(
                "SELECT contract_id FROM contracts WHERE state IN "
                "('active','paused','blocked','drafted') "
                "ORDER BY updated_at DESC LIMIT ?",
                (int(args.limit),),
            ).fetchall()
            pairs = []
            now = datetime.now(UTC)
            for (cid,) in rows:
                view = get_contract(conn, cid)
                if view is None:
                    continue
                pairs.append((view, compute_deadline_level(view.draft.deadline_at, now=now)))
            actions = DeadlineEnforcer().enforce_all(pairs)
            deadline_report: dict[str, Any] = format_deadline_report(actions)
        finally:
            conn.close()
        print(json.dumps(deadline_report, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "plan":
        if args.plan_cmd == "submit":
            from lhgp.contracts.plan import Plan
            from lhgp.persistence.events import EventType
            from lhgp.persistence.events_query import append_event
            from lhgp.persistence.schema import transaction as _tx

            if args.from_file:
                raw = Path(args.from_file).read_text(encoding="utf-8")
            else:
                raw = sys.stdin.read()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"Error: invalid JSON: {exc}", file=sys.stderr)
                return 2
            if not isinstance(payload, dict):
                print("Error: plan payload must be a JSON object", file=sys.stderr)
                return 2
            steps = _steps_from_raw(payload.get("steps") or [])

            conn = _open_read_conn(args.data_dir)
            try:
                view = get_contract(conn, args.contract_id)
                if view is None:
                    print(f"Error: contract {args.contract_id} not found", file=sys.stderr)
                    return 1
                now = datetime.now(UTC)
                new_plan = Plan(
                    contract_id=args.contract_id,
                    steps=tuple(steps),
                    submitted_at=now,
                    submitted_by=str(args.submitted_by),
                )
                plan_validation = new_plan.validate(view)
                with _tx(conn):
                    append_event(
                        conn,
                        contract_id=args.contract_id,
                        event_type=EventType.PLAN_SUBMITTED,
                        payload={
                            "submitted_by": args.submitted_by,
                            "step_count": len(steps),
                            "step_ids": [s.step_id for s in steps],
                        },
                        now=now,
                        actor=args.submitted_by,
                    )
                    if plan_validation.approved and not plan_validation.requires_signoff:
                        from lhgp.contracts.plan import _extract_check_identifiers
                        from longtask.cli.dispatch import (
                            wake_blocked_after_plan_approval,
                        )

                        append_event(
                            conn,
                            contract_id=args.contract_id,
                            event_type=EventType.PLAN_APPROVED,
                            payload={
                                "submitted_by": args.submitted_by,
                                "step_count": len(steps),
                                "contract_revision": view.revision,
                                "content_hash": new_plan.content_hash,
                                "accepted_check_ids": list(_extract_check_identifiers(view)),
                                "auto_approved": True,
                            },
                            now=now,
                            actor="daemon",
                            contract_revision=view.revision,
                        )
                        # Same P1 review fix as tool_submit_plan: re-activate
                        # contracts that were BLOCKED(NO_EXECUTOR) waiting
                        # for this plan.
                        wake_blocked_after_plan_approval(conn, args.contract_id, now)
                    elif plan_validation.approved and plan_validation.requires_signoff:
                        # 3rd-round review: structurally valid but outside
                        # the contract's auto-approve scope.  Recorded as
                        # PLAN_REJECTED with requires_signoff; the user
                        # must run ``lhgp plan signoff`` to promote.
                        oos_actions = sorted(
                            {
                                s.action
                                for s in steps
                                if not view.draft.auto_approve.covers_action(s.action)
                            }
                        )
                        append_event(
                            conn,
                            contract_id=args.contract_id,
                            event_type=EventType.PLAN_REJECTED,
                            payload={
                                "submitted_by": args.submitted_by,
                                "step_count": len(steps),
                                "rejection_reasons": ["requires_signoff"],
                                "out_of_scope_actions": oos_actions,
                                "auto_approve_enabled": view.draft.auto_approve.enabled,
                            },
                            now=now,
                            actor="daemon",
                        )
                    else:
                        append_event(
                            conn,
                            contract_id=args.contract_id,
                            event_type=EventType.PLAN_REJECTED,
                            payload={
                                "submitted_by": args.submitted_by,
                                "rejection_reasons": list(plan_validation.rejection_reasons),
                            },
                            now=now,
                            actor="daemon",
                        )
            finally:
                conn.close()
            plan_result = {
                "contract_id": args.contract_id,
                "approved": plan_validation.approved,
                "requires_signoff": plan_validation.requires_signoff,
                "rejection_reasons": list(plan_validation.rejection_reasons),
                "step_count": len(steps),
                "submitted_at": now.isoformat(),
            }
            print(json.dumps(plan_result, ensure_ascii=False, indent=2))
            return 0 if plan_validation.approved else 1

        if args.plan_cmd == "signoff":
            # User sign-off path for a plan that lhgp_plan submit
            # rejected with requires_signoff.  Reads the same JSON
            # shape, re-validates, then writes PLAN_APPROVED on
            # success and wakes the contract.
            from lhgp.contracts.plan import Plan, _extract_check_identifiers
            from lhgp.persistence.events import EventType

            # 4th-round review (2026-09-08): append_event was
            # previously imported only inside the ``lhgp plan
            # submit`` branch; this branch hit UnboundLocalError
            # at runtime.  Import it here, not later, so the
            # call site is unambiguous.
            from lhgp.persistence.events_query import append_event

            payload = _read_plan_input(args.from_file)
            steps = _steps_from_raw(payload.get("steps") or [])

            conn = _open_read_conn(args.data_dir)
            try:
                view = get_contract(conn, args.contract_id)
                if view is None:
                    raise SystemExit(f"contract {args.contract_id} not found")
                now = datetime.now(UTC)
                plan_obj = Plan(
                    contract_id=args.contract_id,
                    steps=tuple(steps),
                    submitted_at=now,
                    submitted_by=args.signoff_by,
                )
                validation = plan_obj.validate(view)
                if not validation.approved:
                    print(
                        json.dumps(
                            {
                                "contract_id": args.contract_id,
                                "signed_off": False,
                                "rejection_reasons": list(validation.rejection_reasons),
                                "step_count": len(steps),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 1
                append_event(
                    conn,
                    contract_id=args.contract_id,
                    event_type=EventType.PLAN_APPROVED,
                    payload={
                        "signed_off_by": args.signoff_by,
                        "note": args.note,
                        "step_count": len(steps),
                        "contract_revision": view.revision,
                        "content_hash": plan_obj.content_hash,
                        "accepted_check_ids": list(_extract_check_identifiers(view)),
                        "spec_hash": view.draft.acceptance.spec_hash,
                        "auto_approved": False,
                    },
                    now=now,
                    actor=args.signoff_by,
                    contract_revision=view.revision,
                )
                from longtask.cli.dispatch import wake_blocked_after_plan_approval

                wake_blocked_after_plan_approval(conn, args.contract_id, now)
            finally:
                conn.close()
            print(
                json.dumps(
                    {
                        "contract_id": args.contract_id,
                        "signed_off": True,
                        "signed_off_by": args.signoff_by,
                        "signed_off_at": now.isoformat(),
                        "step_count": len(steps),
                        "content_hash": plan_obj.content_hash,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        parser.parse_args(["plan", "--help"])
        return 0

    if args.command == "brief":
        from lhgp.persistence.insights import build_brief

        conn = _open_read_conn(args.data_dir)
        try:
            brief = build_brief(conn, contract_id=args.contract_id, now=datetime.now(UTC))
        finally:
            conn.close()
        print(json.dumps(brief, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "wiki":
        from lhgp.wiki import REPO_ROOT, WIKI_ROOT, wiki_command

        if args.wiki_cmd == "publish":
            from lhgp.wiki import publish_active_contracts

            root = (
                Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
            )
            conn = connect(StoreConfig(db_path=root / "state.db"))
            try:
                ensure_schema(conn)
                wiki_root = (
                    Path(args.wiki_root).expanduser().resolve()
                    if getattr(args, "wiki_root", None)
                    else (REPO_ROOT / "docs" / "wiki" if REPO_ROOT else WIKI_ROOT)
                )
                written = publish_active_contracts(conn, wiki_root)
                if not written:
                    print("no active contracts")
                else:
                    print(f"published {len(written)} page(s) under {wiki_root / 'auto'}")
            finally:
                conn.close()
            return 0

        return wiki_command(args)

    if args.command == "memory":
        from lhgp.memory.cli import memory_command

        root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
        db_path = root / "state.db"
        conn = connect(StoreConfig(db_path=db_path))
        try:
            ensure_schema(conn)
            return memory_command(conn, args)
        finally:
            conn.close()

    if args.command == "flow":
        from lhgp.flow.cli import flow_command

        return flow_command(args)

    if args.command == "board":
        from lhgp.persistence.insights import build_board

        conn = _open_read_conn(args.data_dir)
        try:
            board_rows = build_board(
                conn,
                now=datetime.now(UTC),
                include_terminal=args.include_terminal,
                limit=args.limit,
            )
        finally:
            conn.close()
        if not board_rows:
            print("(no non-terminal contracts)")
            return 0
        header = f"{'CONTRACT':<28} {'STATE':<10} {'RISK':<8} {'DEADLINE_STATUS':<16} TITLE"
        print(header)
        for row in board_rows:
            print(
                f"{row['contract_id']:<28} {row['state']:<10} {row['risk']!s:<8}"
                f" {row['deadline_status']:<16} {row['title']}"
            )
        return 0

    if args.command == "stats":
        from lhgp.persistence.insights import build_stats

        conn = _open_read_conn(args.data_dir)
        try:
            stats = build_stats(conn, contract_id=args.contract_id or None)
        finally:
            conn.close()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    if args.command == "diff":
        from lhgp.persistence.maintenance import diff_revisions

        conn = _open_read_conn(args.data_dir)
        try:
            diff_result = diff_revisions(
                conn,
                contract_id=args.contract_id,
                from_revision=args.from_rev,
                to_revision=args.to_rev,
            )
        finally:
            conn.close()
        print(json.dumps(diff_result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "proposals":
        from lhgp.persistence.events_query import get_events

        root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            proposals = [
                {
                    "event_id": e.event_id,
                    "at": e.created_at.isoformat(),
                    "payload": json.loads(e.payload_json or "{}"),
                }
                for e in get_events(conn, contract_id=args.goal_id)
                if e.event_type == "goal/proposed"
            ]
        finally:
            conn.close()
        pending = [p for p in proposals if p["payload"].get("status") == "pending"]
        print(
            json.dumps(
                {
                    "goal_id": args.goal_id,
                    "total": len(proposals),
                    "pending": len(pending),
                    "proposals": proposals,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if args.command == "proposal-apply":
        # E3：提案审批落地——用户读取 pending 提案后以此命令批准；
        # 实际的 CAS 更新由 goal/update（user 通道）执行，提案事件
        # 标记为 applied 以避免重复应用。
        from lhgp.persistence.events_query import get_events
        from longtask.persistence.events import EventType
        from longtask.persistence.store import append_event, get_goal
        from longtask.rpc.handlers.goal import handle_goal_update

        root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            matching = [
                e
                for e in get_events(conn, contract_id=args.goal_id)
                if e.event_id == args.event_id and e.event_type == EventType.GOAL_PROPOSED.value
            ]
            if not matching:
                print(
                    f"error: proposal event {args.event_id} not found for goal {args.goal_id}",
                    file=sys.stderr,
                )
                return 1
            payload = json.loads(matching[0].payload_json or "{}")
            plan = payload.get("plan")
            if not isinstance(plan, dict):
                print("error: proposal has no plan object", file=sys.stderr)
                return 1
            # E3 校验：apply 时用同一套校验（提案和落地结构一致）
            from lhgp.promoter.proposals import validate_proposed_plan

            proposal_validation = validate_proposed_plan(plan)
            if not proposal_validation.ok:
                print(
                    f"error: plan validation failed: {'; '.join(proposal_validation.errors)}",
                    file=sys.stderr,
                )
                return 1
            goal_before = get_goal(conn, args.goal_id)
            if goal_before is None:
                print(f"error: goal {args.goal_id} not found", file=sys.stderr)
                return 1
            revision = goal_before["revision"]
            handle_goal_update(
                RequestEnvelope(
                    method=Method.GOAL_UPDATE,
                    request_id=f"proposal-apply-{args.event_id}",
                    client_id="longtask-cli",
                    protocol_version=2,
                    params={"goal_id": args.goal_id, "revision": revision, "plan": plan},
                ),
                conn=conn,
                now=datetime.now(UTC),
            )
            append_event(
                conn,
                contract_id=args.goal_id,
                goal_id=args.goal_id,
                event_type=EventType.GOAL_AMENDED,
                payload={
                    "source": "proposal",
                    "proposal_event_id": args.event_id,
                    "applied_by": "user",
                },
                now=datetime.now(UTC),
                actor="user",
            )
            print(f"proposal {args.event_id} applied to goal {args.goal_id}")
        finally:
            conn.close()
        return 0

    if args.command == "prune-events":
        from pathlib import Path as _Path

        from lhgp.persistence.maintenance import prune_terminal_events

        root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            prune_result = prune_terminal_events(
                conn, now=datetime.now(UTC), keep_days=args.keep_days, dry_run=not args.execute
            )
        finally:
            conn.close()
        print(json.dumps(prune_result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "watch":
        from longtask.cli.watch import main as _watch_main

        argv = []
        if getattr(args, "data_dir", None):
            # watch 曾丢弃 --data-dir，静默回退读默认目录（promo 实测发现）
            argv += ["--data-dir", str(args.data_dir)]
        if args.contract:
            argv += ["--contract", args.contract]
        if args.executor:
            argv += ["--executor", args.executor]
        if args.since is not None:
            argv += ["--since", str(args.since)]
        if args.kinds:
            argv += ["--kinds", args.kinds]
        if args.duration is not None:
            argv += ["--for", str(args.duration)]
        if args.follow:
            argv += ["--follow"]
        return _watch_main(argv)

    # 5. 合同生命周期命令
    if args.command == "prepare":
        draft_dict: dict[str, Any] = {}
        if args.file:
            f_path = Path(args.file)
            content = f_path.read_text(encoding="utf-8")
            draft_dict = json.loads(content)
        else:
            if not args.title or not args.objective or not args.deadline:
                msg = "Error: prepare requires --file or (--title, --objective, --deadline)"
                print(msg, file=sys.stderr)
                return 2
            draft_dict = {
                "title": args.title,
                "objective": args.objective,
                "deadline_at": args.deadline,
                "workload_initial_hours": args.workload_hours,
                "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
                "acceptance": {"standard": "验收标准通过", "checks": ["核对项 1"]},
                "budget": {
                    "max_dispatches": 5,
                    "max_escalations": 2,
                    "max_concurrent_attempts": 1,
                    "max_attempt_minutes": 60,
                    "max_output_bytes": 1048576,
                },
            }
        params: dict[str, Any] = {"draft": draft_dict}
        if args.contract_id:
            params["contract_id"] = args.contract_id
        return _dispatch_rpc(Method.CONTRACT_PREPARE, params, data_dir=root, dry_run=dry_run)

    if args.command == "approve":
        params = {"contract_id": args.contract_id}
        if args.revision is not None:
            params["expected_revision"] = args.revision
        return _dispatch_rpc(Method.CONTRACT_APPROVE, params, data_dir=root, dry_run=dry_run)

    if args.command == "get":
        return _dispatch_rpc(
            Method.CONTRACT_GET,
            {
                "contract_id": args.contract_id,
                "decision_limit": args.decision_limit,
                "attempt_limit": args.attempt_limit,
            },
            data_dir=root,
            dry_run=dry_run,
        )

    if args.command == "list":
        if not args.verbose:
            # 简版走 RPC（薄）：仅核心字段
            params = {"limit": args.limit}
            if args.state:
                params["state"] = args.state
            if args.cursor:
                params["cursor"] = args.cursor
            return _dispatch_rpc(Method.CONTRACT_LIST, params, data_dir=root, dry_run=dry_run)

        # 详细版：直读 store + 计算 u/ETA（避免污染协议输出）
        from longtask.cli.formatting import now_utc, render_contract_list_verbose

        conn = connect(StoreConfig(db_path=root / "state.db"))
        ensure_schema(conn)
        try:
            from longtask.contracts.schema import ContractState

            state_filter: str | ContractState | None = args.state
            if state_filter:
                try:
                    state_filter = ContractState(state_filter)
                except ValueError:
                    # 未知状态名：如实降级为字符串匹配（不崩、可能零结果）
                    print(
                        f"[list] unknown state '{args.state}'; filtering as raw string",
                        file=sys.stderr,
                    )
            contracts = list_contracts(
                conn,
                state=state_filter,
                after_contract_id=args.cursor,
                limit=args.limit,
            )
            output = render_contract_list_verbose(contracts, min_u=args.min_u, now=now_utc())
        finally:
            conn.close()
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "notifications":
        from longtask.persistence.notifications import list_notifications

        if not 1 <= args.limit <= 200:
            print("Error: --limit must be between 1 and 200", file=sys.stderr)
            return 1
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            ensure_schema(conn)
            rows = list_notifications(
                conn, status=args.status, goal_id=args.goal_id, limit=args.limit
            )
        finally:
            conn.close()
        notification_output: list[dict[str, Any]] = []
        for item in rows:
            record: dict[str, Any] = {
                "notification_id": item.notification_id,
                "idempotency_key": item.idempotency_key,
                "goal_id": item.goal_id,
                "event_type": item.event_type,
                "channel": item.channel,
                "status": item.status,
                "attempts": item.attempts,
                "available_at": item.available_at.isoformat(),
                "last_error": item.last_error,
            }
            if args.include_payload:
                record["payload"] = item.payload
            notification_output.append(record)
        print(json.dumps({"notifications": notification_output}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "patch":
        params = {"contract_id": args.contract_id, "expected_revision": args.revision}
        if args.guidance:
            params["soft_guidance"] = json.loads(args.guidance)
        if args.workload_hours is not None:
            params["workload_initial_hours"] = args.workload_hours
        return _dispatch_rpc(Method.CONTRACT_PATCH, params, data_dir=root, dry_run=dry_run)

    if args.command == "pause":
        return _dispatch_rpc(
            Method.CONTRACT_PAUSE,
            {"contract_id": args.contract_id},
            data_dir=root,
            dry_run=dry_run,
        )

    if args.command == "resume":
        return _dispatch_rpc(
            Method.CONTRACT_RESUME,
            {"contract_id": args.contract_id},
            data_dir=root,
            dry_run=dry_run,
        )

    if args.command == "cancel":
        return _dispatch_rpc(
            Method.CONTRACT_CANCEL,
            {"contract_id": args.contract_id, "reason": args.reason},
            data_dir=root,
            dry_run=dry_run,
        )

    if args.command == "arbitrate":
        return _dispatch_rpc(
            Method.CONTRACT_ARBITRATE,
            {"contract_id": args.contract_id, "decision": args.decision, "note": args.note},
            data_dir=root,
            dry_run=dry_run,
        )

    if args.command == "request-verification":
        return _dispatch_rpc(
            Method.CONTRACT_REQUEST_VERIFICATION,
            {"contract_id": args.contract_id, "reason": args.reason},
            data_dir=root,
            dry_run=dry_run,
        )

    if args.command == "attempt" and args.attempt_cmd == "resume":
        from lhgp.contracts import build_resume_brief

        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            ensure_schema(conn)
            resume_brief = build_resume_brief(
                root,
                args.contract_id,
                args.attempt_id,
                next_attempt_id=args.next_attempt_id,
                conn=conn,
                now=datetime.now(UTC),
                actor="user:cli",
            )
        finally:
            conn.close()
        print(resume_brief.body)
        return 0

    # 6. executor 命令
    if args.command == "executor":
        if args.executor_cmd == "list":
            return _dispatch_rpc(
                Method.EXECUTOR_LIST,
                {"enabled_only": args.enabled_only},
                data_dir=root,
                dry_run=dry_run,
            )
        if args.executor_cmd == "enable":
            return _dispatch_rpc(
                Method.EXECUTOR_ENABLE,
                {"executor_id": args.executor_id},
                data_dir=root,
                dry_run=dry_run,
            )
        if args.executor_cmd == "disable":
            return _dispatch_rpc(
                Method.EXECUTOR_DISABLE,
                {"executor_id": args.executor_id},
                data_dir=root,
                dry_run=dry_run,
            )
        if args.executor_cmd == "health":
            return _dispatch_rpc(
                Method.EXECUTOR_HEALTH,
                {"executor_id": args.executor_id},
                data_dir=root,
                dry_run=dry_run,
            )
        parser.parse_args(["executor", "--help"])
        return 0

    return 0


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
