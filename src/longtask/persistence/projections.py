"""文件投影与交接规范（DESIGN §3.1、§11.3、§13.3、§14.1）。

contract.yaml / log.jsonl / lease.json / handover.md 等文件都是投影：
由权威数据库在事务提交后物化，可落后可重建，绝不超前（DESIGN §3.1）。
盘上的直接修改视为未提交草稿（dirty），需经显式 patch/promote 才能进入权威库。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from longtask.contracts.schema import ContractView
from longtask.persistence.store import get_contract, get_events, get_lease

# DESIGN §3.1 合同目录布局（相对 ~/.longtask/contracts/<contract-id>/）
CONTRACT_FILE = "contract.yaml"
HANDOVER_FILE = "handover.md"
TASK_PLAN_FILE = "task_plan.md"
PROGRESS_FILE = "progress.md"
FINDINGS_FILE = "findings.md"
LEASE_FILE = "lease.json"
LOG_FILE = "log.jsonl"


def _format_markdown_list(items: tuple[str, ...]) -> str:
    """辅助格式化列表区块（DESIGN §3.1）。"""
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)


@dataclass(frozen=True, slots=True)
class HandoverData:
    """交接文件最低必填结构（DESIGN §3.1 handover.md）。

    下一个人凭此无缝续跑，不得写成自由散文。
    """

    current_stage: str
    completed_evidence: tuple[str, ...]
    remaining: tuple[str, ...]
    estimate_remaining_hours: float
    next_action: str
    constraints_digest: str
    source_attempt_id: str
    open_risks: tuple[str, ...] = ()

    def format_markdown(self) -> str:
        """格式化为结构化 handover.md 文本（DESIGN §3.1）。"""
        ev_lines = _format_markdown_list(self.completed_evidence)
        rem_lines = _format_markdown_list(self.remaining)
        risk_lines = _format_markdown_list(self.open_risks)

        return (
            f"# Handover State\n\n"
            f"## current_stage\n{self.current_stage.strip()}\n\n"
            f"## source_attempt_id\n{self.source_attempt_id.strip()}\n\n"
            f"## estimate_remaining_hours\n{self.estimate_remaining_hours}\n\n"
            f"## next_action\n{self.next_action.strip()}\n\n"
            f"## constraints_digest\n{self.constraints_digest.strip()}\n\n"
            f"## completed_evidence\n{ev_lines}\n\n"
            f"## remaining\n{rem_lines}\n\n"
            f"## open_risks\n{risk_lines}\n"
        )


def parse_handover_markdown(content: str) -> tuple[HandoverData | None, list[str]]:
    """解析并校验 handover.md 最低必填字段（DESIGN §3.1）。

    返回 (HandoverData | None, list_of_missing_or_invalid_sections)。
    """
    errors: list[str] = []
    sections: dict[str, str] = {}
    current_sec: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        header_match = re.match(r"^##\s+([a-zA-Z0-9_]+)", line.strip())
        if header_match:
            if current_sec is not None:
                sections[current_sec] = "\n".join(current_lines).strip()
            current_sec = header_match.group(1)
            current_lines = []
        elif current_sec is not None:
            current_lines.append(line)

    if current_sec is not None:
        sections[current_sec] = "\n".join(current_lines).strip()

    # 必填项核对
    for required in (
        "current_stage",
        "completed_evidence",
        "remaining",
        "estimate_remaining_hours",
        "next_action",
        "constraints_digest",
        "source_attempt_id",
    ):
        if required not in sections or not sections[required]:
            errors.append(f"missing required section: {required}")

    raw_hours = sections.get("estimate_remaining_hours", "")
    hours: float = 0.0
    try:
        hours = float(raw_hours)
        if hours < 0:
            errors.append(f"estimate_remaining_hours must be non-negative, got {hours}")
    except ValueError:
        errors.append(f"invalid estimate_remaining_hours: '{raw_hours}'")

    if errors:
        return None, errors

    def _parse_list(raw: str) -> tuple[str, ...]:
        items: list[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("- ") or s.startswith("* "):
                item = s[2:].strip()
                if item and item != "(none)":
                    items.append(item)
            elif s and s != "(none)":
                items.append(s)
        return tuple(items)

    completed_evidence = _parse_list(sections.get("completed_evidence", ""))
    remaining = _parse_list(sections.get("remaining", ""))
    open_risks = _parse_list(sections.get("open_risks", ""))

    data = HandoverData(
        current_stage=sections.get("current_stage", ""),
        completed_evidence=completed_evidence,
        remaining=remaining,
        estimate_remaining_hours=hours,
        next_action=sections.get("next_action", ""),
        constraints_digest=sections.get("constraints_digest", ""),
        source_attempt_id=sections.get("source_attempt_id", ""),
        open_risks=open_risks,
    )
    return data, []


def contract_dir(root: Path, contract_id: str) -> Path:
    """合同目录路径。调用方负责 root 已归一化（DESIGN §14.1 路径穿越防线）。"""
    return root / "contracts" / contract_id


def format_contract_yaml(view: ContractView) -> str:
    """生成人类与模型可读的 contract.yaml 内容（DESIGN §4、§11.6）。"""
    data = view.to_dict()
    # 纯标准库输出干净可读的伪 YAML / JSON 结构
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def format_lease_json(lease_obj: Any) -> str:
    """生成 lease.json 投影内容（DESIGN §3.1、§7）。"""
    if lease_obj is None:
        return json.dumps({"active_lease": None}, ensure_ascii=False, indent=2) + "\n"
    return (
        json.dumps(
            {
                "contract_id": lease_obj.contract_id,
                "holder_attempt_id": lease_obj.holder_attempt_id,
                "generation": lease_obj.generation,
                "heartbeat_at": lease_obj.heartbeat_at.isoformat(),
                "timeout_seconds": lease_obj.timeout.total_seconds(),
                "partition_id": lease_obj.partition_id,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def check_projection_dirty(root: Path, contract_id: str, current_view: ContractView) -> bool:
    """检测盘上 contract.yaml 是否被外部直接修改（DESIGN §3.1 人类编辑门）。

    若盘上文件存在且内容与当前权威数据库序列化不一致，判定为 dirty 草稿。
    """
    path = contract_dir(root, contract_id) / CONTRACT_FILE
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8")
        parsed = json.loads(content)
        authoritative = json.loads(format_contract_yaml(current_view))
        return bool(parsed != authoritative)
    except (OSError, json.JSONDecodeError):
        return True


def _atomic_write(path: Path, content: str) -> None:
    """temp + os.replace 的原子写入：crash 不会留下截断文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def rebuild_projection(
    root: Path,
    contract_id: str,
    conn: sqlite3.Connection,
) -> dict[str, Path]:
    """从权威库事件与当前状态重建全部文件投影（DESIGN §3.1、§11.3）。

    物化生成：
    - contract.yaml
    - lease.json
    - log.jsonl
    - handover.md（若不存在则初始化骨架）
    - task_plan.md / progress.md / findings.md（若不存在则初始化骨架）
    """
    view = get_contract(conn, contract_id)
    if view is None:
        raise ValueError(f"cannot rebuild projection: contract '{contract_id}' not found")

    cdir = contract_dir(root, contract_id)
    cdir.mkdir(parents=True, exist_ok=True)

    created_paths: dict[str, Path] = {}

    # 1. 物化 contract.yaml
    contract_path = cdir / CONTRACT_FILE
    _atomic_write(contract_path, format_contract_yaml(view))
    created_paths[CONTRACT_FILE] = contract_path

    # 2. 物化 lease.json
    lease_obj = get_lease(conn, contract_id)
    lease_path = cdir / LEASE_FILE
    _atomic_write(lease_path, format_lease_json(lease_obj))
    created_paths[LEASE_FILE] = lease_path

    # 3. 物化 log.jsonl
    events = get_events(conn, contract_id=contract_id)
    log_path = cdir / LOG_FILE
    log_lines = [
        json.dumps(
            {
                "event_id": e.event_id,
                "contract_id": e.contract_id,
                "attempt_id": e.attempt_id,
                "lease_generation": e.lease_generation,
                "event_type": e.event_type,
                "payload": json.loads(e.payload_json),
                "request_id": e.request_id,
                "created_at": e.created_at.isoformat(),
                "actor": e.actor,
                "schema_version": e.schema_version,
            },
            ensure_ascii=False,
        )
        for e in events
    ]
    _atomic_write(log_path, "\n".join(log_lines) + ("\n" if log_lines else ""))
    created_paths[LOG_FILE] = log_path

    # 4. 初始化 handover.md / task_plan.md / progress.md / findings.md
    handover_path = cdir / HANDOVER_FILE
    if not handover_path.is_file():
        default_handover = HandoverData(
            current_stage="initial",
            completed_evidence=(),
            remaining=(view.draft.objective.strip(),),
            estimate_remaining_hours=view.draft.workload_initial_hours,
            next_action="初始化计划并开始执行",
            constraints_digest=json.dumps(view.draft.hard_constraints, ensure_ascii=False),
            source_attempt_id="init",
            open_risks=(),
        )
        _atomic_write(handover_path, default_handover.format_markdown())
    created_paths[HANDOVER_FILE] = handover_path

    created_at_iso = view.created_at.isoformat()
    init_headers = (
        (TASK_PLAN_FILE, f"# Task Plan: {view.draft.title}\n\n- Stage 1: Initial Implementation\n"),
        (
            PROGRESS_FILE,
            f"# Progress Log: {contract_id}\n\n"
            f"- [{created_at_iso}] Contract initialized in state '{view.state.value}'.\n",
        ),
        (FINDINGS_FILE, f"# Findings: {contract_id}\n\n(No findings recorded yet.)\n"),
    )
    for f_name, header in init_headers:
        p = cdir / f_name
        if not p.is_file():
            _atomic_write(p, header)
        created_paths[f_name] = p

    return created_paths


def revert_projection(
    root: Path,
    contract_id: str,
    conn: sqlite3.Connection,
) -> dict[str, Path]:
    """丢弃盘上草稿改动，以权威库状态为准强制重建投影（DESIGN §3.1 人类编辑门）。"""
    return rebuild_projection(root, contract_id, conn)
