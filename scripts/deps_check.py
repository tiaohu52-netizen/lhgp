#!/usr/bin/env python3
"""依赖白名单门（DESIGN §13.1、CONTRIBUTING「质量门」）。

规则：
1. pyproject.toml 的 runtime dependencies 必须为空（零运行时依赖；
   第一个例外需要 ADR + 本白名单 runtime.allow 登记）。
2. dev 依赖必须在 allowed-deps.json 登记，且全部 == 锁定。
3. 白名单里登记了但 pyproject 里没有的，打印提醒（文档腐坏，不阻断）。
fail-closed：文件读不到、TOML/JSON 解析失败 → 报错退出。
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
ALLOWLIST = REPO_ROOT / "scripts" / "allowed-deps.json"

PINNED = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+$")


def dep_name(spec: str) -> str:
    return re.split(r"[=<>!~;\[]", spec, maxsplit=1)[0].strip().lower()


def main() -> int:
    try:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"[deps] cannot read pyproject.toml (fail-closed): {exc}", file=sys.stderr)
        return 1
    try:
        allow = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[deps] cannot read allowed-deps.json (fail-closed): {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    project = pyproject.get("project", {})

    runtime: list[str] = project.get("dependencies", [])
    runtime_allow = set(allow.get("runtime", {}).get("allow", {}))
    for spec in runtime:
        name = dep_name(spec)
        if name not in runtime_allow:
            errors.append(
                f"runtime dependency '{spec}' not in allowlist (zero-runtime-deps policy)"
            )

    dev: list[str] = project.get("optional-dependencies", {}).get("dev", [])
    dev_allow = set(allow.get("dev", {}).get("allow", {}))
    for spec in dev:
        name = dep_name(spec)
        if name not in dev_allow:
            errors.append(f"dev dependency '{spec}' not registered in allowed-deps.json")
        if not PINNED.match(spec):
            errors.append(f"dev dependency '{spec}' must be pinned with ==")

    for name in sorted(dev_allow - {dep_name(s) for s in dev}):
        print(f"[deps] note: allowlisted dev tool '{name}' not present in pyproject (stale entry)")

    if errors:
        print("[deps] violations:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    print(f"[deps] OK: runtime={len(runtime)} dev={len(dev)} all pinned and allowlisted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
