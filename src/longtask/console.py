"""Entry-point console hardening shared by the CLI and MCP entries."""

from __future__ import annotations

import contextlib
import sys

__all__ = ["harden_stdio"]


def harden_stdio() -> None:
    """Keep ``--help`` / ``--usage`` printable on non-UTF-8 consoles (cp1252).

    Unencodable characters are replaced instead of raising UnicodeEncodeError;
    the deprecation warnings above are ASCII and unaffected. MCP stdio JSON is
    ASCII-escaped by default, so protocol payloads keep their strict encoding.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # A closed or detached stream means printing is best-effort anyway.
        with contextlib.suppress(OSError, ValueError):
            reconfigure(errors="replace")
