# ruff: noqa: E501  # HTML template lines are long by design
"""Contract timeline: render event history as a self-contained HTML page.

Inspired by archify's "generate → validate → deliver" pipeline, but
dependency-free: pure Python string templating into one HTML file that
works offline in any browser.  No JS framework, no external assets.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime
from typing import Any

from lhgp.persistence.events_query import get_events
from lhgp.persistence.store import get_contract

# 事件类型 → 视觉分类（颜色 + 图标语义）
_EVENT_KIND = {
    "contract/prepared": ("drafted", "#6b7280"),
    "contract/approved": ("active", "#2563eb"),
    "contract/started": ("active", "#2563eb"),
    "attempt/admitted": ("dispatch", "#7c3aed"),
    "attempt/started": ("dispatch", "#7c3aed"),
    "attempt/succeeded": ("success", "#16a34a"),
    "attempt/failed": ("failure", "#dc2626"),
    "attempt/cancelled": ("cancelled", "#9ca3af"),
    "attempt/stale": ("warning", "#d97706"),
    "attempt/orphaned": ("warning", "#d97706"),
    "lease/acquired": ("lease", "#0891b2"),
    "lease/renewed": ("lease", "#0891b2"),
    "lease/released": ("lease", "#0891b2"),
    "verification/requested": ("verify", "#9333ea"),
    "verification/consumed": ("verify", "#9333ea"),
    "verification/started": ("verify", "#9333ea"),
    "contract/completed": ("success", "#16a34a"),
    "contract/satisfied": ("success", "#16a34a"),
    "contract/expired": ("failure", "#dc2626"),
    "contract/blocked": ("warning", "#d97706"),
    "escalation/reminded": ("escalation", "#d97706"),
    "escalation/steered": ("escalation", "#d97706"),
    "escalation/spawned": ("escalation", "#d97706"),
    "escalation/handed-to-user": ("escalation", "#dc2626"),
    "reconcile/reattached": ("recovery", "#0891b2"),
    "reconcile/collected": ("recovery", "#0891b2"),
    "dispatch/deferred": ("warning", "#d97706"),
    "dispatch/refused": ("failure", "#dc2626"),
}


def _event_kind(event_type: str) -> tuple[str, str]:
    return _EVENT_KIND.get(event_type, ("other", "#6b7280"))


def build_timeline_html(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    now: datetime,
) -> tuple[str, str | None]:
    """渲染合同事件时间轴为自包含 HTML。

    Returns:
        (html_content, error) — error 非 None 表示合同不存在。
    """
    contract = get_contract(conn, contract_id)
    if contract is None:
        return "", f"contract {contract_id!r} not found"

    events = get_events(conn, contract_id=contract_id)
    rows: list[dict[str, Any]] = []
    for e in events:
        kind, color = _event_kind(str(e.event_type))
        try:
            payload = json.loads(e.payload_json or "{}")
        except ValueError:
            payload = {}
        summary = payload.get("reason") or payload.get("note") or ""
        if isinstance(summary, dict):
            summary = json.dumps(summary, ensure_ascii=False)
        rows.append(
            {
                "event_id": e.event_id,
                "at": e.created_at.isoformat() if e.created_at else "",
                "type": str(e.event_type),
                "kind": kind,
                "color": color,
                # actor / attempt_id 也来自事件流（actor 可由 RPC 客户端决定），
                # 它们随后被拼进 innerHTML——只转义 summary 会让 actor 里的
                # ``<img onerror=...>`` 直接执行。凡是进 DOM 的字符串一律转义。
                "actor": html.escape(e.actor or ""),
                "attempt_id": html.escape(e.attempt_id or ""),
                "summary": html.escape(str(summary)[:160]),
            }
        )

    # 生成 JSON 数据嵌入 HTML（无需服务端）。
    # ``</`` 必须转义成 ``<\/``：否则任何字段里出现 ``</script>`` 就能提前
    # 闭合宿主 script 标签，把后面的内容当 HTML 解析（XSS）。``\/`` 在 JSON
    # 与 JS 字符串里都是合法转义，语义不变。
    data_json = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(f"{contract_id} — {contract.draft.title}")
    state = html.escape(contract.state.value)
    deadline = html.escape(contract.draft.deadline_at.isoformat())
    generated = html.escape(now.isoformat())

    page = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 2rem; }}
  h1 {{ font-size: 1.3rem; }}
  .meta {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  .timeline {{ border-left: 2px solid #334155; margin-left: 8px; padding-left: 1.5rem; }}
  .entry {{ margin-bottom: 0.6rem; position: relative; }}
  .entry::before {{ content: ''; position: absolute; left: -1.72rem; top: 0.45em; width: 9px; height: 9px; border-radius: 50%; background: var(--c); }}
  .type {{ font-weight: 600; }}
  .detail {{ color: #94a3b8; font-size: 0.82rem; }}
  .filter {{ margin-bottom: 1rem; }}
  .filter button {{ background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 4px; padding: 2px 10px; cursor: pointer; margin-right: 4px; font-size: 0.8rem; }}
  .filter button.active {{ background: #2563eb; border-color: #2563eb; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">state: {state} &nbsp;|&nbsp; deadline: {deadline} &nbsp;|&nbsp; generated: {generated} &nbsp;|&nbsp; {len(rows)} events</div>
<div class="filter" id="filters"></div>
<div class="timeline" id="tl"></div>
<script>
const DATA = {data_json};
const TL = document.getElementById('tl');
const FILTERS = document.getElementById('filters');
function render(filter) {{
  TL.innerHTML = '';
  for (const e of DATA) {{
    if (filter && filter !== 'all' && e.kind !== filter) continue;
    const div = document.createElement('div');
    div.className = 'entry';
    div.style.setProperty('--c', e.color);
    div.innerHTML = `<span class="type" style="color:${{e.color}}">${{e.type}}</span>` +
      ` <span class="detail">${{e.at}} · ${{e.actor}}${{e.attempt_id ? ' · ' + e.attempt_id : ''}}</span>` +
      (e.summary ? `<div class="detail">${{e.summary}}</div>` : '');
    TL.appendChild(div);
  }}
}}
const kinds = [...new Set(DATA.map(e => e.kind))];
FILTERS.innerHTML = '<button class="active" onclick="render(null);this.classList.add(\\'active\\');[...this.parentNode.children].forEach(b=>b!==this&&b.classList.remove(\\'active\\'))">all</button>' +
  kinds.map(k => `<button onclick="render('${{k}}');this.classList.add('active');[...this.parentNode.children].forEach(b=>b!==this&&b.classList.remove('active'))">${{k}}</button>`).join('');
render(null);
</script>
</body>
</html>"""
    return page, None


__all__ = ["build_timeline_html"]
