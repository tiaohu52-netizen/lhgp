"""Compatibility facade for :mod:`longtask.cli.main`."""

from longtask.cli.main import *  # noqa: F403, I001


if __name__ == "__main__":
    import sys as _sys

    _sys.argv[0] = "lhgp"
    entrypoint()  # noqa: F405
