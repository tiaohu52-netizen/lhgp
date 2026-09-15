"""Compatibility facade for :mod:`longtask.mcp_server`."""

from longtask.mcp_server import *  # noqa: F403, I001


if __name__ == "__main__":
    import sys as _sys

    _sys.argv[0] = "lhgp-mcp"
    raise SystemExit(main())  # noqa: F405
