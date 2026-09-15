"""Regression tests for release artifact metadata validation."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest
from scripts.check_artifacts import (
    _STRICT_SEMVER,
    _artifact_version,
    _partition_candidates,
    _validate_wheel_entrypoints,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "version",
    ("0.1.0", "1.2.3-alpha.1", "2.0.0+build.7", "1.2.3-rc.1+windows"),
)
def test_accepts_semver_versions(version: str) -> None:
    assert _STRICT_SEMVER.fullmatch(version)


@pytest.mark.unit
@pytest.mark.parametrize("version", ("0.1.0a0", "v1.2.3", "1.2", "01.2.3"))
def test_rejects_non_semver_versions(version: str) -> None:
    assert _STRICT_SEMVER.fullmatch(version) is None


@pytest.mark.unit
def test_wheel_requires_canonical_entrypoints(tmp_path: Path) -> None:
    wheel = tmp_path / "demo.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "lhgp = lhgp.cli.main:entrypoint\n"
            "lhgpd = lhgp.cli.daemon_proc:lhgpd_entrypoint\n"
            "lhgp-mcp = lhgp.mcp_server:main\n",
        )
    assert _validate_wheel_entrypoints(wheel) is None


@pytest.mark.unit
def test_wheel_reports_missing_canonical_entrypoint(tmp_path: Path) -> None:
    wheel = tmp_path / "demo.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\nlhgp = lhgp.cli.main:entrypoint\n",
        )
    error = _validate_wheel_entrypoints(wheel)
    assert error is not None
    assert "lhgpd" in error


@pytest.mark.unit
def test_artifact_version_parsed_from_both_distribution_names(tmp_path: Path) -> None:
    assert _artifact_version(tmp_path / "longtask_protocol-0.1.0a13-py3-none-any.whl") == "0.1.0a13"
    assert _artifact_version(tmp_path / "longtask_protocol-0.1.0a13.tar.gz") == "0.1.0a13"
    assert _artifact_version(tmp_path / "something-else.whl") is None


@pytest.mark.unit
def test_stale_artifacts_are_skipped_not_checked(tmp_path: Path) -> None:
    """dist/ 里的历史版本制品不能纳入本次候选检查。

    审计 C5 修复后首次运行 check_artifacts.py 时，dist/ 里的 a0 wheel 让脚本
    首错即退——新构建的 a13 制品根本没被检查。对陈旧制品报错会误导发布判断，
    而对它放行更糟（可能拿旧制品当候选证据）。
    """
    artifacts = [
        tmp_path / "longtask_protocol-0.1.0a0-py3-none-any.whl",
        tmp_path / "longtask_protocol-0.1.0a13-py3-none-any.whl",
        tmp_path / "longtask_protocol-0.1.0a13.tar.gz",
    ]
    stale, candidates = _partition_candidates(artifacts, "0.1.0a13")
    assert [path.name for path in stale] == ["longtask_protocol-0.1.0a0-py3-none-any.whl"]
    assert sorted(path.name for path in candidates) == [
        "longtask_protocol-0.1.0a13-py3-none-any.whl",
        "longtask_protocol-0.1.0a13.tar.gz",
    ]


@pytest.mark.unit
def test_unknown_current_version_checks_everything(tmp_path: Path) -> None:
    """读不到当前版本时不猜：全部纳入检查，宁可多查不可漏查。"""
    artifacts = [tmp_path / "longtask_protocol-0.1.0a0-py3-none-any.whl"]
    stale, candidates = _partition_candidates(artifacts, None)
    assert stale == []
    assert candidates == artifacts
