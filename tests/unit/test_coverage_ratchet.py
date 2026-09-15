"""覆盖率棘轮基线的回归（P1-4）。

`--cov-fail-under` 曾经是 `scripts/quality_gate.py` 里写死的 70，而实测覆盖率
早在 77 上下——门绿着，覆盖率却可以从 77 掉到 70.1 而无人报警。CONTRIBUTING
「棘轮」条款要求存量债记在 `quality/` 下、只许收紧不许放松，本测试就是让这条
纪律对覆盖率真正生效：

- 阈值来自 `quality/coverage-baseline.json`，脚本里没有硬编码数字；
- 阈值不得超过最近一次实测值（不许写成做不到的目标）；
- 相对上一次提交，阈值只许上调（下调必须写明理由，走 review 而不是静默改数）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "quality_gate.py"
BASELINE = REPO_ROOT / "quality" / "coverage-baseline.json"

# 门脚本改自 `--cov-fail-under=70` 之前的历史硬编码值；新阈值不得低于它。
LEGACY_HARDCODED_FLOOR = 70


def _load_gate() -> object:
    spec = importlib.util.spec_from_file_location("quality_gate_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` + ``from __future__ import annotations`` resolves field
    # types through ``sys.modules[cls.__module__]`` at class-creation time, so
    # the module must be registered before exec — otherwise the import blows up
    # inside dataclasses rather than in the code under test.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture(scope="module")
def gate() -> object:
    assert SCRIPT.is_file(), f"gate script missing: {SCRIPT}"
    return _load_gate()


@pytest.fixture(scope="module")
def baseline() -> dict[str, object]:
    assert BASELINE.is_file(), f"coverage baseline missing: {BASELINE}"
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "baseline must be a JSON object"
    return data


def test_floor_is_read_from_the_baseline(gate: object, baseline: dict[str, object]) -> None:
    floor = gate.coverage_fail_under()  # type: ignore[attr-defined]
    assert floor is not None, "baseline unreadable: the gate must fail closed, not default"
    assert floor == f"{baseline['fail_under']:g}"


def test_script_has_no_hardcoded_floor() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    literal = re.search(r"--cov-fail-under=(\d+(?:\.\d+)?)", source)
    assert literal is None, (
        f"--cov-fail-under must come from {BASELINE.name}, "
        f"found hardcoded {literal.group(0) if literal else ''}"
    )


def test_gates_wire_the_floor_into_pytest(gate: object) -> None:
    test_gate = next(g for g in gate.build_gates("88") if g.label == "test+coverage")  # type: ignore[attr-defined]
    assert "--cov-fail-under=88" in test_gate.argv


def test_floor_is_reachable_and_not_below_the_legacy_value(baseline: dict[str, object]) -> None:
    floor = baseline["fail_under"]
    measured = baseline["last_measured"]
    assert isinstance(floor, (int, float)) and isinstance(measured, (int, float))
    assert 0 <= floor <= 100
    assert floor >= LEGACY_HARDCODED_FLOOR, (
        f"floor {floor} is below the previous hard-coded {LEGACY_HARDCODED_FLOOR}"
    )
    assert floor <= measured, (
        f"floor {floor} exceeds the last measured coverage {measured}; "
        "an aspirational baseline is not a ratchet"
    )


def test_floor_only_ratchets_upward(baseline: dict[str, object]) -> None:
    """相对 HEAD 的上一版基线，阈值只许上调。"""
    probe = subprocess.run(
        ["git", "show", f"HEAD:{BASELINE.relative_to(REPO_ROOT).as_posix()}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if probe.returncode != 0:
        pytest.skip("baseline is new in this commit; nothing to ratchet against yet")
    previous = json.loads(probe.stdout)
    assert float(baseline["fail_under"]) >= float(previous["fail_under"]), (
        f"coverage floor loosened from {previous['fail_under']} to "
        f"{baseline['fail_under']}; tightening-only is the rule"
    )
