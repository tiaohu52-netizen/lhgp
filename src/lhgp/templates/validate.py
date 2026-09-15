"""Contract draft pre-validation: catch common mistakes before prepare.

``validate_draft_file`` reads a JSON draft file (the same shape
``lhgp prepare --file`` accepts) and reports problems that would cause
prepare to reject it — placeholder text, missing timezone, nonexistent
acceptance targets, budget typos — *before* the user hits the error.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def validate_draft_file(
    draft_path: Path,
    *,
    workspace_root: Path | None = None,
) -> list[str]:
    """Validate a draft JSON file and return a list of problems (empty = OK).

    Checks fall into three tiers:
    1. JSON parse + required fields (these would fail prepare immediately);
    2. Placeholder detection (``<...>`` patterns from bundled templates);
    3. Semantic checks: deadline timezone, acceptance command targets
       exist on disk, budget positivity.
    """
    problems: list[str] = []

    # 1. JSON parse
    try:
        raw = draft_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read file: {exc}"]
    try:
        draft: Any = json.loads(raw)
    except ValueError as exc:
        return [f"invalid JSON: {exc}"]
    if not isinstance(draft, dict):
        return ["draft must be a JSON object"]

    # 2. Required fields (mirror prepare's requirements)
    for field in ("title", "objective", "deadline_at", "acceptance", "budget"):
        if field not in draft:
            problems.append(f"missing required field: {field}")

    # Placeholder detection: template users forget to replace <...>
    _check_placeholders(draft, problems, path="draft")

    # 3. Deadline timezone
    #
    # 不再用「后缀白名单」判断带不带时区：旧写法只要字符串以 ``+08:00`` 结尾
    # 就整段跳过解析，于是 ``"2026-13-45+08:00"`` 这种内容乱写但后缀正确的
    # deadline 被判 OK，而 prepare 会拒（fail-open）。现在一律交给
    # ``fromisoformat``：解析失败 → 报非法；解析成功但无 tzinfo → 报缺时区。
    # 与 ``contracts/validation._looks_like_datetime_with_tz`` 同口径。
    deadline = draft.get("deadline_at")
    if isinstance(deadline, str) and "<" not in deadline:
        from datetime import datetime as _dt

        try:
            parsed = _dt.fromisoformat(deadline)
        except ValueError:
            problems.append(f"deadline_at is not valid ISO 8601: {deadline!r}")
        else:
            if parsed.tzinfo is None:
                problems.append(
                    f"deadline_at has no timezone: {deadline!r} — "
                    "prepare requires an explicit UTC offset"
                )

    # 4. Acceptance checks
    acceptance = draft.get("acceptance")
    if "acceptance" in draft and not isinstance(acceptance, dict):
        # 旧实现只在 acceptance 是 dict 时检查 checks，非 dict 直接静默跳过——
        # 用户写成 ``"acceptance": ["..."]`` 会拿到「OK」而 prepare 拒收。
        problems.append(f"acceptance must be a JSON object, got {type(acceptance).__name__}")
    elif isinstance(acceptance, dict):
        checks = acceptance.get("checks")
        if isinstance(checks, list):
            for i, check in enumerate(checks):
                _validate_one_check(check, i, problems, workspace_root)
        elif isinstance(checks, str):
            problems.append(
                f'acceptance_checks is a bare string {checks!r}; pass an array: ["..."]'
            )

    # 5. Budget positivity
    budget = draft.get("budget")
    if isinstance(budget, dict):
        for key in (
            "max_dispatches",
            "max_escalations",
            "max_concurrent_attempts",
            "max_attempt_minutes",
        ):
            value = budget.get(key)
            if isinstance(value, (int, float)) and value <= 0:
                problems.append(f"budget.{key} must be positive, got {value}")
        reserved = budget.get("verification_attempts_reserved")
        if isinstance(reserved, (int, float)) and reserved < 0:
            problems.append("budget.verification_attempts_reserved must be >= 0")

    return problems


def _check_placeholders(obj: Any, problems: list[str], *, path: str) -> None:
    """Recursively find unreplaced <placeholder> patterns from templates."""
    if isinstance(obj, str):
        if obj.startswith("<") and obj.endswith(">"):
            problems.append(f"{path}: unreplaced placeholder {obj!r}")
        elif "<" in obj and ">" in obj and "如" in obj:
            problems.append(f"{path}: looks like template text: {obj[:60]!r}")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _check_placeholders(value, problems, path=f"{path}.{key}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _check_placeholders(item, problems, path=f"{path}[{i}]")


def _validate_one_check(
    check: Any, index: int, problems: list[str], workspace_root: Path | None
) -> None:
    """Validate one acceptance check entry."""
    if isinstance(check, str):
        return  # legacy free-text check, no structured validation possible

    if not isinstance(check, dict):
        problems.append(f"acceptance.checks[{index}] must be a string or object")
        return

    kind = check.get("kind")
    target = check.get("target")

    if (
        kind == "command-exit-zero"
        and isinstance(target, str)
        and ("/" in target or "\\" in target)
    ):
        resolved = Path(target)
        if workspace_root is not None and not resolved.is_absolute():
            resolved = workspace_root / target
        if not resolved.is_file():
            problems.append(
                f"acceptance.checks[{index}].command-exit-zero: "
                f"target {target!r} does not exist on disk"
            )
        elif not shutil.which(str(resolved)) and not os_access_ok(resolved):
            problems.append(f"acceptance.checks[{index}]: {target!r} exists but is not executable")
    elif kind == "file-exists" and workspace_root is not None and isinstance(target, str):
        resolved = workspace_root / target
        # Don't require existence — the executor creates deliverables —
        # but flag absolute paths outside workspace
        if resolved.is_absolute() and workspace_root not in resolved.parents:
            problems.append(
                f"acceptance.checks[{index}].file-exists: target {target!r} "
                "is outside workspace_root"
            )


def os_access_ok(path: Path) -> bool:
    try:
        return path.stat().st_mode & 0o111 != 0
    except OSError:
        return False


__all__ = ["validate_draft_file"]
