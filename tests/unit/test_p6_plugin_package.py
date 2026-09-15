"""P6 插件包（SPEC §14）冒烟测试。

只验证静态清单：.codex-plugin/plugin.json 与 .mcp.json 存在、合法
JSON，并符合官方插件清单的 companion path 形状。
不改 pyproject 也不动代码（警惕累赘）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from longtask import __version__

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# 与 scripts/check_artifacts.py 的 _STRICT_SEMVER 同口径：Codex 插件清单要求
# 严格 SemVer，PEP 440 的 0.1.0a13 不合法。
_STRICT_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
# companion 自己的预发布号形状（longtask-contract 跟包版本走，flowgen 独立）。
_PEP440_PRERELEASE = re.compile(r"^\d+\.\d+\.\d+a\d+$")


class TestPluginManifest:
    def test_plugin_json_exists_and_is_valid_json(self) -> None:
        path = REPO_ROOT / ".codex-plugin" / "plugin.json"
        assert path.is_file(), f"missing {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "lhgp"
        # The Codex plugin manifest requires strict SemVer; the Python package
        # may remain on a prerelease while the plugin surface is in preview.
        #
        # 审计 C6：这里原先写 `data["version"] == "0.1.0"`——一条字面量断言。
        # 它既挡不住该挡的（把 0.1.0 改成合法的 0.2.0 也会红，于是合法升版被
        # 拦下），又没验注释声称的那件事。改为验**属性**，与
        # scripts/check_artifacts.py 的 _STRICT_SEMVER 同口径（PEP 440 的
        # `0.1.0a13` 不是合法 SemVer，必须被拒）。
        version = data["version"]
        assert isinstance(version, str)
        assert _STRICT_SEMVER.fullmatch(version), (
            f"plugin.json version {version!r} 不是严格 SemVer"
            "（Codex 插件清单不接受 0.1.0a13 这类 PEP 440 预发布号）"
        )
        assert data["skills"] == "skills"
        assert data["mcpServers"] == ".mcp.json"
        assert data["author"]["name"] == "LHGP maintainers"
        assert data["interface"]["displayName"] == "远期目标协议"

    def test_every_advertised_skill_has_an_entry_file(self) -> None:
        """plugin.json 声明整个 `skills` 目录，所以每个子目录都得能装载。

        没有 SKILL.md 的子目录对宿主来说不是 skill——声明了整个目录却放一个
        装不出来的子目录，等于对使用者撒谎。
        """
        data = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        skills_root = REPO_ROOT / data["skills"]
        assert skills_root.is_dir()
        dirs = sorted(p for p in skills_root.iterdir() if p.is_dir())
        assert dirs, f"{skills_root} 下没有任何 skill 子目录"
        for skill_dir in dirs:
            assert (skill_dir / "SKILL.md").is_file(), (
                f"{skill_dir.name} 没有 SKILL.md，装不成 skill"
            )

    def test_companion_manifests_are_consistent(self) -> None:
        """有 MANIFEST.json 的 companion 必须自洽（flowgen 原先完全没被测试）。

        不要求每个 skill 都有 MANIFEST：`long-horizon-goals` 只有 SKILL.md 是
        现状（宿主按 SKILL.md 装载即够）。但对**存在**的清单，版本号形状、
        协议版本与 entry 指向都要成立，否则模型工具链的索引会指向不存在的文件。
        """
        manifests = sorted((REPO_ROOT / "skills").glob("*/MANIFEST.json"))
        assert manifests, "skills/ 下没有任何 MANIFEST.json"
        seen: set[str] = set()
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            name = manifest["name"]
            assert name == manifest_path.parent.name, (
                f"{manifest_path} 的 name={name!r} 与目录名不符"
            )
            assert name not in seen, f"两个 companion 用了同一个 name: {name}"
            seen.add(name)
            assert manifest["protocol_version"] == "lhgp/v1alpha1"
            version = manifest["version"]
            assert isinstance(version, str)
            assert _PEP440_PRERELEASE.fullmatch(version), (
                f"{name} 的 version {version!r} 不是 0.1.0aN 形状"
            )
            entry = manifest_path.parent / manifest["entry"]
            assert entry.is_file(), f"{name} 的 entry {entry} 不存在"

    def test_plugin_referenced_skill_path_exists(self) -> None:
        data = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        skill_path = REPO_ROOT / data["skills"] / "long-horizon-goals" / "SKILL.md"
        assert skill_path.is_file(), f"missing skill at {skill_path}"
        skill = skill_path.read_text(encoding="utf-8")
        assert "`lhgp-mcp`" in skill

    def test_plugin_referenced_mcp_config_exists(self) -> None:
        data = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        mcp_path = REPO_ROOT / data["mcpServers"]
        assert mcp_path.is_file(), f"missing mcp config at {mcp_path}"

    def test_legacy_skill_manifest_matches_protocol(self) -> None:
        manifest = json.loads(
            (REPO_ROOT / "skills" / "longtask-contract" / "MANIFEST.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["version"] == __version__
        assert manifest["protocol_version"] == "lhgp/v1alpha1"

    def test_runtime_version_matches_the_packaged_version(self) -> None:
        """Release metadata and the runtime self-report may not drift.

        ``lhgp --version``, ``lhgp doctor`` and MCP ``implementation_version``
        all report ``longtask.__version__``.  It was last bumped at a6 while
        the package went on to ship a7-a10, so four releases were installed
        with ``doctor`` still printing ``v0.1.0a6``.  ``PROTOCOL_VERSION``
        is deliberately independent; the package version is not.
        """
        import tomllib

        with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
            project = tomllib.load(fh)["project"]
        assert project["name"] == "longtask-protocol"
        assert project["version"] == __version__, (
            f"pyproject.toml says {project['version']} but the runtime reports "
            f"{__version__}; bump longtask.__version__ and the companion "
            "skills/longtask-contract/MANIFEST.json together"
        )

        with (REPO_ROOT / "uv.lock").open("rb") as fh:
            lock = tomllib.load(fh)
        self_pins = [
            pkg
            for pkg in lock.get("package", [])
            if isinstance(pkg, dict) and pkg.get("name") == "longtask-protocol"
        ]
        assert self_pins, "uv.lock does not carry the project package"
        assert [pkg.get("version") for pkg in self_pins] == [__version__], (
            "uv.lock is stale; re-run `uv lock` after bumping the version"
        )


class TestMcpConfig:
    def test_mcp_json_valid_with_lhgp_server(self) -> None:
        path = REPO_ROOT / ".mcp.json"
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "mcpServers" in data
        assert "lhgp" in data["mcpServers"]
        server = data["mcpServers"]["lhgp"]
        assert server["type"] == "stdio"
        assert server["command"] == "lhgp-mcp"


class TestEntryPointAlignment:
    def test_legacy_entry_points_preserved(self) -> None:
        """P6 范围说明：longtask / longtaskd 旧名保留至 P6 末尾再迁移。

        本阶段：插件 manifest 引用的命令必须与 pyproject scripts 一致。
        """
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "longtask = " in pyproject
        assert "longtaskd = " in pyproject
        assert "longtask-mcp = " in pyproject

    def test_wheel_includes_plugin_companion_resources(self) -> None:
        """wheel 不能退化成只含 runtime 的包，必须携带模型接入资源。

        审计 C5：这里原先手写 3 条资源路径，于是 `skills/flowgen/` 的两个文件
        被漏在 wheel 之外，而 plugin.json 声明的是整个 `skills` 目录——从 wheel
        安装的插件静默少一个 skill。改为**从文件系统推导**必需清单：skills/ 下
        每一个文件都必须出现在 force-include 里，新增 skill 不可能再被漏掉。
        （制品层面的核对在 scripts/check_artifacts.py，它同样改为推导。）
        """
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for resource in _required_wheel_resources():
            assert f'"{resource}" = "{resource}"' in pyproject, (
                f"{resource} 未进 wheel force-include；从 wheel 安装的插件会缺这个文件"
            )


def _required_wheel_resources() -> list[str]:
    """必须在 wheel 里出现的 companion 资源（推导，不手写）。"""
    resources = [".codex-plugin/plugin.json", ".mcp.json"]
    resources += sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "skills").rglob("*")
        if path.is_file()
    )
    return resources
