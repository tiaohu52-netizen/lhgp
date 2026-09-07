"""P6+1: protocol-internal wiki reader (CLI side).

The wiki is plain Markdown under ``docs/wiki/`` (git-tracked, Obsidian-style).
This module wraps ``scripts/build_wiki_index.py`` for the CLI: load
``.index.json``, dispatch list/read/search/show-graph subcommands.

The wiki never touches the SQL store. The CLI is read-only and works
without a running daemon — it's a documentation tool, not a runtime
component.

Why a CLI and not just ``cat docs/wiki/...``:
  - ``show-graph`` shows backlinks, which would otherwise need a second
    script run.
  - ``search`` does case-insensitive title + tag + body scan with consistent
    output.
  - The same entry point is what the planned ``lhgp-memory`` (Phase 2) will
    reuse for surfacing memories alongside wiki pages.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
WIKI_ROOT = REPO_ROOT / "docs" / "wiki"
INDEX_PATH = WIKI_ROOT / ".index.json"


@dataclass(frozen=True, slots=True)
class WikiEntry:
    path: str
    title: str
    type: str
    status: str
    tags: tuple[str, ...]
    audience: tuple[str, ...]
    requires: tuple[str, ...]
    related: tuple[str, ...]
    outgoing: tuple[str, ...]
    outgoing_raw: tuple[str, ...]
    backlinks: tuple[str, ...]
    block_ids: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WikiEntry:
        return cls(
            path=str(payload["path"]),
            title=str(payload.get("title", payload["path"])),
            type=str(payload.get("type", "")),
            status=str(payload.get("status", "")),
            tags=tuple(payload.get("tags", [])),
            audience=tuple(payload.get("audience", [])),
            requires=tuple(payload.get("requires", [])),
            related=tuple(payload.get("related", [])),
            outgoing=tuple(payload.get("outgoing", [])),
            outgoing_raw=tuple(payload.get("outgoing_raw", [])),
            backlinks=tuple(payload.get("backlinks", [])),
            block_ids=tuple(payload.get("block_ids", [])),
        )


def _load_index() -> dict[str, Any] | None:
    if not INDEX_PATH.is_file():
        return None
    loaded: Any = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return None
    return loaded


def _resolve_page(pages: dict[str, WikiEntry], ref: str) -> WikiEntry | None:
    """Resolve bare stem or relative path. Case-insensitive.

    Tries in order: full path, stem-without-.md, and basename-only
    (so `add-event-type` finds `playbook/add-event-type.md`).
    """
    needle = ref.strip().removesuffix(".md").lower()
    if needle in pages:
        return pages[needle]
    needle_md = needle + ".md"
    if needle_md in pages:
        return pages[needle_md]
    # basename fallback: any page whose filename (without folder) matches
    basename = needle.rsplit("/", 1)[-1]
    for k, e in pages.items():
        if k.endswith("/" + basename) or k == basename:
            return e
        if k.endswith("/" + basename + ".md") or k == basename + ".md":
            return e
    return None


def _build_pages() -> dict[str, WikiEntry]:
    payload = _load_index()
    if payload is None:
        return {}
    pages: dict[str, WikiEntry] = {}
    for entry in payload.get("pages", []):
        e = WikiEntry.from_payload(entry)
        # Index both by full path and by stem (so `wiki read glossary`
        # works without typing .md)
        pages[e.path.lower()] = e
        pages[e.path.removesuffix(".md").lower()] = e
    return pages


def _cmd_list(pages: dict[str, WikiEntry], _args: argparse.Namespace) -> int:
    if not pages:
        print("(no wiki pages — run scripts/build_wiki_index.py first)")
        return 1
    by_type: dict[str, list[WikiEntry]] = {}
    seen: set[str] = set()
    for e in pages.values():
        if e.path in seen:
            continue
        seen.add(e.path)
        by_type.setdefault(e.type or "—", []).append(e)
    for t in sorted(by_type):
        print(f"## {t or 'untagged'}")
        for e in sorted(by_type[t], key=lambda x: x.path):
            tag_summary = ", ".join(e.tags[:3]) if e.tags else ""
            print(f"  - {e.path:40s}  {e.status:9s}  {tag_summary}")
    return 0


def _cmd_read(pages: dict[str, WikiEntry], args: argparse.Namespace) -> int:
    entry = _resolve_page(pages, args.page)
    if entry is None:
        print(f"wiki: page not found: {args.page}")
        return 1
    full_path = WIKI_ROOT / entry.path
    if not full_path.is_file():
        print(f"wiki: page file missing: {full_path}")
        return 1
    print(full_path.read_text(encoding="utf-8"))
    return 0


def _cmd_search(pages: dict[str, WikiEntry], args: argparse.Namespace) -> int:
    needle = args.keyword.lower()
    type_filter = args.type
    body_cache: dict[str, str] = {}

    def _body(path: str) -> str:
        if path in body_cache:
            return body_cache[path]
        full = WIKI_ROOT / path
        text = full.read_text(encoding="utf-8") if full.is_file() else ""
        body_cache[path] = text
        return text

    matches: list[tuple[int, WikiEntry, str]] = []
    seen: set[str] = set()
    for e in pages.values():
        if e.path in seen:
            continue
        seen.add(e.path)
        if not e.path.endswith(".md"):
            continue
        if type_filter and e.type != type_filter:
            continue
        haystack = " ".join([e.title, e.path, *e.tags, *e.audience, _body(e.path)]).lower()
        if needle in haystack:
            # score: title/tag hit > body hit
            score = 0
            if needle in e.title.lower():
                score += 3
            if any(needle in t.lower() for t in e.tags):
                score += 2
            if needle in _body(e.path).lower():
                score += 1
            snippet = ""
            body_lower = _body(e.path).lower()
            idx = body_lower.find(needle)
            if idx >= 0:
                start = max(0, idx - 30)
                end = min(len(_body(e.path)), idx + len(needle) + 50)
                snippet = _body(e.path)[start:end].replace("\n", " ")
            matches.append((score, e, snippet))
    if not matches:
        print(f"wiki: no matches for {needle!r}")
        return 1
    for score, e, snippet in sorted(matches, key=lambda t: -t[0]):
        print(f"  [{score}] {e.path}  {e.title}")
        if snippet:
            print(f"      …{snippet}…")
    return 0


def _cmd_graph(pages: dict[str, WikiEntry], args: argparse.Namespace) -> int:
    entry = _resolve_page(pages, args.page)
    if entry is None:
        print(f"wiki: page not found: {args.page}")
        return 1
    print(f"# {entry.title} ({entry.path})")
    print(f"  type:     {entry.type}")
    print(f"  status:   {entry.status}")
    print(f"  audience: {', '.join(entry.audience) or '—'}")
    print(f"  tags:     {', '.join(entry.tags) or '—'}")
    print(f"  requires: {', '.join(entry.requires) or '—'}")
    print()
    print("## outgoing (this page links to)")
    if entry.outgoing:
        for o in entry.outgoing:
            print(f"  - {o}")
    elif entry.outgoing_raw:
        print("  (no resolved targets — only unresolved links below)")
    else:
        print("  (none)")
    unresolved = [r for r in entry.outgoing_raw if r not in entry.outgoing]
    if unresolved:
        print()
        print("## outgoing_raw (unresolved — target page does not exist yet)")
        for r in unresolved:
            print(f"  - {r}")
    print()
    print("## backlinks (link to this page)")
    for b in entry.backlinks:
        print(f"  - {b}")
    if not entry.backlinks:
        print("  (none)")
    print()
    if entry.block_ids:
        print("## block ids (stable anchors)")
        for bid in entry.block_ids:
            print(f"  - ^{bid}")
    return 0


def wiki_command(args: argparse.Namespace) -> int:
    pages = _build_pages()
    if args.wiki_cmd == "list":
        return _cmd_list(pages, args)
    if args.wiki_cmd == "read":
        return _cmd_read(pages, args)
    if args.wiki_cmd == "search":
        return _cmd_search(pages, args)
    if args.wiki_cmd == "show-graph":
        return _cmd_graph(pages, args)
    print(f"wiki: unknown subcommand {args.wiki_cmd}")
    return 2


__all__ = ["WikiEntry", "wiki_command"]
