"""claims 门的锚点校验回归（发布证据完整性）。

`quality/claims.json` 的 `pinned_sha` 是"这些实现声明在哪个候选上验证过"
的唯一锚点。2026-09-05 仓库做过一次脱敏历史重建（reflog：
`chore: repair identifiers after history sanitization`），此后所有旧
`pinned_sha` 与 `docs/evidence/*.md` 里引用的提交全部变成悬空对象，而门只
判断"不等于 unpinned"，于是在锚链断裂的情况下继续打印 OK。

本测试钉住修复后的行为：不可达 / 不存在 / 格式非法的锚点必须报错。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "claims_check.py"


def _load_claims_check() -> object:
    spec = importlib.util.spec_from_file_location("claims_check_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> object:
    assert SCRIPT.is_file(), f"claims gate script missing: {SCRIPT}"
    return _load_claims_check()


def _head() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_live_anchor_passes(gate: object) -> None:
    """当前 HEAD 是自身历史的祖先，合法锚点必须放行。"""
    assert gate.check_pinned_sha(REPO_ROOT, _head()) is None  # type: ignore[attr-defined]


def test_dropped_object_is_rejected(gate: object) -> None:
    """这正是 2026-09-05 之后的真实状态：对象被历史重写丢弃。

    用那个曾经骗过门的 SHA 作反例——它在本仓里已不存在。
    """
    dangling = "29833dc6928398565642578be1a952a80c536651"
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{dangling}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:  # pragma: no cover - depends on local object cache
        pytest.skip(f"{dangling} is present in this object store; cannot use as a dangling pin")
    error = gate.check_pinned_sha(REPO_ROOT, dangling)  # type: ignore[attr-defined]
    assert error is not None
    assert "does not exist in this repository" in error


def test_unpinned_and_malformed_anchors_are_rejected(gate: object) -> None:
    check = gate.check_pinned_sha  # type: ignore[attr-defined]
    assert check is not None
    for bad in ("unpinned", "", "deadbeef", None, 123):
        error = check(REPO_ROOT, bad)  # type: ignore[arg-type]
        assert error is not None, f"{bad!r} must not pass the anchor check"
        assert "pinned_sha" in error


def test_registry_currently_anchored_sha_is_reachable(gate: object) -> None:
    """门本身跑的就是这条路径：注册表里的锚点必须能在当前历史中解析。

    这条在 claims.json 被改坏（例如沿用重写前的旧 SHA）时会红。
    """
    import json

    registry = json.loads((REPO_ROOT / "quality" / "claims.json").read_text(encoding="utf-8"))
    pinned = registry.get("pinned_sha")
    if pinned == "unpinned":
        pytest.skip("registry explicitly declares it is not anchored yet")
    error = gate.check_pinned_sha(REPO_ROOT, pinned)  # type: ignore[attr-defined]
    assert error is None, error
