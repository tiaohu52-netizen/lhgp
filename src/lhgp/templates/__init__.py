"""Built-in contract templates shipped with the package."""

from __future__ import annotations

from importlib import resources

__all__ = ["available", "load"]


def available() -> list[str]:
    """Names of the bundled templates (without extension)."""
    package = resources.files("lhgp.templates")
    return sorted(
        entry.name[: -len(".json")] for entry in package.iterdir() if entry.name.endswith(".json")
    )


def load(name: str) -> str:
    """Return the template JSON text for *name*.

    Raises FileNotFoundError for unknown names - callers should list first.
    """
    package = resources.files("lhgp.templates")
    resource = package.joinpath(f"{name}.json")
    if not resource.is_file():
        raise FileNotFoundError(f"unknown template {name!r}; available: {', '.join(available())}")
    return resource.read_text(encoding="utf-8")
