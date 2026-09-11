"""Self-publishing wiki tests: render active contracts to docs/wiki/auto/.

The publish path is the only writer to ``<wiki_root>/auto/`` and it must
stay non-destructive: re-runs overwrite the same file (no duplicates),
hand-written pages elsewhere in the wiki are not touched, and terminal
contracts only get re-rendered once their existing page is older than the
banner threshold.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.contract_view import ContractState
from lhgp.contracts.state_machine import NON_TERMINAL_STATES
from lhgp.memory.store import record_memory
from lhgp.memory.types import Memory, MemoryKind, MemoryScope
from lhgp.wiki import publish_active_contracts
from lhgp.wiki.sync import AUTO_SUBDIR, TERMINAL_BANNER_DAYS
from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _draft(deadline: datetime | None = None) -> ContractDraft:
    return ContractDraft(
        title="Sample contract",
        objective="Make sure publish_active_contracts writes a Markdown page.",
        deadline_at=deadline if deadline is not None else NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="docstring is the truth", checks=("present",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=2,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1024,
        ),
        context={"tags": ["topic/wiki"]},
    )


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "data" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    return conn


def _backdate(path: Path, *, days_old: int, ref: datetime) -> None:
    """Force the file's mtime to ``ref - days_old`` so terminal-banner
    freshness checks see it as stale without time.sleep."""
    target = ref - timedelta(days=days_old)
    os.utime(str(path), (target.timestamp(), target.timestamp()))


class TestEmpty:
    def test_no_active_contracts_writes_nothing(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            written = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        assert written == []
        # No pages — even if the auto/ directory was pre-created, no file
        # under it should exist. Empty contract set means nothing rendered.
        auto = wiki_root / AUTO_SUBDIR
        if auto.exists():
            assert list(auto.iterdir()) == []


class TestActiveContract:
    def test_writes_one_file_with_expected_frontmatter(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            save_contract(
                conn, _draft(), contract_id="lt-active-1", now=NOW, state=ContractState.ACTIVE
            )
            written = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()

        assert [p.name for p in written] == ["lt-active-1.md"]
        page = (wiki_root / AUTO_SUBDIR / "lt-active-1.md").read_text(encoding="utf-8")
        # Frontmatter gates the indexer — keep these lines in lockstep
        # with the keys scripts/build_wiki_index.py knows how to parse.
        assert page.startswith("---\n")
        assert "type: contract-page\n" in page
        assert "id: lt-active-1\n" in page
        assert "status: active\n" in page
        # Sections the indexer treats as H2 headings
        for section in (
            "## objective",
            "## status",
            "## acceptance",
            "## deadline",
            "## next_action",
            "## memory",
            "## related",
        ):
            assert section in page, f"missing section {section!r}"
        # No terminal banner on an active contract
        assert "TERMINAL" not in page

    def test_deadline_overdue_announces_minutes(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            deadline = NOW - timedelta(minutes=42)
            save_contract(
                conn,
                _draft(deadline=deadline),
                contract_id="lt-breach",
                now=NOW - timedelta(hours=2),
                state=ContractState.ACTIVE,
            )
            publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        page = (wiki_root / AUTO_SUBDIR / "lt-breach.md").read_text(encoding="utf-8")
        # The header must show how many minutes the contract is past due.
        match = re.search(r"已超期 (\d+) 分钟", page)
        assert match is not None
        assert int(match.group(1)) >= 1


class TestMemorySection:
    def test_memory_section_lists_every_linked_memory(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            save_contract(
                conn, _draft(), contract_id="lt-mem-1", now=NOW, state=ContractState.ACTIVE
            )
            record_memory(
                conn,
                Memory(
                    scope=MemoryScope.PROJECT,
                    kind=MemoryKind.PATTERN,
                    title="wiki frontmatter schema",
                    body_md="contract-page needs type / id / status / audience",
                    tags=("topic/wiki",),
                    source_contract_id="lt-mem-1",
                    score=0.8,
                ),
            )
            record_memory(
                conn,
                Memory(
                    scope=MemoryScope.DOMAIN,
                    kind=MemoryKind.GOTCHA,
                    title="obsidian re-export gotcha",
                    body_md="backlink graph is computed at index time, not at write time",
                    tags=("topic/obsidian",),
                    source_contract_id="lt-mem-1",
                    score=0.5,
                ),
            )
            # A memory linked to a *different* contract must not leak in.
            record_memory(
                conn,
                Memory(
                    scope=MemoryScope.PROJECT,
                    kind=MemoryKind.PATTERN,
                    title="unrelated",
                    body_md="other contract",
                    source_contract_id="lt-other",
                    score=0.9,
                ),
            )
            publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()

        page = (wiki_root / AUTO_SUBDIR / "lt-mem-1.md").read_text(encoding="utf-8")
        memory_section = page.split("## memory", 1)[1].split("## related", 1)[0]
        assert "wiki frontmatter schema (pattern, project, score=0.80)" in memory_section
        assert "obsidian re-export gotcha (gotcha, domain, score=0.50)" in memory_section
        assert "unrelated" not in memory_section

    def test_no_memories_renders_helpful_placeholder(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            save_contract(
                conn, _draft(), contract_id="lt-nomem", now=NOW, state=ContractState.ACTIVE
            )
            publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        page = (wiki_root / AUTO_SUBDIR / "lt-nomem.md").read_text(encoding="utf-8")
        assert "no memories linked" in page


class TestTerminalContracts:
    def _save_terminal(
        self,
        conn: sqlite3.Connection,
        *,
        contract_id: str,
        state: ContractState,
    ) -> None:
        save_contract(conn, _draft(), contract_id=contract_id, now=NOW, state=ContractState.ACTIVE)
        if state == ContractState.ARCHIVED:
            # 状态机（审计 B1）：ACTIVE 没有到 archived 的边（表里只允许
            # blocked/expired→archived），fixture 补上真实中间态，而不是直写非法边。
            update_contract_state(
                conn, contract_id=contract_id, new_state=ContractState.BLOCKED, now=NOW
            )
        update_contract_state(conn, contract_id=contract_id, new_state=state, now=NOW)

    def test_stale_terminal_page_is_rerendered_with_banner(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        auto = wiki_root / AUTO_SUBDIR
        try:
            self._save_terminal(conn, contract_id="lt-old", state=ContractState.SATISFIED)
            stale = auto / "lt-old.md"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("PLACEHOLDER", encoding="utf-8")
            _backdate(stale, days_old=TERMINAL_BANNER_DAYS + 2, ref=NOW)
            written = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        assert stale in written
        text = stale.read_text(encoding="utf-8")
        assert "TERMINAL" in text
        assert "satisfied" in text
        assert "PLACEHOLDER" not in text

    def test_fresh_terminal_page_is_not_touched(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        auto = wiki_root / AUTO_SUBDIR
        try:
            self._save_terminal(conn, contract_id="lt-fresh", state=ContractState.CANCELLED)
            fresh = auto / "lt-fresh.md"
            fresh.parent.mkdir(parents=True, exist_ok=True)
            fresh.write_text("FRESH PLACEHOLDER", encoding="utf-8")
            _backdate(fresh, days_old=1, ref=NOW)
            written = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        assert fresh not in written
        assert fresh.read_text(encoding="utf-8") == "FRESH PLACEHOLDER"

    def test_terminal_without_existing_page_is_not_created(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            self._save_terminal(conn, contract_id="lt-newdone", state=ContractState.ARCHIVED)
            written = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        # No page for lt-newdone — the publisher never re-introduces a
        # terminal contract that has been archived without ever being
        # published while active.
        assert all(p.name != "lt-newdone.md" for p in written)
        assert not (wiki_root / AUTO_SUBDIR / "lt-newdone.md").exists()


class TestIdempotency:
    def test_rerun_overwrites_without_duplicating(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            save_contract(
                conn, _draft(), contract_id="lt-idem", now=NOW, state=ContractState.ACTIVE
            )
            first = publish_active_contracts(conn, wiki_root, now=NOW)
            second = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        assert [p.name for p in first] == ["lt-idem.md"]
        assert [p.name for p in second] == ["lt-idem.md"]
        auto = wiki_root / AUTO_SUBDIR
        assert sorted(p.name for p in auto.iterdir()) == ["lt-idem.md"]

    def test_does_not_create_files_outside_auto_dir(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        # Drop a hand-written page into the wiki tree. It must survive
        # a publish pass untouched.
        hand = wiki_root / "playbook"
        hand.mkdir(parents=True, exist_ok=True)
        guard = hand / "human-curated.md"
        guard.write_text("# human\n", encoding="utf-8")
        try:
            save_contract(conn, _draft(), contract_id="lt-iso", now=NOW, state=ContractState.ACTIVE)
            publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        assert guard.read_text(encoding="utf-8") == "# human\n"
        # No leakage to the wiki root itself
        assert not (wiki_root / "lt-iso.md").exists()


class TestRelatedContracts:
    """``## related`` lists other contracts that share a memory
    ``topic/<domain>`` tag. Two contracts are linked when their
    memories tag the same domain; the page surfaces up to 5 siblings
    so a reader scanning one contract can pivot to peers working
    on the same domain."""

    def test_related_lists_contracts_sharing_a_topic_tag(self, tmp_path: Path) -> None:
        from lhgp.memory import Memory, MemoryKind, MemoryScope, record_memory

        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            now = datetime.now(UTC)
            base = now - timedelta(hours=2)
            save_contract(
                conn, _draft(), contract_id="lt-rel-a", now=base, state=ContractState.ACTIVE
            )
            save_contract(
                conn, _draft(), contract_id="lt-rel-b", now=base, state=ContractState.ACTIVE
            )

            m_a = Memory(
                scope=MemoryScope.DOMAIN,
                kind=MemoryKind.PATTERN,
                title="shared-pattern",
                body_md="x",
                tags=("topic/foo",),
                source_contract_id="lt-rel-a",
                created_at=base,
                schema_version=4,
            )
            m_b = Memory(
                scope=MemoryScope.DOMAIN,
                kind=MemoryKind.PATTERN,
                title="shared-pattern",
                body_md="x",
                tags=("topic/foo",),
                source_contract_id="lt-rel-b",
                created_at=base,
                schema_version=4,
            )
            record_memory(conn, m_a)
            record_memory(conn, m_b)
            conn.commit()
            publish_active_contracts(conn, wiki_root, now=now)
        finally:
            conn.close()

        page_a = (wiki_root / AUTO_SUBDIR / "lt-rel-a.md").read_text(encoding="utf-8")
        page_b = (wiki_root / AUTO_SUBDIR / "lt-rel-b.md").read_text(encoding="utf-8")
        assert "[[lt-rel-b]]" in page_a
        assert "[[lt-rel-a]]" in page_b
        # The two contracts do not list themselves.
        assert "[[lt-rel-a]]" not in page_a
        assert "[[lt-rel-b]]" not in page_b

    def test_related_falls_back_to_placeholder_when_no_topic_tags(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            save_contract(
                conn, _draft(), contract_id="lt-rel-empty", now=NOW, state=ContractState.ACTIVE
            )
            publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        page = (wiki_root / AUTO_SUBDIR / "lt-rel-empty.md").read_text(encoding="utf-8")
        assert "_(no related contracts)_" in page


class TestStateAxis:
    def test_publishes_every_non_terminal_state(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            for st in NON_TERMINAL_STATES:
                save_contract(
                    conn,
                    _draft(),
                    contract_id=f"lt-{st.value}",
                    now=NOW,
                    state=st,
                )
            written = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        written_names = {p.name for p in written}
        for st in NON_TERMINAL_STATES:
            assert f"lt-{st.value}.md" in written_names


class TestPathSafety:
    """`contract_id` is concatenated into the page path. A malicious id
    with ``..`` segments or path separators must not let the page
    escape ``auto/`` (or the wiki root entirely). The publisher
    normalises to a slug and asserts the resolved path is under
    ``auto_dir``; a contract that would otherwise escape is logged
    and skipped."""

    @pytest.mark.parametrize(
        "malicious_id",
        [
            "../../../etc/passwd",
            "..\\..\\..\\evil",
            "subdir/inside",
            "name with space",
            "../auto-collision",
        ],
    )
    def test_malicious_contract_id_does_not_escape(
        self, tmp_path: Path, malicious_id: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        conn = _open_db(tmp_path)
        wiki_root = tmp_path / "wiki"
        try:
            save_contract(
                conn, _draft(), contract_id=malicious_id, now=NOW, state=ContractState.ACTIVE
            )
            with caplog.at_level("WARNING"):
                written = publish_active_contracts(conn, wiki_root, now=NOW)
        finally:
            conn.close()
        # No file lives outside auto_dir — the slug normalisation
        # replaces path separators with underscores and the
        # resolved-path assertion rejects any slug that still escapes
        # (defence in depth). Two dots in the middle of a filename
        # are not path traversal — those are just two characters
        # inside a single filename segment.
        auto_dir = wiki_root / AUTO_SUBDIR
        for p in wiki_root.rglob("*.md"):
            assert p.resolve().is_relative_to(auto_dir.resolve()), f"file {p} escaped auto_dir"
        for p in written:
            assert p.resolve().is_relative_to(auto_dir.resolve())
            assert "/" not in p.name
            assert "\\" not in p.name
