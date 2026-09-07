---
title: Adding a Daemon Tick Hook
type: playbook
status: permanent
tags: [topic/daemon, type/howto, audience/ai]
audience: [human, ai]
requires: [python, longtask.cli.daemon_loop]
related: [[add-event-type]]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# Adding a Daemon Tick Hook

`run_daemon_loop` 每轮执行固定流程,新钩子必须挂在**正确位置**才生效:

```
1. reconcile_attempts        # 外部 attempt 回收
2. cancel_terminal_attempts   # 终态合同已派 attempt 终止
3. poll_attempts              # 心跳 / 续约 / 回收
4. consume_interrupt_requests  # 消费 control/interrupt
5. consume_verification_requests  # 消费 verification/requested
6. **enforce_deadlines**      # ← 你的钩子常挂这附近
7. run_daemon_tick            # 调度簿记
8. drain_notifications        # outbox 推送
9. start_attempts             # 真实拉起新 attempt
10. rtc.refresh / guard.update  # 唤醒
```

## 钩子模式

```python
def _my_hook(
    root: Path,
    conn: sqlite3.Connection,
    now: datetime,
    emit: Callable[[str], None] | None = None,
) -> list[Action]:
    """读 DB,做决策,落事件,emit 操作日志。

    必须是纯函数(不启动子进程,不写文件) — pure decision 留给上层 apply。
    """
    actions = []
    for contract in list_contracts(conn, ...):
        if _should_act(contract, now):
            actions.append(...)
    return actions
```

**关键约束**:
- 纯函数(只读 DB + 返回 action 列表),副作用留在 daemon_loop 里 apply
- 幂等(同 level 重扫不重复落事件)
- emit() 回调接 `render_text()` 给 operator log

## 挂载位置

- 在 `run_daemon_tick` **之前**:你的决策影响本轮调度(breached lock 类)
- 在 `drain_notifications` **之前**:你想被推到 outbox
- 在 `start_attempts` **之后**:你想看到本轮新建的 attempt(罕见)

P6 的 `_enforce_deadlines` 挂在第 6 步,breached 立即锁住新 attempt。

## 验证清单

- [ ] 纯函数(只读 DB + 返回 action)
- [ ] 幂等(同 level 重扫 no-op)
- [ ] 落事件用 `EventType.X.value` 不用字面量(见 [[sql-binding]])
- [ ] 集成测试:挂在 daemon_loop 里能跑通
- [ ] 边界 test: breached / warning / urgent / normal 四级都验
