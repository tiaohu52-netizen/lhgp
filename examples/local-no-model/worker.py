#!/usr/bin/env python3
"""示例执行者：普通程序，无模型、无 API key。

协议面（DESIGN §12.1、SPEC §12.4 通道 1 的 stdout 形态）：
- 工作目录由适配器绑定为合同 workspace_root（cwd）；
- 干完活在 stdout 写一行 ``attempt/finished`` 事件声明终态——一次性 CLI
  harness 无法回调 RPC，事件行就是它的完成声明（适配器持续扫 stdout）。

本脚本刻意最小：它证明「任何能读得懂入场提示词、命令行可拉起的程序」
都能作为执行者接入，不依赖任何模型账号。
"""

from __future__ import annotations

import json
import pathlib
import sys

DELIVERABLE = "result.txt"
MARKER = "LHGP-LOCAL-EXAMPLE-OK"


def main() -> int:
    # cwd = workspace_root（适配器绑定，DESIGN §9 编译表）
    target = pathlib.Path.cwd() / DELIVERABLE
    target.write_text(
        f"{MARKER}\n"
        "produced by examples/local-no-model/worker.py —— 无模型、无网络、无密钥\n",
        encoding="utf-8",
    )
    # 终态事件行：必须独占一行、可被 json 解析（适配器按前缀扫描）
    # 紧凑 JSON（分隔符无空格）：适配器按前缀扫描事件行，带空格的序列化
    # 会扫不到——这是实测踩到的坑，示例按协议写紧凑形式（产品侧另有容错）。
    print(
        json.dumps(
            {"event": "attempt/finished", "outcome": "succeeded", "returncode": 0},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    print(f"wrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
