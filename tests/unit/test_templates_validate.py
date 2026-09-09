"""Focused tests for ``lhgp.templates.validate.validate_draft_file``.

The module exists so a template user gets a readable list of problems
before ``lhgp prepare`` rejects the file.  It shipped at 0 % coverage,
which means every one of its five check tiers was unverified — the
tier that matters most (acceptance targets pointing at nonexistent
scripts) is exactly the kind of thing that silently stops working when
someone touches the path resolution.

Platform note: the "is it executable" branch is only exercised through
``sys.executable`` (a real, executable file on every supported OS).  We
never assert on the Unix-only ``st_mode & 0o111`` *value*; ``os_access_ok``
is checked against the file's own mode so the test says the same thing
on Windows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from lhgp.templates.validate import os_access_ok, validate_draft_file

pytestmark = pytest.mark.unit


def _draft(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "ship the release",
        "objective": "cut v0.1.0 and push the tag",
        "deadline_at": "2026-09-20T18:00:00+00:00",
        "acceptance": {"standard": "tests green", "checks": ["pytest passes"]},
        "budget": {
            "max_dispatches": 6,
            "max_escalations": 2,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 45,
        },
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, payload: Any, *, name: str = "draft.json") -> Path:
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestTierOneShape:
    def test_clean_draft_has_no_problems(self, tmp_path: Path) -> None:
        assert validate_draft_file(_write(tmp_path, _draft())) == []

    def test_unreadable_file_short_circuits(self, tmp_path: Path) -> None:
        problems = validate_draft_file(tmp_path / "missing.json")
        assert len(problems) == 1
        assert problems[0].startswith("cannot read file:")

    def test_invalid_json_short_circuits(self, tmp_path: Path) -> None:
        problems = validate_draft_file(_write(tmp_path, "{not json"))
        assert len(problems) == 1
        assert problems[0].startswith("invalid JSON:")

    def test_non_object_json_short_circuits(self, tmp_path: Path) -> None:
        problems = validate_draft_file(_write(tmp_path, [1, 2, 3]))
        assert problems == ["draft must be a JSON object"]

    @pytest.mark.parametrize(
        "field",
        ["title", "objective", "deadline_at", "acceptance", "budget"],
    )
    def test_each_required_field_is_reported(self, tmp_path: Path, field: str) -> None:
        draft = _draft()
        del draft[field]
        problems = validate_draft_file(_write(tmp_path, draft))
        assert f"missing required field: {field}" in problems


class TestTierTwoPlaceholders:
    def test_unreplaced_angle_bracket_placeholder(self, tmp_path: Path) -> None:
        problems = validate_draft_file(_write(tmp_path, _draft(title="<TITLE>")))
        assert any("draft.title" in p and "unreplaced placeholder" in p for p in problems)

    def test_template_hint_text_is_flagged(self, tmp_path: Path) -> None:
        # Bundled templates say e.g. "如：修复登录 <bug>" — the 如 marker means
        # the author left the example in place.
        draft = _draft(objective="完成一个目标（如：修复登录 bug <id>）")
        problems = validate_draft_file(_write(tmp_path, draft))
        assert any("looks like template text" in p for p in problems)

    def test_placeholders_are_found_inside_nested_lists(self, tmp_path: Path) -> None:
        draft = _draft(acceptance={"standard": "ok", "checks": ["<PATTERN>"]})
        problems = validate_draft_file(_write(tmp_path, draft))
        assert any("acceptance.checks[0]" in p for p in problems)


class TestTierThreeDeadline:
    def test_naive_datetime_needs_an_explicit_offset(self, tmp_path: Path) -> None:
        problems = validate_draft_file(_write(tmp_path, _draft(deadline_at="2026-09-20T18:00:00")))
        assert any("deadline_at has no timezone" in p for p in problems)

    def test_garbage_timestamp_is_reported(self, tmp_path: Path) -> None:
        problems = validate_draft_file(_write(tmp_path, _draft(deadline_at="not-a-date")))
        assert any("not valid ISO 8601" in p for p in problems)

    @pytest.mark.parametrize("value", ["2026-09-20T18:00:00Z", "2026-09-20T18:00:00+08:00"])
    def test_offset_aware_values_pass(self, tmp_path: Path, value: str) -> None:
        assert validate_draft_file(_write(tmp_path, _draft(deadline_at=value))) == []

    def test_placeholder_deadline_is_not_double_reported(self, tmp_path: Path) -> None:
        # A still-templated deadline is already caught by the placeholder tier;
        # the timezone tier must skip it so the user sees one problem, not two.
        problems = validate_draft_file(_write(tmp_path, _draft(deadline_at="<YYYY-MM-DD>")))
        assert sum("deadline_at" in p for p in problems) == 1


class TestTierFourAcceptance:
    def test_bare_string_checks_is_rejected(self, tmp_path: Path) -> None:
        problems = validate_draft_file(_write(tmp_path, _draft(acceptance={"checks": "pytest"})))
        assert any("bare string" in p for p in problems)

    def test_non_structured_check_entry_is_rejected(self, tmp_path: Path) -> None:
        draft = _draft(acceptance={"checks": [{"kind": "file-exists", "target": "a.txt"}, 42]})
        problems = validate_draft_file(_write(tmp_path, draft))
        assert "acceptance.checks[1] must be a string or object" in problems

    def test_legacy_free_text_check_is_skipped(self, tmp_path: Path) -> None:
        draft = _draft(acceptance={"checks": ["旧式自由文本验收", "another"]})
        assert validate_draft_file(_write(tmp_path, draft)) == []

    def test_missing_command_target_is_reported(self, tmp_path: Path) -> None:
        draft = _draft(
            acceptance={"checks": [{"kind": "command-exit-zero", "target": "scripts/nope.py"}]}
        )
        problems = validate_draft_file(
            _write(tmp_path, draft),
        )
        # No workspace_root means the relative path is checked as-is.
        assert any("does not exist on disk" in p for p in problems)

    def test_existing_executable_target_passes(self, tmp_path: Path) -> None:
        draft = _draft(
            acceptance={"checks": [{"kind": "command-exit-zero", "target": sys.executable}]}
        )
        problems = validate_draft_file(_write(tmp_path, draft))
        assert [p for p in problems if "command-exit-zero" in p or "not executable" in p] == []

    def test_workspace_root_anchors_relative_targets(self, tmp_path: Path) -> None:
        (tmp_path / "ws").mkdir()
        draft = _draft(
            acceptance={"checks": [{"kind": "command-exit-zero", "target": "bin/missing.sh"}]}
        )
        problems = validate_draft_file(_write(tmp_path, draft), workspace_root=tmp_path / "ws")
        assert any("does not exist on disk" in p for p in problems)

    def test_file_exists_outside_workspace_is_flagged(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "elsewhere" / "report.md"
        draft = _draft(acceptance={"checks": [{"kind": "file-exists", "target": str(outside)}]})
        problems = validate_draft_file(_write(tmp_path, draft), workspace_root=ws)
        assert any("outside workspace_root" in p for p in problems)

    def test_file_exists_relative_target_is_accepted(self, tmp_path: Path) -> None:
        # Deliverables do not exist yet — the executor creates them — so a
        # relative file-exists check must stay quiet.
        ws = tmp_path / "ws"
        ws.mkdir()
        draft = _draft(acceptance={"checks": [{"kind": "file-exists", "target": "out/report.md"}]})
        assert validate_draft_file(_write(tmp_path, draft), workspace_root=ws) == []


class TestTierFiveBudget:
    @pytest.mark.parametrize(
        "key",
        [
            "max_dispatches",
            "max_escalations",
            "max_concurrent_attempts",
            "max_attempt_minutes",
        ],
    )
    def test_non_positive_limits_are_rejected(self, tmp_path: Path, key: str) -> None:
        draft = _draft(budget={**_draft()["budget"], key: 0})
        problems = validate_draft_file(_write(tmp_path, draft))
        assert f"budget.{key} must be positive, got 0" in problems

    def test_negative_verification_reserve_is_rejected(self, tmp_path: Path) -> None:
        draft = _draft(budget={**_draft()["budget"], "verification_attempts_reserved": -1})
        problems = validate_draft_file(_write(tmp_path, draft))
        assert "budget.verification_attempts_reserved must be >= 0" in problems

    def test_zero_verification_reserve_is_allowed(self, tmp_path: Path) -> None:
        draft = _draft(budget={**_draft()["budget"], "verification_attempts_reserved": 0})
        assert validate_draft_file(_write(tmp_path, draft)) == []


class TestOsAccessOk:
    def test_missing_path_is_not_ok(self, tmp_path: Path) -> None:
        assert os_access_ok(tmp_path / "nope") is False

    def test_matches_the_file_own_mode_bits(self, tmp_path: Path) -> None:
        path = tmp_path / "script.sh"
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        expected = path.stat().st_mode & 0o111 != 0
        assert os_access_ok(path) is expected
