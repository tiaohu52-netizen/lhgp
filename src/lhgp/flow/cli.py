"""P6+1 / memory-and-wiki Phase 3: ``lhgp flow`` CLI dispatch.

Subcommands:

  - ``ast <file>``          walk a Python file and emit Mermaid / Excalidraw
  - ``wiki <page>``         extract the ``## flow`` section of a wiki page
  - ``list-flow-pages``     list all wiki pages that contain a flow section
  - ``contract <id>``       walk the source files referenced by a contract

The wiki subcommand reads from the project's ``docs/wiki/`` tree by
default; pass ``--wiki-root`` to override (useful for testing or for
mounting a remote wiki). The contract subcommand reads the SQLite
state database; pass ``--state-db`` to override the default
``<data-dir>/state.db`` location.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lhgp.flow.ast_walker import _MAX_SOURCE_BYTES, walk_source
from lhgp.flow.contract_flow import walk_contract
from lhgp.flow.render_excalidraw import render_excalidraw
from lhgp.flow.render_mermaid import render_mermaid
from lhgp.flow.wiki_parser import (
    extract_flow_section,
    list_flow_pages,
    resolve_page_path,
)


def _default_wiki_root() -> Path:
    # src/lhgp/flow/cli.py -> src/lhgp/flow -> src/lhgp -> src -> REPO
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "docs" / "wiki"
        if candidate.exists():
            return candidate
    return here.parents[3] / "docs" / "wiki"


def _default_data_root() -> Path:
    # src/lhgp/flow/cli.py -> src/lhgp/flow -> src/lhgp -> src -> REPO
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[3]


def _resolve_state_db(state_db: str | None, data_dir: str | None) -> Path:
    """Pick the SQLite state.db path from ``--state-db`` / ``--data-dir`` flags."""
    # Imported lazily so the lighter subcommands don't pay for the path
    # resolution import (which itself reads ``Path.home()``).
    from lhgp.persistence.paths import default_data_root

    if state_db is not None:
        return Path(state_db).expanduser().resolve()
    root = Path(data_dir).expanduser().resolve() if data_dir else default_data_root()
    return root / "state.db"


def flow_command(args: argparse.Namespace) -> int:
    if args.flow_cmd == "ast":
        path = Path(args.file)
        if not path.exists():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2
        # Pre-check size before read_text so a multi-GB file doesn't
        # land in memory just to be rejected by walk_source below.
        try:
            size = path.stat().st_size
        except OSError as exc:
            print(f"error: cannot stat {path}: {exc}", file=sys.stderr)
            return 2
        if size > _MAX_SOURCE_BYTES:
            print(
                f"error: {path} is {size} bytes; walk_source refuses > {_MAX_SOURCE_BYTES}",
                file=sys.stderr,
            )
            return 2
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 2
        try:
            flow = walk_source(source, args.module or path.stem)
        except SyntaxError as exc:
            print(
                f"error: syntax error in {path}:{getattr(exc, 'lineno', '?')}: {exc.msg}",
                file=sys.stderr,
            )
            return 2
        if args.format == "excalidraw":
            json.dump(render_excalidraw(flow), sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            sys.stdout.write(render_mermaid(flow))
        return 0

    if args.flow_cmd == "wiki":
        wiki_root = (
            Path(args.wiki_root).expanduser().resolve() if args.wiki_root else _default_wiki_root()
        )
        page_path = resolve_page_path(wiki_root, args.page)
        if page_path is None:
            # Try the basename fallback (e.g. "glossary" -> "glossary.md").
            page_path = resolve_page_path(wiki_root, f"{args.page}.md")
        if page_path is None:
            print(
                f"error: page not found or outside wiki root: {args.page}",
                file=sys.stderr,
            )
            return 2
        text = page_path.read_text(encoding="utf-8")
        rel = page_path.relative_to(wiki_root).as_posix()
        section = extract_flow_section(text, page=rel)
        if section is None:
            print(f"no '## flow' section in {rel}")
            return 1
        print(f"--- {rel} (line {section.line}) ---")
        sys.stdout.write("```mermaid\n")
        sys.stdout.write(section.mermaid)
        if not section.mermaid.endswith("```\n"):
            print("```")
        return 0

    if args.flow_cmd == "list-flow-pages":
        wiki_root = (
            Path(args.wiki_root).expanduser().resolve() if args.wiki_root else _default_wiki_root()
        )
        for rel in list_flow_pages(wiki_root):
            print(rel)
        return 0

    if args.flow_cmd == "contract":
        # Lazy import: lighter subcommands must not pay for SQLite open.
        from longtask.persistence.schema import connect as _store_connect
        from longtask.persistence.types import StoreConfig

        db_path = _resolve_state_db(args.state_db, args.data_dir)
        if not db_path.is_file():
            print(f"error: state db not found: {db_path}", file=sys.stderr)
            return 2
        try:
            conn = _store_connect(StoreConfig(db_path=db_path))
        except Exception as exc:
            print(f"error: cannot open state db: {exc}", file=sys.stderr)
            return 2
        try:
            flow = walk_contract(
                conn, args.contract_id, src_root=Path(args.src_root) if args.src_root else None
            )
        finally:
            conn.close()

        if flow.source.startswith("contract:"):
            print(
                f"warning: no source files resolved for contract {args.contract_id}; "
                "rendering empty flow",
                file=sys.stderr,
            )

        if args.format == "excalidraw":
            json.dump(render_excalidraw(flow), sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            sys.stdout.write(render_mermaid(flow))
        return 0

    print(f"flow: unknown subcommand {args.flow_cmd}", file=sys.stderr)
    return 2


__all__ = ["flow_command"]
