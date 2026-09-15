"""仓库卫生：已跟踪文件不得命中 .gitignore（P2-6 回归）。

`.gitignore` 只能挡住未跟踪的文件；已经进过索引的文件会一直留在版本库里，
所以调试脚本、门日志这类"声明了不该入库"的东西在仓库里越积越多
（本次从索引里移走 67 个：`scratch-trash/` 54 个 + `quality/` 下 12 个
日志与 commit message 草稿 + `_move_list.json`，磁盘文件全部保留）。
它们里外都可能带着本机路径、会话内容或一次性的噪声，且让 `git clone`
的产物不再是发布物。

本测试钉住清理后的状态：`git ls-files -i -c --exclude-standard` 必须为空。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> list[str]:
    # argv 是固定元组，只调 git 本身，不接受用户输入。
    result = subprocess.run(
        ("git", *args),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_no_tracked_file_matches_gitignore() -> None:
    """已跟踪 + 被忽略 = 规则形同虚设，必须为零。"""
    offenders = _git("ls-files", "--ignored", "--cached", "--exclude-standard")
    assert not offenders, (
        "these files are tracked but matched by .gitignore; untrack them with "
        "`git rm --cached` (keep them on disk) instead of weakening the rules:\n"
        + "\n".join(offenders)
    )


def test_scratch_trash_stays_untracked() -> None:
    """一次性 scratch 目录不得再进索引（文件留在磁盘上无所谓）。"""
    tracked = _git("ls-files", "--", "scratch-trash", ".scratch-trash")
    assert not tracked, f"scratch dumps must not be versioned: {tracked}"
