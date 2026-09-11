#!/usr/bin/env python3
"""示例核验者：独立程序，无模型、无 API key。

协议面（SPEC §12.4 通道 2）：headless verifier 无法回调 RPC，约定在 stdout
末尾写一个机器可读的 ``lhgp-verdict`` 判定块，运行时解析最后一个块。

判定块里的 ``check_id`` 与合同的 typed check 同名（``<kind>:<target>``，
见 lhgp/acceptance/evaluator.py），因此协议侧确定性评估与本次观察可以在
``merge_evidence`` 里按同一 id 对齐（确定性结果优先，模型观察不覆盖它）。
"""

from __future__ import annotations

import json
import pathlib

DELIVERABLE = "result.txt"
MARKER = "LHGP-LOCAL-EXAMPLE-OK"


def main() -> int:
    target = pathlib.Path.cwd() / DELIVERABLE
    ok = target.is_file() and MARKER in target.read_text(encoding="utf-8")
    check_id = f"file-exists:{DELIVERABLE}"
    verdict = {
        "verdict": "succeeded" if ok else "failed",
        "checks": [
            {
                "check_id": check_id,
                "outcome": "pass" if ok else "fail",
                "source": str(target),
                "details": "交付物存在且含标记" if ok else "交付物缺失或不含标记",
            }
        ],
    }
    print("核验完成。")
    print("```lhgp-verdict")
    print(json.dumps(verdict, ensure_ascii=False))
    print("```")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
