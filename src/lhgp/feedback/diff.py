"""P6: Compute file-level diff between an attempt's reported artifacts and the
final workspace state.

The result is an :class:`AcceptanceDiff` capturing what the executor
*reported* it produced (``snapshot_before``) versus what the contract
runtime actually found at the contract's terminal state
(``snapshot_after``).

This is intentionally filesystem-light: we do not hash every byte. We
list the workspace tree once, take sizes + mtimes, and diff the two
lists. Files present in only one of the two lists are flagged
created/deleted. Files present in both with size or mtime diff are
flagged modified.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lhgp.feedback.types import AcceptanceDiff


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    path: str
    size_bytes: int
    mtime_ns: int

    @classmethod
    def of(cls, root: Path, rel: str) -> FileFingerprint:
        full = root / rel
        try:
            stat = full.stat()
        except FileNotFoundError:
            return cls(path=rel, size_bytes=-1, mtime_ns=0)
        return cls(
            path=rel,
            size_bytes=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
        )


def _walk(
    root: Path,
    *,
    skip_dirs: Iterable[str] = (".git", "node_modules", ".venv", "__pycache__"),
) -> list[str]:
    if not root.exists():
        return []
    out: list[str] = []
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            full = Path(dirpath) / fn
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            out.append(rel)
    return out


def _fingerprint_set(root: Path, rels: Iterable[str]) -> dict[str, FileFingerprint]:
    return {rel: FileFingerprint.of(root, rel) for rel in rels}


def compute_acceptance_diff(
    contract_id: str,
    contract_revision: int,
    workspace: Path,
    *,
    before: dict[str, Any] | None = None,
    attempt_id: str | None = None,
) -> AcceptanceDiff:
    """Compare ``before`` artifact set to the current ``workspace`` tree.

    ``before`` is typically a dict like
    ``{"files": [{"path": "src/x.py", "size_bytes": 1234}, ...]}`` pulled
    from the last attempt's reported artifacts. If omitted or empty, the
    diff is "everything in the workspace is new".
    """
    before_files: dict[str, FileFingerprint] = {}
    if before:
        for entry in before.get("files", []):
            rel = entry.get("path")
            if not rel:
                continue
            before_files[rel] = FileFingerprint(
                path=rel,
                size_bytes=int(entry.get("size_bytes", 0)),
                mtime_ns=int(entry.get("mtime_ns", 0)),
            )

    after_rels = _walk(workspace)
    after_files = _fingerprint_set(workspace, after_rels)

    changes: list[dict[str, Any]] = []
    created = [r for r in after_files if r not in before_files]
    deleted = [r for r in before_files if r not in after_files]
    modified = []
    for r in after_files:
        if r in before_files and (
            before_files[r].size_bytes != after_files[r].size_bytes
            or before_files[r].mtime_ns != after_files[r].mtime_ns
        ):
            modified.append(
                {
                    "path": r,
                    "action": "modified",
                    "size_before": before_files[r].size_bytes,
                    "size_after": after_files[r].size_bytes,
                }
            )
    for r in created:
        changes.append({"path": r, "action": "created", "size_after": after_files[r].size_bytes})
    for r in deleted:
        changes.append({"path": r, "action": "deleted", "size_before": before_files[r].size_bytes})
    for m in modified:
        changes.append(m)

    summary = (
        f"{len(created)} created, {len(modified)} modified, "
        f"{len(deleted)} deleted; {len(after_files)} files after, "
        f"{len(before_files)} files before"
    )
    return AcceptanceDiff(
        contract_id=contract_id,
        contract_revision=contract_revision,
        attempt_id=attempt_id,
        snapshot_before={"files": [asdict(before_files[r]) for r in sorted(before_files)]},
        snapshot_after={"files": [asdict(after_files[r]) for r in sorted(after_files)]},
        files_changed=sorted(changes, key=lambda c: c["path"]),
        summary=summary,
        computed_at=datetime.now(UTC),
    )
