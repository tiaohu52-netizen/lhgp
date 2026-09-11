"""Verify that built distributions retain the plugin companion resources."""

from __future__ import annotations

import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _required_members() -> set[str]:
    """从仓库树推导制品里必须出现的 companion 资源。

    手写清单会漂移：``skills/flowgen/`` 就曾被漏在 wheel 之外，而
    ``.codex-plugin/plugin.json`` 声明的是整个 ``skills`` 目录——从 wheel
    安装的插件因此静默少一个 skill（审计 C5）。改为推导后，新增一个 skill
    文件不可能再被漏掉。
    """
    members = {".codex-plugin/plugin.json", ".mcp.json"}
    skills = REPO_ROOT / "skills"
    if skills.is_dir():
        members |= {
            path.relative_to(REPO_ROOT).as_posix() for path in skills.rglob("*") if path.is_file()
        }
    return members


REQUIRED = _required_members()
CANONICAL_ENTRYPOINTS = {
    "lhgp = lhgp.cli.main:entrypoint",
    "lhgpd = lhgp.cli.daemon_proc:lhgpd_entrypoint",
    "lhgp-mcp = lhgp.mcp_server:main",
}
_ARTIFACT_PREFIX = "longtask_protocol-"
_ARTIFACT_SUFFIXES = ("-py3-none-any.whl", ".tar.gz")
_STRICT_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _current_version() -> str | None:
    """pyproject 里的项目版本，用于判定制品是否属于本次候选。"""
    try:
        import tomllib

        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None


def _artifact_version(path: Path) -> str | None:
    """从制品文件名取版本：``longtask_protocol-0.1.0a13-py3-none-any.whl``。"""
    stem = path.name
    for suffix in _ARTIFACT_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if not stem.startswith(_ARTIFACT_PREFIX):
        return None
    version = stem[len(_ARTIFACT_PREFIX) :]
    return version or None


def _partition_candidates(
    artifacts: list[Path], current: str | None
) -> tuple[list[Path], list[Path]]:
    """按当前版本切分制品，返回 (stale, candidates)。

    ``dist/`` 常留着历史版本的构建产物。对着陈旧制品报错（本脚本首错即退时
    就会这样）或**对着陈旧制品放行**都会误导发布判断——两种都真实发生过：
    审计 C5 修复后首次运行就撞上 dist 里的 a0 wheel 并因此提前退出，而新制品
    根本没被检查。所以陈旧制品明确跳过并打印，匹配不到当前版本才算失败。
    """
    if current is None:
        return [], list(artifacts)
    stale = [path for path in artifacts if _artifact_version(path) != current]
    candidates = [path for path in artifacts if _artifact_version(path) == current]
    return stale, candidates


def _names(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {name for name in archive.namelist() if name in REQUIRED}
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return {
                member.name.split("/", 1)[1]
                for member in archive.getmembers()
                if "/" in member.name and member.name.split("/", 1)[1] in REQUIRED
            }
    raise ValueError(f"unsupported artifact: {path}")


def _read_member(path: Path, target: str) -> bytes:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.read(target)
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if "/" in member.name and member.name.split("/", 1)[1] == target:
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
    raise KeyError(f"{target} not found in {path}")


def _validate_companion_metadata(path: Path) -> str | None:
    try:
        plugin = json.loads(_read_member(path, ".codex-plugin/plugin.json"))
        mcp = json.loads(_read_member(path, ".mcp.json"))
    except (KeyError, json.JSONDecodeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return f"invalid companion metadata: {exc}"
    version = plugin.get("version")
    if not isinstance(version, str) or _STRICT_SEMVER.fullmatch(version) is None:
        return f"plugin manifest version is not strict SemVer: {version!r}"
    if plugin.get("id") != "lhgp" or plugin.get("name") != "lhgp":
        return "plugin manifest id/name must both be 'lhgp'"
    server = mcp.get("mcpServers", {}).get("lhgp", {})
    if server.get("command") != "lhgp-mcp":
        return "MCP companion must expose the canonical 'lhgp-mcp' command"
    return None


def _validate_wheel_entrypoints(path: Path) -> str | None:
    if path.suffix != ".whl":
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_name = next(
                name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
            )
            entries = {
                line.strip()
                for line in archive.read(metadata_name).decode("utf-8").splitlines()
                if " = " in line
            }
    except (StopIteration, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        return f"wheel entry-point metadata is invalid: {exc}"
    missing = CANONICAL_ENTRYPOINTS - entries
    return f"wheel missing canonical entry points: {sorted(missing)}" if missing else None


def _check_one(artifact: Path) -> list[str]:
    """单个制品的全部问题（不首错即退——一次跑完给全量结论）。"""
    problems: list[str] = []
    missing = REQUIRED - _names(artifact)
    if missing:
        problems.append(f"{artifact}: missing {sorted(missing)}")
        return problems
    metadata_error = _validate_companion_metadata(artifact)
    if metadata_error:
        problems.append(f"{artifact}: {metadata_error}")
    entrypoint_error = _validate_wheel_entrypoints(artifact)
    if entrypoint_error:
        problems.append(f"{artifact}: {entrypoint_error}")
    return problems


def main() -> int:
    dist = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist")
    artifacts = sorted((*dist.glob("*.whl"), *dist.glob("*.tar.gz")))
    if not artifacts:
        print(f"no distribution artifacts found in {dist}", file=sys.stderr)
        return 1

    current = _current_version()
    stale, candidates = _partition_candidates(artifacts, current)
    if stale:
        print(
            f"skipping {len(stale)} artifact(s) not matching current version {current}: "
            f"{[path.name for path in stale]}"
        )
    if not candidates:
        print(
            f"no artifacts matching current version {current or '<unknown>'} in {dist}; "
            "build the candidate first (uv build)",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    for artifact in candidates:
        problems = _check_one(artifact)
        if problems:
            failures.extend(problems)
        else:
            print(f"{artifact}: companion resources OK")
    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
