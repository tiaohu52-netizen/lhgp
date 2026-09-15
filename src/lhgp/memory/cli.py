"""P6+1 / memory-and-wiki Phase 2: ``lhgp memory`` CLI dispatch.

Five subcommands: add / list / search / show / expire. All read-write
operate on the SQL store; the wiki CLI is its own sibling. Keeps the
store I/O in one place so callers don't reinvent the read paths.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from typing import Any

from lhgp.memory.store import (
    MemoryStoreError,
    expire_due,
    get_memory,
    list_memories,
    record_memory,
    search_memories,
)
from lhgp.memory.types import Memory, MemoryKind, MemoryScope


def _print_memories(memories: list[Memory], as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                [m.to_db_row() for m in memories],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return
    if not memories:
        print("(no memories)")
        return
    for m in memories:
        print(f"  [{m.id}] {m.scope.value}/{m.kind.value}  {m.title}")
        if m.tags:
            print(f"      tags: {', '.join('#' + t for t in m.tags)}")
        if m.score != 0.0:
            print(f"      score: {m.score:.3f}")
        if m.source_contract_id or m.source_event_id:
            print(
                f"      source: {m.source_actor or 'auto'}"
                f"{f':{m.source_contract_id}' if m.source_contract_id else ''}"
                f"{f':e{m.source_event_id}' if m.source_event_id else ''}"
            )
        if m.expires_at:
            print(f"      expires: {m.expires_at.isoformat()}")
        for line in m.body_md.splitlines()[:6]:
            print(f"      {line}")


def memory_command(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.memory_cmd == "add":
        title = str(args.title).strip()
        if not title:
            print("error: --title is required", file=sys.stderr)
            return 2
        body = args.body
        if body is None:
            body = sys.stdin.read() if not sys.stdin.isatty() else ""
        if not body.strip():
            print("error: --body (or stdin) is required", file=sys.stderr)
            return 2
        try:
            scope = MemoryScope(args.scope)
            kind = MemoryKind(args.kind)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        tags: tuple[str, ...] = tuple(t.strip() for t in (args.tags or "").split(",") if t.strip())
        from datetime import timedelta as _td

        now = datetime.now(UTC)
        if args.no_expire:
            expires_at = None
        else:
            days = int(args.expires_in_days) if args.expires_in_days is not None else 180
            expires_at = now + _td(days=days)
        memory = Memory(
            scope=scope,
            kind=kind,
            title=title,
            body_md=body,
            tags=tags,
            source_contract_id=args.source_contract or None,
            source_event_id=args.source_event_id,
            source_actor=args.actor or "user",
            score=float(args.score),
            created_at=now,
            expires_at=expires_at,
        )
        try:
            mid = record_memory(conn, memory)
        except MemoryStoreError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"recorded memory id={mid}")
        return 0

    if args.memory_cmd == "list":
        kwargs: dict[str, Any] = {"limit": int(args.limit)}
        if args.scope:
            try:
                kwargs["scope"] = MemoryScope(args.scope)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
        if args.kind:
            try:
                kwargs["kind"] = MemoryKind(args.kind)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
        if args.tags_any:
            kwargs["tags_any"] = tuple(t.strip() for t in args.tags_any.split(",") if t.strip())
        if args.include_expired:
            kwargs["include_expired"] = True
        memories = list_memories(conn, **kwargs)
        _print_memories(memories, args.json)
        return 0

    if args.memory_cmd == "search":
        memories = search_memories(conn, args.keyword, scope=args.scope, limit=int(args.limit))
        _print_memories(memories, args.json)
        return 0 if memories else 1

    if args.memory_cmd == "show":
        if args.id is None:
            print("error: --id is required for show", file=sys.stderr)
            return 2
        m = get_memory(conn, int(args.id))
        if m is None:
            print(f"memory id={args.id} not found")
            return 1
        print(json.dumps(m.to_db_row(), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.memory_cmd == "expire":
        # ``expire_due`` returns the deleted ids, not a count — the report the
        # user asked for is how many rows went away.
        n = len(expire_due(conn))
        print(f"expired {n} due memories")
        return 0

    print(f"memory: unknown subcommand {args.memory_cmd}")
    return 2


__all__ = ["memory_command"]
