"""通用 subprocess 适配器（DESIGN §9、§12.1、§15.2）。

适合只有 CLI、没有外部会话 API 的执行器。约束翻译遵循 §9 编译表：
编译失败的默认行为是拒接（PrepareRefusedError），绝不静默降级。
spawn 只收结构化 argv（列表参数、shell=False），模型输出是不可信数据，
永不进入命令行（DESIGN §12.1、§14 注入防线）。

任务文本位置（dogfood v4 教训：各 CLI 参数语法不同）：
- 位置参数型（cli-bridge headless）：argv 不含占位符 → task_prompt 追加为尾元素；
- flag 值型（如 `<cli> -p "<task>"`）：argv 含一个 {task} 占位符 → 原位替换。
「prompt 插在哪」由此成为注册表配置数据，不再需要每个 CLI 手写包装器。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

from lhgp.untrusted.sanitize import has_terminal_controls, sanitize_terminal_text
from longtask.adapters.base import (
    AttemptInput,
    ExecutorAdapter,
    PreparedLaunch,
    PrepareRefusedError,
)
from longtask.adapters.handles import (
    EXTERNAL_STATE_UNKNOWN,
    RECOVERY_REATTACH,
    ExternalRunHandle,
)
from longtask.adapters.manifest import ExecutorManifest
from longtask.adapters.processes import (
    identity_matches,
    process_alive,
    process_start_time,
    terminate_tree,
)
from longtask.adapters.registry import LaunchSpec
from longtask.contracts.schema import AttemptState, Enforcement

# cancel 宽限期默认值：terminate 后等这么久仍存活才升级 kill（不真实长睡）。
DEFAULT_GRACE_SECONDS = 5.0
# collect 默认等待上限：超时抛 subprocess.TimeoutExpired（如实回收失败）。
DEFAULT_COLLECT_TIMEOUT_SECONDS = 60.0
# 任务文本占位符（注册表 argv 内）：标记 task_prompt 的插入位置。
# 固定词表的一部分，不是模型可控数据；替换是单元素原位换，无拼接面。
TASK_PLACEHOLDER = "{task}"

# 重绑后能力诚实声明（SPEC §11.3 capability_snapshot）：管道与退出码随原
# Popen 丢失，只能观察存活；collect 会如实报错而不是编一个退出码。
_DETACHED_CAPABILITY = {
    "transport": "subprocess",
    "reattach": "pid+start-time",
    "observe": "liveness",
    "collect": "unavailable",
    "cancel": "terminate",
}


class _DetachedProcess:
    """重启后按持久句柄重新绑定的外部进程观察器（SPEC §11.3 reattach）。

    与 Popen 的区别（诚实声明，不掩饰）：
    - 存活可观察：每次都用「pid + 启动时间」双重比对，pid 被复用可检出；
    - 退出码不可得：非子进程，退出状态已随原进程回收丢失，绝不猜 0；
    - stdout/stderr 不可回收：管道随原 Popen 一并消失。
    """

    def __init__(self, pid: int, start_time: float) -> None:
        self.pid = pid
        self.start_time = start_time

    def check(self) -> bool | None:
        """三态判定：True 同一 run 仍活着 / False 已终止或 pid 复用 / None 无法确认。

        身份比对只回答「是不是同一 run」（pid 复用检出），不回答死活——
        Windows 下已退出的进程句柄仍可打开、启动时间仍可读，身份比对
        对死进程照样返回 True。死活必须再问 process_alive：身份已证明时
        存活即「同一 run 活着」、不存活即「该 run 已终止」。
        """
        if identity_matches(self.pid, self.start_time) is False:
            return False  # pid 复用：确认不是同一 run
        # 身份证明通过（或启动时间读不到）：以存活探测为准。
        # 读不到启动时间的退化路径不再声称「确认同一 run」——由观察层
        # 按 unknown 处理（reattach 已拒绝过这种句柄，此为防御兜底）。
        return process_alive(self.pid)

    def terminate(self) -> bool:
        """尽力终止整棵进程树（拿不到句柄如实返回 False，不假装成功）。

        重绑进程只有 pid，没有会话关系，因此走的也是树终止而不是单进程杀：
        harness 的 worker 会随主进程一起留下，只收主进程等于把写入源留活。
        """
        return terminate_tree(self.pid)


# ── CLI 兼容性：harness 结构化终态事件（v2 归档候选路径的落地）──
# harness（cli-bridge/自研 CLI）在 stdout 写一行 JSON 声明终态：
#   {"event":"attempt/finished","outcome":"succeeded|failed","returncode":0}
# 适配器持续排水并扫描该行。价值（v2/v3/v4 实测教训）：
# 1. 终态时机：cli-bridge 主进程与内部 worker 生命周期不对齐——事件行让
#    「干完了」由 harness 主动声明，不再等主进程退出；
# 2. 成功语义：CLI 软失败也退 0——事件行的 outcome 是 harness 的显式
#    判定，比裸退出码诚实（退出码仍如实并报，两者矛盾时以事件为准
#    并标注 exit_code_conflict）。
FINISHED_EVENT_PREFIX = '{"event":"attempt/finished"'
# 容错扫描：JSON 序列化器对键值间空白的处理各不相同（Python 的
# json.dumps 默认在 ':' 后加空格），只认紧凑前缀会把合规的事件行拒之门外，
# 且失败是静默的（退化成「等进程退出」）。本正则只放宽空白，
# 事件值仍必须精确匹配，且随后仍要 json.loads 通过才认。
FINISHED_EVENT_RE = re.compile(r'"event"\s*:\s*"attempt/finished"')
# 凭据字段（可选）：事件行是**自报**的完成声明，任何打印出该行的输出都会被
# 采信——包括 harness 把文档示例回显出来。spawn 时注入的 per-attempt
# session_token 已在子进程环境里，因此鼓励 harness 在事件行里带上它：
# 带且匹配 → completion_attested=True（凭据自报）；不带 → False（自报，仍
# 按既有语义采信，不破坏存量 harness）；带了但不匹配 → 视同未自报，拒绝。
# 这**不改**完成语义，只让「谁有能力声明完成」在审计里可区分。
FINISHED_TOKEN_FIELD = "session_token"  # noqa: S105 — JSON 字段名，非密钥（真值是 spawn 时注入的 per-attempt 凭据）
FINISHED_LINE_MAX = 8192  # 事件行长度上限：防御性，超长截断不匹配


class _MonitoredProcess:
    """Popen 包装：后台排水 + stdout 终态事件扫描（CLI 兼容性核心）。

    为什么必须排水（真 bug 级修复）：PIPE 缓冲区约 64KB，LLM CLI 的
    长输出会写满管道 → 子进程 write() 阻塞 → 进程卡死永不退出。
    后台线程持续读空管道，输出积累在内存（带上限，防 OOM）。

    线程安全：reader 线程只 append buffer；主线程只在 join 后读。
    退出前最后一段输出由 _drain_final 兜底收全。
    """

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        max_output_bytes: int | None = None,
        expected_session_token: str | None = None,
    ) -> None:
        self._proc = proc
        self._output_limit = max(0, int(max_output_bytes or 0)) or None
        self._output_bytes = 0
        self.output_truncated = False
        self.stdout_buf: list[bytes] = []
        self.stderr_buf: list[bytes] = []
        self.finished_event: dict[str, Any] | None = None
        # 完成事件是自报声明；带凭据的声明与裸声明在审计上可区分（见
        # FINISHED_TOKEN_FIELD 说明）。None = 本 attempt 未注入凭据。
        self._expected_session_token = expected_session_token
        self.completion_attested: bool | None = None
        self._stdout_bytes = 0
        self._t_out = _start_reader(proc.stdout, self.stdout_buf, self, "out")
        self._t_err = _start_reader(proc.stderr, self.stderr_buf, self, "err")

    # -- Popen 兼容面 --
    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def poll(self) -> int | None:
        return self._proc.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        return self._proc.wait(timeout=timeout)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    # -- 监控面 --
    def _raw_stdout(self) -> str:
        return b"".join(self.stdout_buf).decode("utf-8", errors="replace")

    def _raw_stderr(self) -> str:
        return b"".join(self.stderr_buf).decode("utf-8", errors="replace")

    def stdout_text(self) -> str:
        """累积输出 → 清洗后的文本（DESIGN §14.2 入站边界）。

        清洗放在这里而不是各个消费方：collect 是本适配器唯一的输出出口，
        证据固化、``lhgp-verdict`` 解析、投影写入都从它取数，逐个清洗必然
        漂移成几份口径（终端清洗是幂等的，重复经过边界不会叠加损耗）。
        """
        return sanitize_terminal_text(self._raw_stdout())

    def stderr_text(self) -> str:
        return sanitize_terminal_text(self._raw_stderr())

    def output_sanitized(self) -> bool:
        """本次输出是否被清洗改动过（DESIGN §14.2）。

        清洗**改写了证据的字节**，这件事必须和 ``output_truncated`` 一样在
        collect 结果里可见：审计要能区分「执行者的原始输出」与「协议改写过的
        输出」，否则「不可信文本一定经过清洗」这条保证就没有可观察面
        （可观察性是 §14 的硬要求，不是附赠项）。
        """
        return has_terminal_controls(self._raw_stdout()) or has_terminal_controls(
            self._raw_stderr()
        )

    def join_readers(self, timeout: float = 10.0) -> None:
        """进程退出后收尾 reader 线程（管道 EOF 即自然结束）。"""
        for t in (self._t_out, self._t_err):
            t.join(timeout=timeout)

    def close_streams(self) -> None:
        """关闭已退出子进程的父端管道，释放句柄。"""
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _note_finished(self, event: dict[str, Any]) -> None:
        """reader 线程回调：扫到 attempt/finished 事件行。"""
        self.finished_event = event


def _start_reader(
    stream: Any,
    buf: list[bytes],
    monitored: _MonitoredProcess,
    which: str,
) -> threading.Thread:
    """启动排水线程：读满即 append；扫描终态事件行。"""
    import threading

    def _run() -> None:
        line_so_far = b""
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            # 始终排空管道防止子进程死锁，但只保留预算允许的字节数。
            # 终态事件先扫描完整 chunk，再截断审计缓冲，确保兼容性事件
            # 不会因输出预算而丢失。
            if which == "out":
                line_so_far += chunk
                if len(line_so_far) > FINISHED_LINE_MAX:
                    line_so_far = line_so_far[-FINISHED_LINE_MAX:]
                if _scan_finished(line_so_far, monitored) or chunk.endswith(b"\n"):
                    line_so_far = b""
            if monitored._output_limit is None:
                buf.append(chunk)
            else:
                remaining = monitored._output_limit - monitored._output_bytes
                if remaining > 0:
                    kept = chunk[:remaining]
                    buf.append(kept)
                    monitored._output_bytes += len(kept)
                if len(chunk) > max(0, remaining):
                    monitored.output_truncated = True
        stream.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _scan_finished(line_bytes: bytes, monitored: _MonitoredProcess) -> bool:
    """尝试把累积的行解析成 attempt/finished 事件；成功即回调。

    识别分两步且都不可省：先正则找事件（容忍键值间空白，见
    FINISHED_EVENT_RE 的说明），再 json.loads 校验整行——正则命中的是
    「形似」，只有能解析成 JSON 且 event 字段精确相等才算数。
    """
    text = line_bytes.decode("utf-8", errors="replace").strip()
    match = FINISHED_EVENT_RE.search(text)
    if match is None:
        return False
    # 从最近的 '{' 起解析（事件行前缀可能被前一段输出污染）
    start = text.rfind("{", 0, match.start() + 1)
    if start < 0:
        return False
    candidate = text[start:]
    try:
        import json as _json

        data = _json.loads(candidate)
    except ValueError:
        return False
    if not isinstance(data, dict) or data.get("event") != "attempt/finished":
        return False
    supplied = data.get(FINISHED_TOKEN_FIELD)
    if supplied is not None:
        # 用了凭据字段就必须对：错 token 视同未自报（拒绝该行）。这防的是
        # 「别的 attempt 的回显」与拼错的 token 被当成完成声明。
        expected = monitored._expected_session_token
        if not isinstance(supplied, str) or not expected or supplied != expected:
            return False
        monitored.completion_attested = True
    else:
        monitored.completion_attested = False
    monitored._note_finished(data)
    return True


class SubprocessAdapter(ExecutorAdapter):
    """结构化 argv 拉起的 CLI 执行器适配器。

    约束翻译（DESIGN §9 编译表，翻不了即拒接）：
    - file_effects.mode=workspace-write → 结构化 cwd 绑定，实测 enforcement=partial；
      read-only 等其余模式无强制面可证 → 拒接
    - workspace_root / deny_paths 服务端归一化后比较，deny 落入或覆盖
      workspace 即拒接（含 ../ 穿越变体与符号链接逃逸，DESIGN §14）
    - network.mode=deny 只有 manifest 声明独立网络策略（network=deny）才兑现；
      提示词不算（DESIGN §9）
    - process / package_install 无法证明 → 拒接
    """

    def __init__(
        self,
        manifest: ExecutorManifest,
        launch: LaunchSpec | None = None,
        grace_period_seconds: float = DEFAULT_GRACE_SECONDS,
        collect_timeout_seconds: float = DEFAULT_COLLECT_TIMEOUT_SECONDS,
    ) -> None:
        if grace_period_seconds <= 0:
            raise ValueError(f"grace_period_seconds 必须为正: {grace_period_seconds}")
        if collect_timeout_seconds <= 0:
            raise ValueError(f"collect_timeout_seconds 必须为正: {collect_timeout_seconds}")
        self._manifest = manifest
        self._launch = launch if launch is not None else LaunchSpec()
        self._grace_period_seconds = grace_period_seconds
        self._collect_timeout_seconds = collect_timeout_seconds
        # attempt_id → 进程映射：observe/cancel/collect 都以 attempt 为键。
        # 值可能是本进程 spawn 的 _MonitoredProcess（排水+终态事件），
        # 也可能是重启后按句柄重绑的 _DetachedProcess。
        self._procs: dict[str, _MonitoredProcess | _DetachedProcess] = {}
        # attempt_id → spawn 时记录的进程身份（pid + 启动时间），供 run_handle 持久返回
        self._identities: dict[str, dict[str, float]] = {}
        self._cancelled: set[str] = set()

    @property
    def id(self) -> str:
        return self._manifest.executor_id

    def describe(self) -> ExecutorManifest:
        return self._manifest

    def health(self) -> bool:
        """launch argv 干跑自检：可执行文件现在可达。

        只证明「现在能连接」，不证明任何能力（DESIGN §12.4）。
        """
        if not self._launch.argv:
            return False
        return shutil.which(self._launch.argv[0]) is not None

    def prepare(self, input_: AttemptInput) -> PreparedLaunch:
        """翻译合同硬约束为运行时强制面；翻不了 → PrepareRefusedError（拒接）。"""
        reasons: list[str] = []
        hard = _hard_constraints(input_, reasons)
        workspace = self._resolve_workspace(input_, hard, reasons)
        if workspace is not None:
            _check_deny_paths(hard, workspace, reasons)
        _check_file_effects(hard, self._manifest, reasons)
        _check_network(hard, self._manifest, reasons)
        _check_process(hard, reasons)
        _check_package_install(hard, reasons)
        _check_context(input_, reasons)
        if not self._launch.argv:
            reasons.append("launch argv 未配置：没有结构化 argv 可拉起")
        else:
            # {task} 占位符唯一性（dogfood v4 教训：各 CLI 的任务文本位置
            # 不同——位置参数 / -p 值 / 子命令——占位符把「插在哪」变成
            # 注册表配置数据）。多于一个 → 拒接（歧义，fail-closed）。
            placeholders = sum(1 for a in self._launch.argv if a == TASK_PLACEHOLDER)
            if placeholders > 1:
                reasons.append(
                    f"launch.argv 含 {placeholders} 个 {TASK_PLACEHOLDER} 占位符：最多一个"
                )
        if reasons:
            raise refuse("; ".join(reasons))
        if workspace is None:
            # 不可达兜底：workspace 为 None 时 _resolve_workspace 必已记理由（fail-closed）
            raise refuse("workspace_root 解析失败")
        return PreparedLaunch(
            argv=self._launch.argv,
            cwd=str(workspace),
            env_allowlist=self._launch.env_allowlist,
            # 实测只有 cwd 绑定，报告 partial（DESIGN §12.4：夸口即拒接）
            enforcement=Enforcement.PARTIAL,
            context_snapshot_path=input_.context_snapshot_path,
        )

    def spawn(self, input_: AttemptInput, launch: PreparedLaunch) -> str:
        """按结构化声明拉起子进程（列表参数、shell=False），返回 session_ref。"""
        if input_.attempt_id in self._procs:
            raise ValueError(f"attempt 已拉起，拒绝重复 spawn: {input_.attempt_id}")
        reasons: list[str] = []
        if not launch.argv:
            reasons.append("launch.argv 为空：没有可执行的结构化声明")
        for element in launch.argv:
            if not isinstance(element, str) or not element:
                reasons.append("launch.argv 必须是非空字符串元组")
                break
        expected = self._resolve_workspace(input_, _hard_constraints(input_, []), reasons)
        if expected is None:
            reasons.append("spawn 无法确定期望 workspace_root，拒绝拉起")
        elif launch.cwd is not None and Path(launch.cwd).expanduser().resolve() != expected:
            # §14 适配器侧防线：伪造的 launch 不能借 spawn 把 cwd 挪出工作区
            reasons.append(f"launch.cwd 越出 workspace_root: {launch.cwd}")
        if reasons:
            raise refuse("; ".join(reasons))
        if expected is None:
            # 不可达兜底：expected 为 None 时上方必已记理由（fail-closed）
            raise refuse("spawn 无法确定期望 workspace_root")
        # 环境白名单：只透显式列出的变量；模型输出永不进入 argv 或环境
        env = {name: os.environ[name] for name in launch.env_allowlist if name in os.environ}
        # Per-attempt session token：执行者进程通过此环境变量获取写回凭据
        if input_.session_token:
            env["LHGP_SESSION_TOKEN"] = input_.session_token
        # SPEC §12.4「通道编码」：stdout 的两条协议通道（完成事件行与
        # lhgp-verdict 判定块）约定 UTF-8，适配器按 UTF-8 解码子进程输出
        # （stdout_text / _scan_finished）。但派生的解释器默认跟随宿主 locale
        # ——中文 Windows 是 cp936——于是任何非 ASCII 证据（验收者 details、
        # 含中文的路径）会被 errors="replace" 静默打成 U+FFFD：损坏不可逆、
        # 不报错，只在断言恰好漏过时表现为「跑通了但证据是乱码」。
        # 这里为 Python 执行者钉住 stdio 编码，让实现兑现它已经假设的契约。
        # 与 LHGP_SESSION_TOKEN 同理，属协议注入而非宿主透传：放行宿主传入的
        # 其他取值只会保证解码出错。非 Python 执行者不受该变量影响，其义务由
        # task_prompt 与 SPEC §12.4 声明。
        env["PYTHONIOENCODING"] = "utf-8"
        # P1 review fix (2026-09-08): pass the context snapshot path to the
        # subprocess. The default executor must be able to read the freshly
        # built active.md (including the user's directive queue, handover
        # summary, contract anchor, etc.). Without this env var the executor
        # only sees task_prompt in argv and the cursor advances uselessly
        # because nothing actually consumes the snapshot.
        if input_.context_snapshot_path:
            env["LHGP_CONTEXT_SNAPSHOT_PATH"] = input_.context_snapshot_path
        # 任务文本注入（DESIGN §12.1）：{task} 占位符 → 原位替换；无占位符
        # → 尾元素追加（向后兼容：cli-bridge 等位置参数 CLI 的既有形态）。
        # 文本是用户审定的冻结区数据，非模型输出；列表参数 + shell=False，
        # 无 shell 拼接面（§14 注入防线）——占位符只是位置标记，不引入拼接。
        argv = list(launch.argv)
        if input_.task_prompt is not None:
            if not input_.task_prompt.strip():
                raise refuse("task_prompt 为空白：没有可交付的任务文本")
            if TASK_PLACEHOLDER in argv:
                argv = [input_.task_prompt if a == TASK_PLACEHOLDER else a for a in argv]
            else:
                argv.append(input_.task_prompt)
        proc = subprocess.Popen(  # noqa: S603 —— 结构化 argv + shell=False，DESIGN §12.1 注入防线
            argv,
            cwd=launch.cwd if launch.cwd is not None else str(expected),
            shell=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # POSIX：子进程自成会话/进程组组长，取消时能用 killpg 一次收掉
            # 整棵树（harness 的 worker 不会留成孤儿继续写工作区）。
            # Windows 无进程组，走 taskkill /T；该参数在 Windows 被忽略，
            # 这里显式只在 POSIX 传，保持 Windows 的拉起参数与既有形态一致。
            start_new_session=os.name == "posix",
        )
        # CLI 兼容性：包装成受监控进程（后台排水防管道死锁 + 终态事件扫描）
        monitored = _MonitoredProcess(
            proc,
            max_output_bytes=input_.budget_remaining.get("max_output_bytes"),
            expected_session_token=input_.session_token,
        )
        self._procs[input_.attempt_id] = monitored
        # §11.3：spawn 后立刻取进程身份（pid + 启动时间）。取不到就如实留空，
        # 之后 reattach 会因无法证明身份而失败，走 orphan grace——不假装可恢复。
        start_time = process_start_time(proc.pid)
        identity: dict[str, float] = {"pid": float(proc.pid)}
        if start_time is not None:
            identity["start_time"] = start_time
        self._identities[input_.attempt_id] = identity
        return f"subprocess:{input_.attempt_id}:{proc.pid}"

    def run_handle(self, attempt_id: str) -> ExternalRunHandle | None:
        """持久返回外部运行句柄（SPEC §11.3）。

        recovery_strategy='reattach'：该适配器能用「pid + 启动时间」双重比对
        在守护进程重启后确认同一外部 run。若 spawn 时取不到启动时间，这里
        如实退化 —— 句柄仍返回，但 process_identity 只有 pid，身份无法证明，
        reattach 会拒绝（不因为「有 pid」就声称能恢复）。
        """
        identity = self._identities.get(attempt_id)
        if identity is None:
            return None
        return ExternalRunHandle(
            external_run_id=str(int(identity["pid"])),
            session_locator=attempt_id,
            recovery_strategy=RECOVERY_REATTACH,
            capability_snapshot=dict(_DETACHED_CAPABILITY),
            process_identity=dict(identity),
        )

    def reattach(self, handle: ExternalRunHandle) -> bool:
        """按持久句柄重新绑定观察关系（SPEC §11.3 分支 1/2 的入口）。

        身份证明通过即绑定并返回 True——无论该 run 活着还是已终止：
        活着走分支 1（observe 报 running），已终止走分支 2（observe 报
        failed，由 reconcile collect 结算）。绝不因「已终止」就拒绝绑定：
        那会把确认的终态降级成「状态未知」，白白烧掉一整轮 orphan grace。
        - 缺 pid 或启动时间 → 无法证明身份，返回 False；
        - pid 存在但启动时间对不上 → pid 复用，返回 False；
        返回 False 一律按「状态未知」处理，绝不据此判定外部 run 已终止。
        """
        raw_pid = handle.process_identity.get("pid")
        if raw_pid is None:
            return False
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            return False
        start_time = handle.process_identity.get("start_time")
        if not isinstance(start_time, (int, float)):
            # 只有 pid：规范明令 PID 不得单独作为身份真相，拒绝绑定
            return False
        if identity_matches(pid, float(start_time)) is not True and process_alive(pid) is not False:
            # 身份载体已消失（进程退出并被收尸，/proc 条目不复存在）说明
            # 本 run 必然已终止——我们的进程先退出，pid 才可能被释放，
            # 此时仍按已终止绑定，交给 observe→分支 2 结算。活着或身份无法
            # 证明的 pid 复用者不会误入：探活非 False 时在此拒绝绑定。
            return False
        self._procs[handle.session_locator] = _DetachedProcess(pid, float(start_time))
        return True

    def observe(self, attempt_id: str) -> dict[str, object]:
        """观察进程状态：存活/退出码/harness 终态事件（CLI 兼容性）。

        判定优先级（v2/v3/v4 实测教训的落地）：
        1. harness 结构化事件 attempt/finished——「干完了」由 harness 主动
           声明（cli-bridge 主进程与 worker 生命周期不对齐的根治路径）：主进程
           还活着但事件已到 → 按事件的 outcome 判终态，事件早于退出即
           价值的全部所在；
        2. 主进程退出码（既有语义，无事件的 CLI 走这里）。
        事件 outcome 与最终退出码矛盾时以事件为准并标注（CLI 软失败退 0
        的情形——harness 的显式判定比裸退出码诚实）。
        """
        proc = self._require(attempt_id)
        if isinstance(proc, _DetachedProcess):
            return self._observe_detached(attempt_id, proc)
        if isinstance(proc, _MonitoredProcess):
            event = proc.finished_event
            if event is not None:
                outcome = str(event.get("outcome", ""))
                if attempt_id in self._cancelled:
                    state = AttemptState.CANCELLED
                elif outcome == "succeeded":
                    state = AttemptState.SUCCEEDED
                else:
                    state = AttemptState.FAILED
                returncode = proc.poll()
                if returncode is None and state is AttemptState.FAILED:
                    # A failed terminal event is authoritative, but collect the
                    # already-imminent process exit when possible so a soft CLI
                    # failure (event=failed, exit=0) is auditable.  Successful
                    # events intentionally remain non-blocking because harnesses
                    # may keep a long-lived process alive during cleanup.
                    with suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=1.0)
                    returncode = proc.poll()
                conflict = (
                    returncode is not None and returncode == 0 and state is AttemptState.FAILED
                )
                result: dict[str, object] = {
                    "state": state.value,
                    "alive": returncode is None,  # 事件已到但主进程可能仍在收尾
                    "returncode": returncode,
                    "exit_code_known": returncode is not None,
                    "finished_by_event": True,
                    "event_outcome": outcome,
                    "completion_attested": proc.completion_attested,
                }
                if conflict:
                    result["exit_code_conflict"] = True
                    result["note"] = "harness declared failure but exit code is 0: event wins"
                return result
        returncode = proc.poll()
        if returncode is None:
            state = AttemptState.RUNNING
        elif attempt_id in self._cancelled:
            state = AttemptState.CANCELLED
        elif returncode == 0:
            state = AttemptState.SUCCEEDED
        else:
            state = AttemptState.FAILED
        return {
            "state": state.value,
            "alive": returncode is None,
            "returncode": returncode,
            "exit_code_known": True,
        }

    def _observe_detached(self, attempt_id: str, proc: _DetachedProcess) -> dict[str, object]:
        """观察重绑进程（§11.3）：三态——存活/确认非同一 run/无法确认。"""
        status = proc.check()
        if status is None:
            # 无法确认 ≠ 已终止：如实报 unknown，由 reconcile 走 orphan grace
            return {
                "state": EXTERNAL_STATE_UNKNOWN,
                "alive": None,
                "returncode": None,
                "exit_code_known": False,
                "reason": "external run identity unverifiable (§11.3)",
            }
        if status is True:
            return {
                "state": AttemptState.RUNNING.value,
                "alive": True,
                "returncode": None,
                "exit_code_known": False,
            }
        # 退出码不可得：不可得 ≠ 成功 —— fail-closed 判 failed 并写明原因，
        # 由验收/repair 闭环复核，不把「不知道」伪装成 succeeded。
        state = AttemptState.CANCELLED if attempt_id in self._cancelled else AttemptState.FAILED
        return {
            "state": state.value,
            "alive": False,
            "returncode": None,
            "exit_code_known": False,
            "error_class": "external-run-exit-unrecoverable",
        }

    def cancel(self, attempt_id: str, reason: str) -> None:
        """取消 attempt：整棵树先礼后兵，仍存活才升级强杀。

        只杀直接子进程会把 harness 的 worker 留成孤儿——对合同来说这是
        「已取消却还在改盘」，比取消失败更难查（没有事件、没有报错，只有
        盘上多出来的改动）。因此两条路径都以**树**为单位。
        """
        proc = self._require(attempt_id)
        if isinstance(proc, _DetachedProcess):
            # 重绑进程：只能尽力终止，无法等待其退出（非子进程）
            if proc.check() is not False:
                proc.terminate()
                self._cancelled.add(attempt_id)
            return
        if proc.poll() is not None:
            # 已自行退出：无可取消对象，保留原终态（不伪装成 cancelled）
            return
        graceful = terminate_tree(proc.pid)
        if graceful:
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=self._grace_period_seconds)
        # 优雅路径不可用、或宽限期内没退：强杀整棵树。即使直接子进程已经
        # 退出也照杀——此时残留的正是要清理的孙进程。
        if proc.poll() is None or not graceful:
            terminate_tree(proc.pid, force=True)
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=self._grace_period_seconds)
        proc.close_streams()
        self._cancelled.add(attempt_id)

    def collect(self, attempt_id: str) -> dict[str, object]:
        """回收结果：退出码 + stdout/stderr；未退出抛 TimeoutExpired（不伪造结果）。

        CLI 兼容性（v2/v3/v4 教训）：输出由后台 reader 线程持续积累——
        collect 时不再 communicate()（它要求进程退出且阻塞等管道，观察
        窗口错配就永远拿不到输出）。进程已退出 → join reader 收尾后从
        累积缓冲取全文；终态事件已到但主进程未退 → 也允许 collect
        （事件即 harness 的完成声明），退出码字段如实标不可得。
        """
        proc = self._require(attempt_id)
        if isinstance(proc, _DetachedProcess):
            # 管道与退出状态随原 Popen 丢失：如实报错，绝不编一个退出码或空输出
            raise RuntimeError(
                "detached run: exit code and output unrecoverable after reattach (§11.3)"
            )
        if isinstance(proc, _MonitoredProcess):
            event = proc.finished_event
            if event is not None:
                # harness 已声明完成：不等主进程退出（cli-bridge 收尾期可长达分钟）
                proc.join_readers(timeout=5.0)
                outcome = str(event.get("outcome", ""))
                if attempt_id in self._cancelled:
                    state = AttemptState.CANCELLED
                elif outcome == "succeeded":
                    state = AttemptState.SUCCEEDED
                else:
                    state = AttemptState.FAILED
                return {
                    "state": state.value,
                    "returncode": None,
                    "exit_code_known": False,
                    "finished_by_event": True,
                    "event_outcome": outcome,
                    "completion_attested": proc.completion_attested,
                    "stdout": proc.stdout_text(),
                    "stderr": proc.stderr_text(),
                    "output_truncated": proc.output_truncated,
                    "output_sanitized": proc.output_sanitized(),
                }
            # 无事件：等进程退出（语义与旧版一致——不退出就不结算）
            try:
                proc.wait(timeout=self._collect_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise subprocess.TimeoutExpired(
                    cmd="collect", timeout=self._collect_timeout_seconds
                ) from exc
            proc.join_readers(timeout=5.0)
            proc.close_streams()
            returncode = proc.returncode
            if returncode is None:
                # 防御性兜底：wait 返回即已退出，不伪造结果
                raise RuntimeError(f"collect: 进程未退出: {attempt_id}")
            if attempt_id in self._cancelled:
                state = AttemptState.CANCELLED
            elif returncode == 0:
                state = AttemptState.SUCCEEDED
            else:
                state = AttemptState.FAILED
            return {
                "state": state.value,
                "returncode": returncode,
                "stdout": proc.stdout_text(),
                "stderr": proc.stderr_text(),
                "output_truncated": proc.output_truncated,
                "output_sanitized": proc.output_sanitized(),
            }

    def _require(self, attempt_id: str) -> _MonitoredProcess | _DetachedProcess:
        """按 attempt 取进程；未知 attempt 用 KeyError 如实报告。"""
        try:
            return self._procs[attempt_id]
        except KeyError:
            raise KeyError(f"未知 attempt: {attempt_id}") from None

    def _resolve_workspace(
        self,
        input_: AttemptInput,
        hard: dict[str, Any],
        reasons: list[str],
    ) -> Path | None:
        """归一化 workspace_root（DESIGN §14：归一化后与 deny_paths 比较）。"""
        raw_candidates: list[str] = []
        file_effects = hard.get("file_effects")
        if isinstance(file_effects, dict):
            root = file_effects.get("workspace_root")
            if isinstance(root, str) and root.strip():
                raw_candidates.append(root)
        if input_.workspace_root.strip():
            raw_candidates.append(input_.workspace_root)
        if not raw_candidates:
            # 合同与 AttemptInput 都没给 workspace：退回注册表 launch.cwd
            if self._launch.cwd:
                raw_candidates.append(self._launch.cwd)
            else:
                reasons.append("workspace_root 缺失：无法绑定结构化 cwd")
                return None
        for raw in raw_candidates:
            # 相对路径会被 resolve() 静默拼进本进程 cwd，必须显式拒绝
            if not Path(raw).expanduser().is_absolute():
                reasons.append(f"workspace_root 必须是绝对路径: {raw}")
                return None
        resolved_candidates = [_normalize(raw) for raw in raw_candidates]
        first = resolved_candidates[0]
        if any(candidate != first for candidate in resolved_candidates[1:]):
            reasons.append(f"workspace_root 不一致（合同冻结区 vs AttemptInput）: {raw_candidates}")
            return None
        return first


def refuse(reason: str) -> PrepareRefusedError:
    """构造拒接异常。统一入口保证拒接理由总是人类可读（DESIGN §9）。"""
    return PrepareRefusedError(f"constraint untranslatable: {reason}")


def _hard_constraints(input_: AttemptInput, reasons: list[str]) -> dict[str, Any]:
    """取冻结区硬约束；形状不对按 fail-closed 记拒接理由。"""
    hard = input_.contract_snapshot.get("hard_constraints", {})
    if not isinstance(hard, dict):
        reasons.append("hard_constraints 必须是对象")
        return {}
    return hard


def _normalize(raw: str) -> Path:
    """服务端归一化：expanduser + resolve（跟随符号链接、折叠 ../）。"""
    return Path(raw).expanduser().resolve()


def _deny_base(raw: str) -> str:
    """剥掉 glob 尾巴得到禁写区根（如 ~/ref/** → ~/ref）。"""
    text = raw.strip()
    for suffix in ("/**/*", "/**/", "/**"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _check_file_effects(
    hard: dict[str, Any],
    manifest: ExecutorManifest,
    reasons: list[str],
) -> None:
    """file_effects.mode 翻译：本适配器只有 cwd 绑定能力（DESIGN §9）。"""
    file_effects = hard.get("file_effects")
    if not isinstance(file_effects, dict):
        return
    mode = file_effects.get("mode")
    if mode is None:
        return
    if mode != "workspace-write":
        reasons.append(f"file_effects.mode={mode!r} 无法翻译：本适配器只有 cwd 绑定能力")
        return
    if manifest.capabilities.sandbox.file_effects != "workspace-write":
        reasons.append(
            "manifest 声明的 file_effects 能力与 cwd 绑定不符: "
            f"{manifest.capabilities.sandbox.file_effects!r}"
        )


def _check_deny_paths(
    hard: dict[str, Any],
    workspace: Path,
    reasons: list[str],
) -> None:
    """deny_paths 前缀检查：禁写区不得落入或覆盖 workspace（含 ../ 穿越）。"""
    file_effects = hard.get("file_effects")
    if not isinstance(file_effects, dict):
        return
    deny_paths = file_effects.get("deny_paths")
    if deny_paths is None:
        return
    if not isinstance(deny_paths, list):
        reasons.append("deny_paths 必须是列表")
        return
    for raw in deny_paths:
        if not isinstance(raw, str) or not raw.strip():
            reasons.append("deny_paths 含非字符串或空条目")
            continue
        base = Path(_deny_base(raw)).expanduser()
        if not base.is_absolute():
            reasons.append(f"deny_paths 条目必须是绝对路径: {raw}")
            continue
        resolved = base.resolve()
        if resolved == workspace:
            reasons.append(f"deny_paths 禁写区与 workspace_root 重叠: {raw}")
        elif workspace in resolved.parents:
            # 归一化后仍在工作区内：直写子路径或 ../ 穿越变体都算（DESIGN §14）
            reasons.append(f"deny_paths 禁写区落在 workspace_root 内: {raw}")
        elif resolved in workspace.parents:
            reasons.append(f"deny_paths 禁写区覆盖 workspace_root: {raw}")


def _check_network(
    hard: dict[str, Any],
    manifest: ExecutorManifest,
    reasons: list[str],
) -> None:
    """network.mode=deny：只有 manifest 声明独立网络策略才兑现，提示词不算。"""
    network = hard.get("network")
    if not isinstance(network, dict):
        return
    if network.get("mode") == "deny" and manifest.capabilities.sandbox.network != "deny":
        reasons.append(
            "network.mode=deny 无法兑现：manifest 未声明独立网络策略"
            f"（声明的是 {manifest.capabilities.sandbox.network!r}），提示词不算"
        )


def _check_process(hard: dict[str, Any], reasons: list[str]) -> None:
    """process.mode=restricted：通用 subprocess 无进程沙箱，无法证明即拒接。"""
    process = hard.get("process")
    if isinstance(process, dict) and process.get("mode") == "restricted":
        reasons.append("process.mode=restricted 无法证明：通用 subprocess 无进程沙箱")


def _check_package_install(hard: dict[str, Any], reasons: list[str]) -> None:
    """package_install.mode=deny：无包管理拦截面，无法证明即拒接。"""
    package_install = hard.get("package_install")
    if isinstance(package_install, dict) and package_install.get("mode") == "deny":
        reasons.append("package_install.mode=deny 无法证明：无包管理拦截面")


def _check_context(input_: AttemptInput, reasons: list[str]) -> None:
    """context.required=true：必须携带已物化的 context_snapshot_path（DESIGN §9）。"""
    context = input_.contract_snapshot.get("context")
    if (
        isinstance(context, dict)
        and context.get("required") is True
        and not input_.context_snapshot_path
    ):
        reasons.append("context.required=true 但 AttemptInput 未携带 context_snapshot_path")


__all__ = [
    "DEFAULT_COLLECT_TIMEOUT_SECONDS",
    "DEFAULT_GRACE_SECONDS",
    "TASK_PLACEHOLDER",
    "Enforcement",
    "LaunchSpec",
    "SubprocessAdapter",
    "refuse",
]
