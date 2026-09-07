"""P6+1 / memory-and-wiki Phase 3: ``lhgp flow`` CLI dispatch.

Three subcommands:

  - ``ast <file>``     walk a Python file and emit Mermaid / Excalidraw
  - ``wiki <page>``    extract the ``## flow`` section of a wiki page
  - ``list-flow-pages`` list all wiki pages that contain a flow section

The wiki subcommand reads from the project's ``docs/wiki/`` tree by
default; pass ``--wiki-root`` to override (useful for testing or for
mounting a remote wiki).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lhgp.flow.ast_walker import walk_source
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


def flow_command(args: argparse.Namespace) -> int:
    if args.flow_cmd == "ast":
        path = Path(args.file)
        if not path.exists():
            print(f"error: file not found: {path}", file=sys.stderr)
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

    print(f"flow: unknown subcommand {args.flow_cmd}", file=sys.stderr)
    return 2


__all__ = ["flow_command"]
