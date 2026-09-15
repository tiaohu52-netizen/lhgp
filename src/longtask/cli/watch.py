"""交互式事件流：`longtask watch`（DESIGN §10 用户主动观察通道）。

零运行时三方依赖。读 protocols/events 分页（after_event_id 游标），
事件类型按生命周期语义染色（ANSI；Windows 走 ctypes 启用 VT；自动 NO_COLOR）。
纯只读客户端，不发 RPC、不改 daemon；daemon 离线也能复盘历史事件。

折叠：相邻 lease/renewed 连续 N 条合并为「xN」避免噪音；其他事件逐行。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

from longtask.cli.paths import default_data_root
from longtask.persistence.events import EventType
from longtask.persistence.store import StoreConfig, connect, ensure_schema, get_events

DEFAULT_PAGE = 200
DEFAULT_FOLLOW_INTERVAL = 1.5  # 跟随时轮询间隔（秒）


def _enable_vt_on_windows() -> bool:
    """Windows 10+ 控制台 VT 启用：返回是否成功启用（no-op on POSIX）。"""
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        std_output_handle = -11
        handle = kernel32.GetStdHandle(std_output_handle)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False
        enable_vt = 0x0004
        result = kernel32.SetConsoleMode(handle, mode.value | enable_vt)
        return bool(result) if isinstance(result, int) else False
    except (OSError, AttributeError):
        return False


_COLOR_ENABLED = _enable_vt_on_windows() and os.environ.get("NO_COLOR") is None

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"

_STYLE: dict[str, str] = {
    EventType.CONTRACT_PREPARED.value: "\033[36m",
    EventType.CONTRACT_APPROVED.value: "\033[32m",
    EventType.CONTRACT_COMPLETED.value: "\033[1;32m",
    EventType.CONTRACT_EXPIRED.value: "\033[33m",
    EventType.CONTRACT_CANCELLED.value: "\033[90m",
    EventType.CONTRACT_BLOCKED.value: "\033[33m",
    EventType.CONTRACT_ARBITRATED.value: "\033[35m",
    EventType.CONTRACT_PAUSED.value: "\033[34m",
    EventType.CONTRACT_RESUMED.value: "\033[34m",
    EventType.ATTEMPT_STARTED.value: "\033[36m",
    EventType.ATTEMPT_SUCCEEDED.value: "\033[32m",
    EventType.ATTEMPT_FAILED.value: "\033[31m",
    EventType.ATTEMPT_STALE.value: "\033[33m",
    EventType.ATTEMPT_CANCELLED.value: "\033[33m",
    EventType.ATTEMPT_ADMITTED.value: "\033[36m",
    EventType.LEASE_ACQUIRED.value: "\033[35m",
    EventType.LEASE_RENEWED.value: "\033[90m",
    EventType.LEASE_RELEASED.value: "\033[35m",
    EventType.LEASE_RECLAIMED.value: "\033[35m",
    EventType.LEASE_FENCED.value: "\033[31m",
    EventType.WAKEUP_DEGRADED.value: "\033[31m",
    EventType.WAKEUP_RTC_ARMED.value: "\033[35m",
    EventType.WAKEUP_RTC_FIRED.value: "\033[35m",
    EventType.WAKEUP_SLEEP_GUARD.value: "\033[35m",
    EventType.CONTEXT_SNAPSHOT_BUILT.value: "\033[36m",
    EventType.CONTEXT_SNAPSHOT_EXPIRED.value: "\033[33m",
    EventType.CONTEXT_SCRATCH_UPDATED.value: "\033[36m",
    EventType.CONTEXT_CAPACITY_REFUSED.value: "\033[31m",
    EventType.CONTEXT_REBUILT.value: "\033[36m",
    EventType.STORE_TAMPERED.value: "\033[31m",
    EventType.HANDOVER_INCOMPLETE.value: "\033[33m",
    EventType.DISPATCH_REFUSED.value: "\033[33m",
    EventType.ESCALATION_HANDED_TO_USER.value: "\033[33m",
    EventType.ESCALATION_REMINDED.value: "\033[34m",
    EventType.ESCALATION_STEERED.value: "\033[34m",
    EventType.ESCALATION_SPAWNED.value: "\033[36m",
    EventType.ESCALATION_PARALLELIZED.value: "\033[36m",
}


def _shorten(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [+{len(text) - limit} chars]"


def _summary(event_type: EventType | str, payload: dict[str, Any]) -> str:
    """事件单行摘要：关心远期目标的用户能一眼看懂的形态。"""
    if not payload:
        return ""
    e = event_type.value if isinstance(event_type, EventType) else event_type
    if e == "contract/prepared":
        return f'new contract "{payload.get("title", "")}"'
    if e == "contract/approved":
        return f"approved -> active (rev {payload.get('revision', '?')})"
    if e == "contract/completed":
        ver = payload.get("verifier") or payload.get("actor", "?")
        return f"completed (verifier={ver})"
    if e == "contract/blocked":
        return f"blocked: {payload.get('reason', '')}"[:200]
    if e == "contract/expired":
        return f"deadline passed ({payload.get('arbitrated_at', '')})"
    if e == "contract/cancelled":
        return f"cancelled by {payload.get('actor', '?')}"
    if e == "attempt/started":
        return f"executor={payload.get('executor_id', '?')} role={payload.get('role', 'executor')}"
    if e == "attempt/succeeded":
        rc = payload.get("returncode", "?")
        return f"succeeded (rc={rc})"
    if e == "attempt/failed":
        reason = (
            payload.get("reason")
            or payload.get("collect_error")
            or payload.get("reported_by")
            or ""
        )
        return f"failed: {reason}"[:200]
    if e == "attempt/cancelled":
        return f"cancelled: {payload.get('reason', '')}"[:200]
    if e == "lease/acquired":
        gen = payload.get("generation", "?")
        return f"gen {gen}, holder={payload.get('holder_attempt_id', '?')}"
    if e == "lease/renewed":
        return f"gen {payload.get('generation', '?')}"
    if e == "lease/released":
        return f"holder={payload.get('holder_attempt_id', '?')} actor={payload.get('actor', '?')}"
    if e == "lease/fenced":
        return f"fenced: {payload.get('reason', '')[:120]}"
    if e == "context/snapshot-built":
        return f"snapshot -> {payload.get('active_path', '?')}"
    if e == "context/snapshot-expired":
        return f"snapshot expired for {payload.get('attempt_id', '?')}"
    if e == "wakeup/degraded":
        return f"layer={payload.get('layer', '?')}: {payload.get('reason', '')[:120]}"
    if e == "dispatch/refused":
        return f"refused: {payload.get('reason', '')[:160]}"
    if e == "escalation/handed-to-user":
        return str(payload.get("reason", ""))[:160]
    for k, v in payload.items():
        if isinstance(v, (str, int, float, bool)):
            return f"{k}={v}"
    return ""


def _fmt_timestamp(ts: datetime) -> str:
    if not _COLOR_ENABLED:
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    today = ts.date()
    now = datetime.now(ts.tzinfo)
    if today == now.date():
        return f"{_DIM}{ts.strftime('%H:%M:%S')}{_RESET}"
    return f"{_DIM}{ts.strftime('%Y-%m-%d %H:%M:%S')}{_RESET}"


def _format_line(event: Any) -> str:
    color = _STYLE.get(event.event_type, "")
    et = event.event_type
    e_short = et.value if isinstance(et, EventType) else et
    ts = (
        _fmt_timestamp(event.created_at)
        if isinstance(event.created_at, datetime)
        else str(event.created_at)
    )
    cid = event.contract_id or "-"
    aid = event.attempt_id or "-"
    body = _shorten(_summary(event.event_type, _safe_payload(event.payload_json)), 160)
    e_colored = f"{color}{e_short}{_RESET}" if _COLOR_ENABLED and color else e_short
    aid_colored = f"{_BOLD}{aid}{_RESET}" if _COLOR_ENABLED and aid != "-" else aid
    return f"{ts}  {cid:<28}  {aid_colored:<18}  {e_colored:<24}  {body}"


def _emit_renewed_fold(n: int) -> None:
    if _COLOR_ENABLED:
        print(f"    {_DIM}... lease/renewed x {n} ...{_RESET}")
    else:
        print(f"    ... lease/renewed x {n} ...")


def _emit_idle(now: datetime) -> None:
    msg = f"[watch idle @ {now.strftime('%H:%M:%S')}]"
    if _COLOR_ENABLED:
        print(f"{_DIM}{msg}{_RESET}")
    else:
        print(msg)


def _matches_filter(event: Any, *, executor_id: str | None, kinds: set[EventType] | None) -> bool:
    if kinds is not None and event.event_type not in kinds:
        return False
    return not (
        executor_id is not None
        and f'"executor_id": "{executor_id}"' not in (event.payload_json or "")
    )


def _fold_renewed(events: list[Any]) -> list[Any]:
    """相邻 lease/renewed 折叠：连续 N 条合并为 fold 元组。"""
    folded: list[Any] = []
    n_pending = 0
    last_event: Any = None
    for e in events:
        if e.event_type == EventType.LEASE_RENEWED.value:
            n_pending += 1
            last_event = e
            continue
        if n_pending > 0:
            folded.append(("fold", n_pending, last_event))
            n_pending = 0
            last_event = None
        folded.append(e)
    if n_pending > 0:
        folded.append(("fold", n_pending, last_event))
    return folded


def _emit_batch(events: list[Any]) -> None:
    for item in _fold_renewed(events):
        if isinstance(item, tuple) and item[0] == "fold":
            _emit_renewed_fold(item[1])
        else:
            print(_format_line(item))


def _parse_event_kinds(patterns: list[str]) -> set[EventType] | None:
    """glob 展开成具体事件名；返回 None 表示全放行（empty input）。

    fail-closed：无法识别的 kind **必须报错**，不能用 ``suppress`` 悄悄丢掉。
    旧实现把非法值丢弃后若集合为空就返回 ``None``——而 ``None`` 的语义是
    「全放行」，于是 ``--kinds typo`` 会显示**所有**事件而不是报错。用户
    以为自己在看某一类事件，实际看到的是未过滤的全量流。
    """
    if not patterns:
        return None
    all_kinds = [e.value for e in EventType]
    expanded: list[str] = []
    for p in patterns:
        if "*" in p:
            prefix = p.split("*", 1)[0]
            expanded.extend(v for v in all_kinds if v.startswith(prefix))
        else:
            expanded.append(p)
    out: set[EventType] = set()
    unknown: list[str] = []
    for v in expanded:
        try:
            out.add(EventType(v))
        except ValueError:
            unknown.append(v)
    if unknown:
        valid = ", ".join(sorted(all_kinds))
        raise ValueError(
            f"unknown event kind(s): {', '.join(sorted(set(unknown)))}; valid kinds: {valid}"
        )
    return out or None


def _safe_payload(text: str | None) -> dict[str, Any]:
    """损坏的 payload 不得让 watch 崩溃（与 trace 的 ``_raw`` 约定一致）。

    ``events.payload_json`` 是文本列，旧库或手写写入可能留下非 JSON；
    旧实现直接 ``json.loads``，一条坏记录就会让整个 tail 崩溃退出。
    """
    if not text:
        return {}
    try:
        parsed: Any = json.loads(text)
    except (TypeError, ValueError):
        return {"_raw": text[:200]}
    if isinstance(parsed, dict):
        return parsed
    return {"_raw": str(parsed)[:200]}


def watch(
    root: Path,
    *,
    contract_id: str | None = None,
    executor_id: str | None = None,
    follow: bool = False,
    since_event_id: int | None = None,
    duration_seconds: int | None = None,
    page: int = DEFAULT_PAGE,
    kinds: list[str] | None = None,
) -> int:
    """事件流 tail；返回最大 event_id 给后续会话续看。"""
    deadline = datetime.now(UTC) + timedelta(seconds=duration_seconds) if duration_seconds else None
    event_kinds = _parse_event_kinds(kinds or [])

    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    try:
        cursor = since_event_id
        replay_kwargs: dict[str, Any] = {"after_event_id": cursor, "limit": page}
        if contract_id is not None:
            replay_kwargs["contract_id"] = contract_id
        replay = get_events(conn, **replay_kwargs)

        if replay:
            label = "[watch]"
            color_label = "\033[36m[watch]\033[0m" if _COLOR_ENABLED else label
            print(f"{color_label} replaying {len(replay)} events from state.db")
        _emit_batch(replay)
        if replay:
            cursor = replay[-1].event_id

        if not follow:
            return cursor or 0

        last_id = cursor if cursor else 0
        next_heartbeat = time.monotonic() + DEFAULT_FOLLOW_INTERVAL
        while True:
            now = datetime.now(UTC)
            if deadline and now >= deadline:
                break
            batch = get_events(conn, after_event_id=last_id, limit=page)
            keep = [
                e for e in batch if _matches_filter(e, executor_id=executor_id, kinds=event_kinds)
            ]
            if keep:
                _emit_batch(keep)
                last_id = keep[-1].event_id
                sys.stdout.flush()
                next_heartbeat = time.monotonic() + DEFAULT_FOLLOW_INTERVAL
            elif time.monotonic() >= next_heartbeat:
                _emit_idle(datetime.now(UTC))
                sys.stdout.flush()
                next_heartbeat = time.monotonic() + DEFAULT_FOLLOW_INTERVAL
            time.sleep(DEFAULT_FOLLOW_INTERVAL)
        return last_id
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """`longtask watch` CLI 入口（由 cli/main.py 委派）。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="longtask watch",
        description="Tail protocol events (read-only; works even if daemon is offline)",
    )
    parser.add_argument("--data-dir", type=str, default=None)
    parser.add_argument("--contract", type=str, default=None, help="filter by contract id")
    parser.add_argument(
        "--executor", type=str, default=None, help="filter by executor id (in payload)"
    )
    parser.add_argument(
        "--since",
        type=int,
        default=None,
        help="start after this event_id (default: tail from latest)",
    )
    parser.add_argument(
        "--kinds",
        type=str,
        default=None,
        help="comma-separated event types to include (e.g. 'contract/*,attempt/*')",
    )
    parser.add_argument(
        "--for",
        type=int,
        default=None,
        dest="duration",
        help="stop after N seconds (default: until Ctrl-C)",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="continuously tail new events (default: replay then exit)",
    )
    args = parser.parse_args(argv)

    root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
    if not (root / "state.db").is_file():
        print(
            f"longtask state.db not found at {root / 'state.db'}; nothing to watch", file=sys.stderr
        )
        return 1

    kinds = [s.strip() for s in args.kinds.split(",") if s.strip()] if args.kinds else None
    try:
        return watch(
            root,
            contract_id=args.contract,
            executor_id=args.executor,
            follow=args.follow,
            since_event_id=args.since,
            duration_seconds=args.duration,
            kinds=kinds,
        )
    except ValueError as exc:
        # 非法 --kinds / 过滤参数：报错退出，绝不静默退化成「全放行」。
        print(f"longtask watch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
