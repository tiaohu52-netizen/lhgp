---
title: Quality Gate
type: playbook
status: permanent
tags: [topic/quality, type/howto, audience/ai]
audience: [human, ai]
related: [[write-test-first]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Quality Gate

`scripts/quality_gate.py` 跑 7 门,任何一门失败立刻停(fail-closed)。本地和 CI
**同一命令**——这是协议根的硬规矩。

## 7 门顺序

| # | 门 | 工具 | 失败含义 |
|---|----|------|---------|
| 1 | format | `ruff format --check` | 代码风格不合 |
| 2 | lint | `ruff check` | 规则违规(规则集在 pyproject.toml) |
| 3 | arch | `scripts/arch_check.py` | 模块依赖方向违反四平面边界 |
| 4 | deps | `scripts/deps_check.py` | 依赖不在白名单 / 版本未锁定 |
| 5 | claims | `scripts/claims_check.py` | 质量声明(claims)与证据不符 |
| 6 | typecheck | `mypy --strict` | 类型债 |
| 7 | test+coverage | `pytest --cov`,覆盖率棘轮 | 测试失败或覆盖率低于基线(目前 70%) |

**顺序意义**:format → lint 是 cheapest,先快速失败;arch / deps / claims 检查
**声明与现实是否一致**(protocol 级的 spec drift);typecheck → test 是 expensive,
最后跑。

## 本地运行

```bash
# 完整(同 CI)
uv run python scripts/quality_gate.py

# 单门快查
uv run ruff format src tests
uv run ruff check src tests
uv run mypy
uv run pytest
```

## fail-closed 语义

- 任何门失败 → gate 返回非零 → 提交自动被 CI 拒绝 → PR 不能合
- **不要**在本地"快速 commit, 后面再补" —— `fix/machine-only-acceptance` P6 收尾
  一轮就是反例,10 个 commit 修 lint/typecheck 债
- **单测失败先看 test 自身,不要**改源码让测试通过(除非测试假设错了)

## 反模式

- **skip 某个门加 `[skip ci]` 标记**:严重 debt 化,**禁止**
- **降级 coverage 阈值**:`--cov-fail-under=70` 改 60 —— 在 quality-evidence 里写清楚
  理由和回填计划
- **删旧测试让新测试过**:`tests/` 是契约,删测试等于改契约
- **`# type: ignore` 大面积使用**:mypy 债会越积越多,引入新债时同时开 issue

## 与 CLAIMS 的关系

`scripts/claims_check.py` 验证:
- `LHGP-SPEC.md` 声明的协议能力 vs 代码实际实现
- `CLAIMS.json` 注册的能力 vs `tests/` 覆盖
- `evidence/` 目录的测试报告 vs 协议 milestone

CLAIM 改了 → 同步 SPEC / 实现 / 测试 / evidence。**单边漂移**是协议 spec drift
的最大来源。

## 验证清单(改完代码后自检)

- [ ] `uv run ruff format src tests` 无变化
- [ ] `uv run ruff check src tests` 0 errors
- [ ] `uv run mypy` 0 errors
- [ ] `uv run pytest` 全过
- [ ] coverage 不降
- [ ] claims_check 0 missing
