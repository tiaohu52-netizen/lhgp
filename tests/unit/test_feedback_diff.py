"""P6: compute_acceptance_diff boundary tests (lhgp.feedback.diff).

Pins workspace-snapshot semantics so a future rewrite of _walk / fingerprint
comparison doesn't silently mis-classify created/modified/deleted.

Boundary cases covered:
  - missing workspace  (returns empty after-snapshot, no created)
  - empty workspace
  - empty `before` (all current files are created)
  - identical snapshots (no changes)
  - size change  → modified
  - mtime change → modified
  - mix of created + modified + deleted
  - skip_dirs (.git, node_modules, .venv, __pycache__) excluded
  - nested directories
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lhgp.feedback.diff import compute_acceptance_diff
from lhgp.feedback.types import AcceptanceDiff

pytestmark = pytest.mark.unit

CID = "lt-p6-diff"
REV = 1


def _write(root: Path, rel: str, body: str = "x") -> Path:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return full


def _changes_by_path(diff: AcceptanceDiff) -> dict[str, dict]:
    return {c["path"]: c for c in diff.files_changed}


def _paths(diff: AcceptanceDiff) -> set[str]:
    return {c["path"] for c in diff.files_changed}


class TestMissingOrEmptyWorkspace:
    def test_missing_workspace_returns_empty_after(self, tmp_path: Path) -> None:
        diff = compute_acceptance_diff(CID, REV, tmp_path / "nonexistent")
        assert diff.snapshot_after == {"files": []}
        assert diff.files_changed == []
        assert "0 files after" in diff.summary

    def test_empty_workspace_with_no_before(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        diff = compute_acceptance_diff(CID, REV, ws)
        assert _paths(diff) == set()
        assert diff.snapshot_after == {"files": []}

    def test_empty_workspace_against_before_marks_all_deleted(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        before = {
            "files": [
                {"path": "a.py", "size_bytes": 10, "mtime_ns": 1},
                {"path": "b.py", "size_bytes": 20, "mtime_ns": 2},
            ]
        }
        diff = compute_acceptance_diff(CID, REV, ws, before=before)
        assert _paths(diff) == {"a.py", "b.py"}
        for c in diff.files_changed:
            assert c["action"] == "deleted"
        assert "2 deleted" in diff.summary


class TestNoBeforeSnapshot:
    def test_all_files_marked_created(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws, "a.py", "alpha")
        _write(ws, "b.txt", "beta")
        diff = compute_acceptance_diff(CID, REV, ws)
        assert _paths(diff) == {"a.py", "b.txt"}
        for c in diff.files_changed:
            assert c["action"] == "created"
        assert "2 created" in diff.summary

    def test_nested_files_marked_created(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws, "src/pkg/x.py", "x")
        _write(ws, "src/pkg/y.py", "y")
        _write(ws, "docs/readme.md", "r")
        diff = compute_acceptance_diff(CID, REV, ws)
        assert _paths(diff) == {
            "src/pkg/x.py",
            "src/pkg/y.py",
            "docs/readme.md",
        }


class TestIdenticalSnapshots:
    def test_no_changes_when_before_matches_after(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws, "a.py", "alpha")
        before = {
            "files": [
                {
                    "path": "a.py",
                    "size_bytes": 5,
                    "mtime_ns": int((ws / "a.py").stat().st_mtime_ns),
                },
            ]
        }
        diff = compute_acceptance_diff(CID, REV, ws, before=before)
        assert diff.files_changed == []
        assert "0 created, 0 modified, 0 deleted" in diff.summary


class TestModified:
    def test_size_change_marks_modified(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        full = _write(ws, "a.py", "alpha")
        before = {
            "files": [
                {"path": "a.py", "size_bytes": 4, "mtime_ns": int(full.stat().st_mtime_ns)},
            ]
        }
        diff = compute_acceptance_diff(CID, REV, ws, before=before)
        ch = _changes_by_path(diff)
        assert ch["a.py"]["action"] == "modified"
        assert ch["a.py"]["size_before"] == 4
        assert ch["a.py"]["size_after"] == 5

    def test_mtime_change_marks_modified(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws, "a.py", "alpha")
        before = {
            "files": [
                # Same size, but stale mtime → modified.
                {"path": "a.py", "size_bytes": 5, "mtime_ns": 1},
            ]
        }
        diff = compute_acceptance_diff(CID, REV, ws, before=before)
        assert _changes_by_path(diff)["a.py"]["action"] == "modified"

    def test_identical_size_and_mtime_no_change(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        full = _write(ws, "a.py", "alpha")
        mtime = int(full.stat().st_mtime_ns)
        before = {"files": [{"path": "a.py", "size_bytes": 5, "mtime_ns": mtime}]}
        # Touch the file to nudge mtime, then write identical body: the
        # fingerprint also captures mtime_ns, so re-derive the "before"
        # mtime from the actual file to assert no-change baseline.
        diff = compute_acceptance_diff(CID, REV, ws, before=before)
        assert diff.files_changed == []


class TestMixedChanges:
    def test_created_modified_deleted(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws, "kept.py", "kept")  # unchanged
        _write(ws, "modified.py", "after")
        _write(ws, "new.py", "brand new")
        # `gone.py` is in before but not written
        before = {
            "files": [
                {
                    "path": "kept.py",
                    "size_bytes": 4,
                    "mtime_ns": int((ws / "kept.py").stat().st_mtime_ns),
                },
                {"path": "modified.py", "size_bytes": 5, "mtime_ns": 0},
                {"path": "gone.py", "size_bytes": 7, "mtime_ns": 0},
            ]
        }
        diff = compute_acceptance_diff(CID, REV, ws, before=before)
        actions = {p: c["action"] for p, c in _changes_by_path(diff).items()}
        assert actions == {
            "modified.py": "modified",
            "new.py": "created",
            "gone.py": "deleted",
        }
        assert "1 created, 1 modified, 1 deleted" in diff.summary


class TestSkipDirs:
    @pytest.mark.parametrize(
        "skip",
        [".git", "node_modules", ".venv", "__pycache__"],
    )
    def test_skip_dir_excluded(self, tmp_path: Path, skip: str) -> None:
        ws = tmp_path / "ws"
        _write(ws / skip / "ignored.py", "noise")
        _write(ws, "kept.py", "kept")
        diff = compute_acceptance_diff(CID, REV, ws)
        assert _paths(diff) == {"kept.py"}, f"skip={skip} leaked"


class TestSnapshotShape:
    def test_snapshots_are_sorted(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws, "z.py", "z")
        _write(ws, "a.py", "a")
        _write(ws, "m.py", "m")
        diff = compute_acceptance_diff(CID, REV, ws)
        after_paths = [f["path"] for f in diff.snapshot_after["files"]]
        assert after_paths == sorted(after_paths)

    def test_snapshots_round_trip_json(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws, "a.py", "alpha")
        diff = compute_acceptance_diff(CID, REV, ws)
        encoded = json.dumps(diff.snapshot_after, ensure_ascii=False)
        decoded = json.loads(encoded)
        assert decoded == diff.snapshot_after

    def test_attempt_id_preserved(self, tmp_path: Path) -> None:
        diff = compute_acceptance_diff(CID, REV, tmp_path, attempt_id="att-xyz")
        assert diff.attempt_id == "att-xyz"

    def test_contract_id_and_revision_preserved(self, tmp_path: Path) -> None:
        diff = compute_acceptance_diff("lt-abc", 7, tmp_path)
        assert diff.contract_id == "lt-abc"
        assert diff.contract_revision == 7
