"""P3: wiki ``## flow`` section reader.

The parser is small but worth pinning because:
  - it defines the on-disk convention (any wiki page can opt in)
  - it has to ignore hidden files and bad encodings without crashing
  - it must report the right line number (for ``lhgp flow wiki`` to
    point at the section it printed)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lhgp.flow.wiki_parser import extract_flow_section, list_flow_pages

pytestmark = pytest.mark.unit


class TestExtractSection:
    def test_returns_section_for_valid_page(self) -> None:
        md = "# Foo\nintro\n\n## flow\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
        sec = extract_flow_section(md, page="foo.md")
        assert sec is not None
        assert sec.page == "foo.md"
        assert sec.line == 4
        assert "A --> B" in sec.mermaid
        assert "flowchart TD" in sec.mermaid

    def test_returns_none_when_no_flow_heading(self) -> None:
        assert extract_flow_section("# no flow here") is None
        assert extract_flow_section("") is None

    def test_returns_none_when_no_mermaid_fence(self) -> None:
        md = "## flow\n\njust text, no fence\n"
        assert extract_flow_section(md) is None

    def test_ignores_other_h2_sections(self) -> None:
        md = "## other\n\n```mermaid\nX --> Y\n```\n\n## flow\n\n```mermaid\nA --> B\n```\n"
        sec = extract_flow_section(md, page="x")
        assert sec is not None
        assert "A --> B" in sec.mermaid
        assert "X --> Y" not in sec.mermaid

    def test_first_flow_section_only(self) -> None:
        # Two ## flow sections: take the first. Documenting the v1
        # behavior; if a page needs more, split it.
        md = "## flow\n\n```mermaid\nfirst\n```\n\n## flow\n\n```mermaid\nsecond\n```\n"
        sec = extract_flow_section(md, page="x")
        assert sec is not None
        assert "first" in sec.mermaid
        assert "second" not in sec.mermaid


class TestListFlowPages:
    def test_lists_pages_with_flow_section(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("## flow\n\n```mermaid\nX --> Y\n```\n", encoding="utf-8")
        (tmp_path / "b.md").write_text("no flow here", encoding="utf-8")
        (tmp_path / "c.md").write_text("## flow\n\n```mermaid\nA --> B\n```\n", encoding="utf-8")
        assert list_flow_pages(tmp_path) == ["a.md", "c.md"]

    def test_skips_hidden_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "secret.md").write_text(
            "## flow\n\n```mermaid\nA --> B\n```\n", encoding="utf-8"
        )
        (tmp_path / "visible.md").write_text(
            "## flow\n\n```mermaid\nX --> Y\n```\n", encoding="utf-8"
        )
        pages = list_flow_pages(tmp_path)
        assert pages == ["visible.md"]

    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "no-such-wiki"
        assert list_flow_pages(nonexistent) == []

    def test_bad_encoding_skipped(self, tmp_path: Path) -> None:
        # Latin-1 encoded file that is not valid UTF-8 must not crash
        # the directory scan.
        (tmp_path / "good.md").write_text("## flow\n\n```mermaid\nA --> B\n```\n", encoding="utf-8")
        (tmp_path / "bad.md").write_bytes(b"\xff\xfe## flow\n```mermaid\n```\n")
        assert list_flow_pages(tmp_path) == ["good.md"]

    def test_utf8_bom_page_is_recognized(self, tmp_path: Path) -> None:
        # Pages saved by Windows editors carry a UTF-8 BOM at byte 0.
        # Without stripping it the ``^##`` regex never matches and the
        # page silently disappears.
        (tmp_path / "bom.md").write_text(
            "\ufeff## flow\n\n```mermaid\nA --> B\n```\n", encoding="utf-8"
        )
        (tmp_path / "plain.md").write_text(
            "## flow\n\n```mermaid\nX --> Y\n```\n", encoding="utf-8"
        )
        assert list_flow_pages(tmp_path) == ["bom.md", "plain.md"]

    def test_resolve_page_path_escapes_traversal(self, tmp_path: Path) -> None:
        # ``../../README.md`` must not escape the wiki root. A safe
        # resolver returns None for any path that resolves outside
        # ``wiki_root``.
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("not wiki", encoding="utf-8")
        from lhgp.flow.wiki_parser import resolve_page_path

        assert resolve_page_path(wiki_root, "../outside.md") is None
        assert resolve_page_path(wiki_root, "../../etc/passwd") is None
        assert resolve_page_path(wiki_root, "missing.md") is None
        # In-tree lookup still works.
        (wiki_root / "real.md").write_text("## flow", encoding="utf-8")
        assert resolve_page_path(wiki_root, "real.md") == wiki_root / "real.md"
