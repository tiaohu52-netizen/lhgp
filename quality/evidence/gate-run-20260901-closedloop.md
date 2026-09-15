# 质量门运行证据：常驻闭环 + 执行桥接 + 分层唤醒

- 日期：2026-09-01
- 命令：`uv run python scripts/quality_gate.py`（本地，与 CI 同一命令）
- 环境：Windows 11（10.0.26200），uv 0.11.16 管理的 CPython 3.13（uv sync --extra dev）
- 结果：**ALL PASS (7 gates)**

## 各门结果

| # | 门 | 结果 | 备注 |
|---|----|------|------|
| 1 | format | PASS | 61 文件全部已格式化（ruff format --check） |
| 2 | lint | PASS | ruff check 零违规 |
| 3 | arch | PASS | 架构依赖方向违规 0 / 基线 0 |
| 4 | deps | PASS | 运行时依赖 0；dev 依赖 6 个全部 == 锁定且在白名单 |
| 5 | claims | PASS | 14 条声明（13 verified, 1 accepted_debt, 0 blocking） |
| 6 | typecheck | PASS | mypy --strict，32 个源文件零问题 |
| 7 | test+coverage | PASS | 222 passed，总覆盖率 86.26%（≥70% 基线） |

## 本次提交序列（a184d77 / d5ff31a / 05ec3cd + 本次收尾）

1. **longtaskd 常驻生命周期**（DESIGN §3.3/§15.2）：`run_daemon_loop`
   注入式主循环；`longtask start` 分离后台进程 + pid/token，`stop` 优雅
   停止（stop 标记 → 宽限期 → SIGTERM）；Windows 探活改
   GetExitCodeProcess（os.kill(pid,0) 无法区分「已退出未收尸」）。
2. **执行桥接层 AttemptRunner**（DESIGN §3.4/§5.1/§7/§10，独立模块
   cli/runner.py 防巨石）：prepare 复验 + spawn 拉起 + session_ref 绑定；
   存活者心跳续约、租约换代记 stale、终态 collect 落事件并释放租约；
   dispatch 存在死租约先走 lease/reclaimed；预算按 attempt/started 事件
   计数（§6.3 硬边界）。
3. **分层唤醒 L0/L1**（DESIGN §6.4/ADR-0002，独立模块
   scheduler/wakeup.py）：L0 电源守卫（SetThreadExecutionState 端口化）、
   L1 计划任务注册（max(next_wakeup, deadline-margin)）、
   wakeup/* 事件词汇、任一层失效记 wakeup/degraded（fail-closed）。
   L2/L3 依赖外部基础设施未部署，记 accepted_debt 如实声明。
4. **claims/README 收尾**：新增 daemon-lifecycle-and-attempt-runner
   （verified，双集成证据）；strict-deadline-wakeup-design 从 deferred
   升为 accepted_debt（L0/L1 已实现 + L2/L3 边界与 debt_policy）；
   README 发布分级同步。

## 门真实拦下过的问题（本序列）

1. format：三次提交各有排版不合（长行折行），ruff format 修复。
2. lint：未用导入 ×6、unused noqa ×3、SIM105（try/except/pass →
   contextlib.suppress）、RUF002（docstring 全角负号）。
3. typecheck：Windows 平台收窄导致的 unused-ignore 与 unreachable。
4. test：Windows 探活误报（已退出未收尸的子进程被判存活 → 强杀抛
   WinError 5）；runner finished_count 漏计；budget 测试缺 spawn 失败
   释放租约步骤（暴露「测试意图 = 真实循环时序」的校准价值）。
5. claims：accepted_debt 的 debt_policy 必填字段（reentry_trigger、
   non_blocking）由 schema 强制——治理字段不允许手滑省略。
