"""Deterministic acceptance-check evaluator (SPEC §12.1).

The canonical ``lhgp`` namespace owns the evaluator implementation.  The
historical ``longtask.acceptance.evaluator`` path remains a compatibility
facade for the migration window.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lhgp.acceptance.checks import CheckKind, CheckSpec


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    outcome: str
    source: str
    details: str = ""

    def to_evidence(self) -> dict[str, str]:
        return {
            "check_id": self.check_id,
            "outcome": self.outcome,
            "source": self.source,
            "details": self.details,
        }


def _safe_target(root: Path, target: str) -> Path | None:
    candidate = (root / target).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def evaluate_check(
    spec: CheckSpec,
    *,
    workspace_root: Path,
    timeout_seconds: float | None = None,
) -> CheckResult:
    """Evaluate one check, converting execution errors to audit outcomes.

    ``timeout_seconds`` is supplied by the runner from the contract's remaining
    deadline.  A typed check may still request a shorter ``args.timeout``;
    neither path can exceed the hard 60-second safety cap.  A timeout is
    ``undetermined`` evidence, not a fabricated pass/fail verdict.
    """
    check_id = f"{spec.kind.value}:{spec.target}"
    target = _safe_target(workspace_root, spec.target)
    if spec.kind in (
        CheckKind.FILE_EXISTS,
        CheckKind.FILE_CONTENT_MATCHES,
        CheckKind.ARTIFACT_PRESENT,
        CheckKind.STRUCTURE_VALID,
    ):
        if target is None:
            return CheckResult(check_id, "fail", "path-policy", "target escapes workspace")
        if not target.is_file():
            return CheckResult(check_id, "fail", str(target), "artifact does not exist")

    try:
        if spec.kind in (CheckKind.FILE_EXISTS, CheckKind.ARTIFACT_PRESENT):
            return CheckResult(check_id, "pass", str(target or spec.target))
        if spec.kind == CheckKind.FILE_CONTENT_MATCHES:
            text = target.read_text(encoding="utf-8")  # type: ignore[union-attr]
            if "sha256" in spec.args:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()  # type: ignore[union-attr]
                return CheckResult(
                    check_id,
                    "pass" if digest == spec.args["sha256"] else "fail",
                    str(target),
                    digest,
                )
            expected = spec.args.get("contains")
            pattern = spec.args.get("regex")
            if expected is None and pattern is None:
                return CheckResult(check_id, "undetermined", str(target), "missing contains/regex")
            matched = (
                str(expected) in text
                if expected is not None
                else re.search(str(pattern), text) is not None
            )
            return CheckResult(check_id, "pass" if matched else "fail", str(target))
        if spec.kind == CheckKind.COMMAND_EXIT_ZERO:
            argv = [spec.target, *(str(x) for x in spec.args.get("argv", ()))]
            timeout = _command_timeout_seconds(spec, timeout_seconds)
            try:
                completed = subprocess.run(  # noqa: S603 — structured argv + shell=False
                    argv,
                    cwd=workspace_root,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError:
                # SPEC §12.1 环境契约：命令在守护进程环境执行，其 PATH
                # 通常不含项目虚拟环境。给声明者可行动的指引，而不是裸
                # WinError 2 / ENOENT。
                return CheckResult(
                    check_id,
                    "undetermined",
                    " ".join(argv),
                    f"command not found in daemon PATH: {spec.target!r} — "
                    "use an absolute interpreter path or rely on the "
                    "verifier verdict block (§12.4)",
                )
            return CheckResult(
                check_id,
                "pass" if completed.returncode == 0 else "fail",
                " ".join(argv),
                f"exit={completed.returncode}",
            )
        # ── 扩展词汇表：让更多意图能表达成确定性检查 ──
        if spec.kind in (CheckKind.OUTPUT_CONTAINS, CheckKind.OUTPUT_NOT_CONTAINS):
            # 运行命令，检查 stdout 是否包含/不包含指定文本
            argv = [spec.target, *(str(x) for x in spec.args.get("argv", ()))]
            expected = str(spec.args.get("text", ""))
            timeout = _command_timeout_seconds(spec, timeout_seconds)
            completed = subprocess.run(  # noqa: S603
                argv,
                cwd=workspace_root,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = completed.stdout or ""
            if spec.kind == CheckKind.OUTPUT_CONTAINS:
                matched = expected in output
            else:
                matched = expected not in output
            return CheckResult(
                check_id,
                "pass" if matched else "fail",
                " ".join(argv),
                f"looking for {expected!r} in stdout",
            )

        if spec.kind == CheckKind.OUTPUT_MATCHES:
            argv = [spec.target, *(str(x) for x in spec.args.get("argv", ()))]
            pattern = str(spec.args.get("regex", ""))
            timeout = _command_timeout_seconds(spec, timeout_seconds)
            completed = subprocess.run(  # noqa: S603
                argv,
                cwd=workspace_root,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            matched = re.search(pattern, completed.stdout or "") is not None
            return CheckResult(
                check_id,
                "pass" if matched else "fail",
                " ".join(argv),
                f"regex {pattern!r} on stdout",
            )

        if spec.kind == CheckKind.FILE_NOT_EMPTY:
            if target is None:
                return CheckResult(check_id, "fail", "path-policy", "target escapes workspace")
            text = target.read_text(encoding="utf-8").strip()
            return CheckResult(
                check_id,
                "pass" if text else "fail",
                str(target),
                f"{len(text)} chars",
            )

        if spec.kind in (CheckKind.LINE_COUNT_MIN, CheckKind.LINE_COUNT_MAX):
            if target is None:
                return CheckResult(check_id, "fail", "path-policy", "target escapes workspace")
            lines = len(target.read_text(encoding="utf-8").splitlines())
            threshold = int(spec.args.get("count", 0))
            ok = lines >= threshold if spec.kind == CheckKind.LINE_COUNT_MIN else lines <= threshold
            return CheckResult(
                check_id,
                "pass" if ok else "fail",
                str(target),
                f"{lines} lines (limit: {threshold})",
            )

        if spec.kind == CheckKind.NO_FORBIDDEN:
            if target is None:
                return CheckResult(check_id, "fail", "path-policy", "target escapes workspace")
            text = target.read_text(encoding="utf-8")
            forbidden = [str(p) for p in spec.args.get("patterns", ())]
            found = [p for p in forbidden if p in text]
            return CheckResult(
                check_id,
                "pass" if not found else "fail",
                str(target),
                f"forbidden found: {found}" if found else "clean",
            )

        if spec.kind == CheckKind.JSON_PATH_EQUALS:
            if target is None:
                return CheckResult(check_id, "fail", "path-policy", "target escapes workspace")
            data = json.loads(target.read_text(encoding="utf-8"))
            path_parts = str(spec.args.get("path", "")).split(".")
            current = data
            for part in path_parts:
                if isinstance(current, dict):
                    current = current.get(part)
                elif isinstance(current, list) and part.isdigit():
                    current = current[int(part)]
                else:
                    current = None
                    break
            expected = spec.args.get("value")
            ok = current == expected
            return CheckResult(
                check_id,
                "pass" if ok else "fail",
                str(target),
                f"path={spec.args.get('path')} got={current!r} want={expected!r}",
            )

        if spec.kind == CheckKind.STRUCTURE_VALID:
            json.loads(target.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            return CheckResult(check_id, "pass", str(target))
        return CheckResult(
            check_id,
            "undetermined",
            spec.target,
            "requires verifier or human observation",
        )
    except (OSError, json.JSONDecodeError, re.error, subprocess.SubprocessError) as exc:
        return CheckResult(check_id, "undetermined", spec.target, str(exc))


def _command_timeout_seconds(spec: CheckSpec, deadline_budget: float | None) -> float:
    """Return a bounded command timeout (contract budget wins over defaults)."""
    requested = spec.args.get("timeout_seconds")
    try:
        if isinstance(requested, bool):
            raise ValueError
        timeout = float(requested) if requested is not None else 60.0
        if not math.isfinite(timeout):
            raise ValueError
    except (TypeError, ValueError):
        timeout = 60.0
    if deadline_budget is not None:
        try:
            budget = float(deadline_budget)
            if math.isfinite(budget):
                timeout = min(timeout, budget)
        except (TypeError, ValueError):
            pass
    return min(60.0, max(0.1, timeout))


__all__ = ["CheckResult", "evaluate_check"]
