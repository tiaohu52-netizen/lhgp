"""文件投影与交接解析集成测试（DESIGN §3.1、§11.3、§13.3、§14.1）。

测试覆盖：
1. rebuild_projection 从权威库生成全部 7 个投影文件；
2. log.jsonl 包含全部已提交事件记录；
3. lease.json 反映当前活跃租约及无租约状态；
4. check_projection_dirty 人类草稿改动检测与 revert_projection 强制回滚；
5. HandoverData 最低必填区块校验（缺块拒收、负工时拒收、格式化往返解析）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.events import EventType
from longtask.persistence.projections import (
    CONTRACT_FILE,
    FINDINGS_FILE,
    HANDOVER_FILE,
    LEASE_FILE,
    LOG_FILE,
    PROGRESS_FILE,
    TASK_PLAN_FILE,
    HandoverData,
    check_projection_dirty,
    parse_handover_markdown,
    rebuild_projection,
    revert_projection,
)
from longtask.persistence.store import (
    StoreConfig,
    acquire_lease,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)


def make_draft(title: str = "投影测试合同") -> ContractDraft:
    return ContractDraft(
        title=title,
        objective="测试物化 contract.yaml / lease.json / log.jsonl",
        deadline_at=LATER,
        hard_constraints={
            "file_effects": {"mode": "workspace-write"},
            "network": {"mode": "deny"},
            "process": {"mode": "restricted"},
        },
        acceptance=Acceptance(
            standard="全部投影文件可物化且可通过解析",
            checks=("文件完整", "状态同步"),
            verifier="cross_check",
        ),
        workload_initial_hours=5.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=60,
            max_output_bytes=1048576,
        ),
        soft_guidance={"style": "清晰紧凑"},
        context={"required": False},
        execution={"allowed_control": ["notify"]},
    )


def setup_db(tmp_path: Path) -> tuple[StoreConfig, Path]:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "state.db"
    cfg = StoreConfig(db_path=db_path)
    conn = connect(cfg)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return cfg, tmp_path / "root"


class TestProjections:
    def test_rebuild_projection_all_files(self, tmp_path: Path) -> None:
        cfg, root = setup_db(tmp_path)
        conn = connect(cfg)
        try:
            cid = "lt-proj-001"
            save_contract(conn, make_draft(), contract_id=cid, now=NOW)
            acquire_lease(
                conn,
                contract_id=cid,
                holder_attempt_id="att-1",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
            )
            append_event(
                conn,
                contract_id=cid,
                event_type=EventType.ATTEMPT_STARTED,
                payload={"attempt_id": "att-1"},
                now=NOW + timedelta(seconds=5),
            )

            paths = rebuild_projection(root, cid, conn)
            assert len(paths) == 7

            for fname in (
                CONTRACT_FILE,
                LEASE_FILE,
                LOG_FILE,
                HANDOVER_FILE,
                TASK_PLAN_FILE,
                PROGRESS_FILE,
                FINDINGS_FILE,
            ):
                assert fname in paths
                assert paths[fname].is_file()

            # 1. contract.yaml 校验
            c_data = json.loads(paths[CONTRACT_FILE].read_text(encoding="utf-8"))
            assert c_data["contract_id"] == cid
            assert c_data["state"] == "drafted"
            assert c_data["title"] == "投影测试合同"

            # 2. lease.json 校验
            l_data = json.loads(paths[LEASE_FILE].read_text(encoding="utf-8"))
            assert l_data["contract_id"] == cid
            assert l_data["generation"] == 1
            assert l_data["holder_attempt_id"] == "att-1"

            # 3. log.jsonl 校验
            log_lines = paths[LOG_FILE].read_text(encoding="utf-8").strip().splitlines()
            assert len(log_lines) == 3  # contract/prepared, lease/acquired, attempt/started
            parsed_events = [json.loads(line) for line in log_lines]
            assert parsed_events[0]["event_type"] == "contract/prepared"
            assert parsed_events[1]["event_type"] == "lease/acquired"
            assert parsed_events[2]["event_type"] == "attempt/started"

            # 4. handover.md 校验
            h_text = paths[HANDOVER_FILE].read_text(encoding="utf-8")
            h_data, errors = parse_handover_markdown(h_text)
            assert errors == []
            assert h_data is not None
            assert h_data.estimate_remaining_hours == 5.0
        finally:
            conn.close()

    def test_dirty_draft_detection_and_revert(self, tmp_path: Path) -> None:
        cfg, root = setup_db(tmp_path)
        conn = connect(cfg)
        try:
            cid = "lt-proj-002"
            save_contract(conn, make_draft(), contract_id=cid, now=NOW)
            paths = rebuild_projection(root, cid, conn)

            view = get_contract(conn, cid)
            assert view is not None
            assert not check_projection_dirty(root, cid, view)

            # 用户直接修改盘上 contract.yaml
            c_path = paths[CONTRACT_FILE]
            raw = json.loads(c_path.read_text(encoding="utf-8"))
            raw["title"] = "用户未提交的草稿改动"
            c_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

            # 触发 dirty 检测
            assert check_projection_dirty(root, cid, view)

            # 丢弃草稿强制 revert
            revert_projection(root, cid, conn)
            assert not check_projection_dirty(root, cid, view)
            reverted = json.loads(c_path.read_text(encoding="utf-8"))
            assert reverted["title"] == "投影测试合同"
        finally:
            conn.close()

    def test_handover_validation_and_parsing(self) -> None:
        valid = HandoverData(
            current_stage="stage-1",
            completed_evidence=("证据 1", "证据 2"),
            remaining=("未完成 1",),
            estimate_remaining_hours=2.5,
            next_action="执行第一步",
            constraints_digest="deny: ~/other",
            source_attempt_id="att-99",
            open_risks=("风险 A",),
        )
        md_text = valid.format_markdown()
        parsed, errors = parse_handover_markdown(md_text)
        assert errors == []
        assert parsed == valid

        # 缺少必填区块
        bad_md = "## current_stage\nstage-1\n"
        parsed_bad, bad_errors = parse_handover_markdown(bad_md)
        assert parsed_bad is None
        assert any("completed_evidence" in err for err in bad_errors)
        assert any("source_attempt_id" in err for err in bad_errors)

        # 负工时
        bad_hours_md = valid.format_markdown().replace("2.5", "-1.0")
        _, hour_errors = parse_handover_markdown(bad_hours_md)
        assert any("non-negative" in err for err in hour_errors)
