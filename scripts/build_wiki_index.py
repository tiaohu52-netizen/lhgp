"""Build a machine-readable index of `docs/wiki/` for AI consumption.

The wiki itself is plain Markdown (Obsidian-style, git-tracked, IDE-searchable).
The JSON index is the AI-friendly bridge:

  - frontmatter parsed once, exposed as structured fields
  - outgoing wikilinks extracted (`[[link]]`, `[[link|display]]`, `[[link#heading]]`)
  - backlinks computed by inverting outgoing-link graph
  - `^blockid` anchors extracted (so AI can use them as stable cross-page references)

The script is idempotent and exits non-zero on any parse error. Run after any
wiki page is added or updated; the resulting `docs/wiki/.index.json` is what
AI tools and the lhgp `wiki` CLI command consume.

This is intentionally file-only (no DB): the index is regenerated from the
source markdown on every run, so the markdown is the single source of truth.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = REPO_ROOT / "docs" / "wiki"
INDEX_PATH = WIKI_ROOT / ".index.json"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
BLOCKID_RE = re.compile(r"\^([a-zA-Z][\w-]*)\b")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+\^[a-zA-Z][\w-]*)?\s*$", re.MULTILINE)


@dataclass(frozen=True)
class WikiPage:
    rel_path: str  # e.g. "playbook/add-event-type.md"
    abs_path: Path
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body_md: str = ""
    title: str = ""
    type: str = ""
    status: str = ""
    tags: list[str] = field(default_factory=list)
    audience: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)
    block_ids: list[str] = field(default_factory=list)
    headings: list[dict[str, str]] = field(default_factory=list)


def _parse_simple_yaml(frontmatter: str) -> dict[str, Any]:
    """Parse a tiny subset of YAML sufficient for our wiki frontmatter.

    Supports: scalars, lists (`[a, b, c]`), empty values. We do NOT need full
    YAML; if someone needs it, swap this for `yaml.safe_load`.
    """
    out: dict[str, Any] = {}
    for line in frontmatter.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [v.strip() for v in inner.split(",") if v.strip()]
            # Strip surrounding wikilink brackets so the value coming out
            # is the bare page name. Accept either `[[name]]` (Obsidian
            # style) or `[name]` (the user used a single bracket in the
            # frontmatter list). Consumer is free to re-wrap on display.
            cleaned: list[str] = []
            for i in items:
                v = i
                while v.startswith("["):
                    v = v[1:]
                while v.endswith("]"):
                    v = v[:-1]
                v = v.strip().strip("\"'")
                if v:
                    cleaned.append(v)
            out[key] = cleaned
        elif value == "":
            out[key] = ""
        else:
            out[key] = value.strip("\"'")
    return out


def _parse_page(path: Path) -> WikiPage:
    rel = path.relative_to(WIKI_ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    fm_match = FRONTMATTER_RE.match(text)
    frontmatter_raw = ""
    body = text
    if fm_match:
        frontmatter_raw = fm_match.group(1)
        body = text[fm_match.end() :]
    fm = _parse_simple_yaml(frontmatter_raw) if frontmatter_raw else {}
    outgoing = [m.group(1).split("|")[0].split("#")[0].strip() for m in WIKILINK_RE.finditer(body)]
    block_ids = [m.group(1) for m in BLOCKID_RE.finditer(body)]
    headings = [
        {"level": len(m.group(1)), "text": m.group(2).strip()} for m in HEADING_RE.finditer(body)
    ]
    return WikiPage(
        rel_path=rel,
        abs_path=path,
        frontmatter=fm,
        body_md=body,
        title=str(fm.get("title", path.stem)),
        type=str(fm.get("type", "")),
        status=str(fm.get("status", "")),
        tags=list(fm.get("tags", [])),
        audience=list(fm.get("audience", [])),
        related=list(fm.get("related", [])),
        requires=list(fm.get("requires", [])),
        outgoing=outgoing,
        block_ids=block_ids,
        headings=headings,
    )


def _resolve_link(link: str, pages_by_stem: dict[str, WikiPage]) -> str | None:
    """Resolve a bare wikilink to a page rel_path or return None.

    Tries in order:
      1. exact key match (full path or stem form)
      2. basename match (so `[[sql-binding]]` resolves to
         `playbook/sql-binding.md` — the common pattern when MOCs
         are nested in a subdirectory)
    Heading-only references (`#heading`) return None.
    """
    if not link or link.startswith("#"):
        return None
    stem = link.lower()
    if stem in pages_by_stem:
        return pages_by_stem[stem].rel_path
    basename = stem.rsplit("/", 1)[-1]
    for k, page in pages_by_stem.items():
        if k.rsplit("/", 1)[-1] == basename:
            return page.rel_path
    return None


def build() -> dict[str, Any]:
    pages: list[WikiPage] = []
    for path in sorted(WIKI_ROOT.rglob("*.md")):
        # Skip hidden directories (.scratch-trash, .index.json, etc.)
        # so they never accidentally get indexed.
        rel = path.relative_to(WIKI_ROOT)
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        pages.append(_parse_page(path))
    pages_by_stem = {p.rel_path.removesuffix(".md").lower(): p for p in pages}
    outgoing_resolved: dict[str, list[str]] = {}
    for page in pages:
        out: list[str] = []
        # Body wikilinks (free-form prose) + frontmatter related
        # (machine-asserted cross-references). Both are equally part of
        # the link graph from the AI's perspective.
        for link in list(page.outgoing) + list(page.related):
            target = _resolve_link(link, pages_by_stem)
            if target is not None and target not in out:
                out.append(target)
        outgoing_resolved[page.rel_path] = out
    backlinks: dict[str, list[str]] = defaultdict(list)
    for source, targets in outgoing_resolved.items():
        for target in targets:
            backlinks[target].append(source)
    payload_pages = []
    for page in pages:
        payload_pages.append(
            {
                "path": page.rel_path,
                "title": page.title,
                "type": page.type,
                "status": page.status,
                "tags": page.tags,
                "audience": page.audience,
                "requires": page.requires,
                "related": page.related,
                # outgoing_raw preserves the bare wikilink text even when
                # the target page does not exist yet (e.g. an MOC that
                # lists playbooks not yet written). outgoing_resolved
                # only contains targets that exist; the AI can use
                # outgoing_raw to know what's referenced even before
                # the page materialises.
                "outgoing": outgoing_resolved[page.rel_path],
                "outgoing_raw": page.outgoing,
                "backlinks": sorted(backlinks.get(page.rel_path, [])),
                "block_ids": page.block_ids,
                "headings": page.headings,
            }
        )
    types: dict[str, int] = {}
    for p in pages:
        types[p.type] = types.get(p.type, 0) + 1
    return {
        "schema_version": 1,
        "generated_by": "scripts/build_wiki_index.py",
        "wiki_root": str(WIKI_ROOT.relative_to(REPO_ROOT)),
        "page_count": len(pages),
        "by_type": types,
        "pages": payload_pages,
    }


def main() -> int:
    if not WIKI_ROOT.is_dir():
        print(f"error: wiki root not found at {WIKI_ROOT}", file=sys.stderr)
        return 1
    payload = build()
    INDEX_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {INDEX_PATH.relative_to(REPO_ROOT)} ({payload['page_count']} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
