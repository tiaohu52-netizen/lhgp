"""P6 插件包（SPEC §14）冒烟测试。

只验证静态清单：.codex-plugin/plugin.json 与 .mcp.json 存在、合法
JSON，并符合官方插件清单的 companion path 形状。
不改 pyproject 也不动代码（警惕累赘）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from longtask import __version__

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestPluginManifest:
    def test_plugin_json_exists_and_is_valid_json(self) -> None:
        path = REPO_ROOT / ".codex-plugin" / "plugin.json"
        assert path.is_file(), f"missing {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "lhgp"
        # The Codex plugin manifest requires strict SemVer; the Python package
        # may remain on a prerelease while the plugin surface is in preview.
        assert data["version"] == "0.1.0"
        assert data["skills"] == "skills"
        assert data["mcpServers"] == ".mcp.json"
        assert data["author"]["name"] == "LHGP maintainers"
        assert data["interface"]["displayName"] == "远期目标协议"

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
        """wheel 不能退化成只含 runtime 的包，必须携带模型接入资源。"""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for resource in (
            '".codex-plugin/plugin.json" = ".codex-plugin/plugin.json"',
            '".mcp.json" = ".mcp.json"',
            '"skills/long-horizon-goals/SKILL.md" = "skills/long-horizon-goals/SKILL.md"',
        ):
            assert resource in pyproject
