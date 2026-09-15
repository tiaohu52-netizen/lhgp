#!/usr/bin/env python3
"""质量门主编排（CONTRIBUTING「质量门」）。

本地与 CI 同一命令：uv run python scripts/quality_gate.py
固定顺序，任一失败即停；fail-closed：工具缺失/环境不对一律报错退出，
绝不假装通过。快路径（pre-commit 增量钩子）的通过不代表本门通过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_BASELINE = REPO_ROOT / "quality" / "coverage-baseline.json"


def coverage_fail_under() -> str | None:
    """Return the coverage floor from :data:`COVERAGE_BASELINE`, or None.

    The floor lives in the baseline file rather than in this script so the
    ratchet is visible in `quality/` (CONTRIBUTING「棘轮」) and can only move
    upward.  None means the baseline is missing/unparseable/out of range,
    which the caller must treat as a gate failure rather than falling back
    to a permissive default.
    """
    try:
        raw: Any = json.loads(COVERAGE_BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = raw.get("fail_under") if isinstance(raw, dict) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not 0 <= value <= 100:
        return None
    return f"{value:g}"


@dataclass(frozen=True, slots=True)
class Gate:
    label: str
    argv: tuple[str, ...]


def build_gates(cov_fail_under: str) -> list[Gate]:
    py = sys.executable
    return [
        # 使用门禁自身的解释器加载 Ruff，避免 uv/venv 已安装但 PATH 未暴露
        # 可执行文件时产生假阴性；模块缺失仍由 subprocess fail-closed。
        Gate("format", (py, "-m", "ruff", "format", "--check", "src", "tests", "scripts")),
        Gate("lint", (py, "-m", "ruff", "check", "src", "tests", "scripts")),
        Gate("arch", (py, "scripts/arch_check.py")),
        Gate("deps", (py, "scripts/deps_check.py")),
        Gate("claims", (py, "scripts/claims_check.py")),
        Gate("typecheck", (py, "-m", "mypy")),
        Gate(
            "test+coverage",
            (
                py,
                "-m",
                "pytest",
                "--cov=src/longtask",
                "--cov=src/lhgp",
                "--cov-report=term-missing",
                f"--cov-fail-under={cov_fail_under}",
            ),
        ),
    ]


def run_gate(gate: Gate) -> int:
    print(f"\n[gate] >>> {gate.label}", flush=True)
    executable = gate.argv[0]
    if executable != sys.executable and shutil.which(executable) is None:
        # fail-closed：工具缺失绝不跳过
        print(f"[gate] {gate.label}: tool '{executable}' not found; refusing to pass.", flush=True)
        return 1
    result = subprocess.run(gate.argv, cwd=REPO_ROOT, check=False)
    if result.returncode != 0:
        print(f"[gate] STOP after {gate.label} (exit {result.returncode})", flush=True)
        return result.returncode
    print(f"[gate] PASS {gate.label}", flush=True)
    return 0


def main() -> int:
    cov_fail_under = coverage_fail_under()
    if cov_fail_under is None:
        # fail-closed：基线读不出来就不跑测试门，绝不退回宽松默认值
        print(
            f"[gate] cannot read coverage floor from {COVERAGE_BASELINE}; refusing to pass.",
            flush=True,
        )
        return 1
    gates = build_gates(cov_fail_under)
    print(f"[gate] authoritative sequence ({len(gates)} gates), repo={REPO_ROOT}", flush=True)
    for gate in gates:
        status = run_gate(gate)
        if status != 0:
            return status
    print(f"\n[gate] ALL PASS ({len(gates)} gates)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
