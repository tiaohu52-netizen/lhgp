# 本地无模型示例 / Local Example Without a Model

一个**可复制**的最小全链路演示：`doctor → prepare → approve → execute → verify → satisfied`。
执行者与核验者是两个普通程序（`worker.py` / `checker.py`），**不需要模型账号、
不需要网络、不需要任何密钥**。

A reproducible end-to-end walkthrough of `doctor → prepare → approve → execute →
verify → satisfied`. The executor and verifier are two plain programs
(`worker.py` / `checker.py`) — **no model account, no network, no API key**.

```bash
uv run python examples/local-no-model/run_example.py
```

预期输出（节选）：

```
=== doctor ===  CLI 自检
[PASS] python_runtime / storage_directory / database_integrity / executor_registry / kill_switch
=== registry ===  注册两个本地程序（执行者 / 独立核验者）
=== prepare ===   起草合同
=== approve ===   用户批准（drafted → active）
=== execute / verify ===  真实调度轮次派工：worker.py → checker.py
  promoter/dispatched:lt-local-example:local-worker
  runner/attempt-succeeded:lt-local-example:att-…-mple
  runner/verifier-spawned:lt-local-example:ver-…-mple
  runner/attempt-succeeded:lt-local-example:ver-…-mple
  attempt: role=executor  state=succeeded  executor=local-worker
  attempt: role=verifier  state=succeeded  executor=local-checker
=== result ===  终态 complete
完成：doctor → prepare → approve → execute → verify → satisfied 全链路通过。
```

## 它演示了什么 / What it demonstrates

| 环节 | 由谁做 | 走的哪条协议面 |
|---|---|---|
| 起草 + 批准 | CLI | `contract/prepare` → `contract/approve`（Principal 门） |
| 派工 | 真实调度主循环 `run_daemon_loop` | 紧迫度阶梯 + 租约 + 预算 |
| 执行 | `worker.py`（subprocess 执行者） | workspace 内产出 `result.txt` + stdout 写 `attempt/finished` 事件行 |
| 验收 | `checker.py`（**独立** verifier attempt） | stdout 写 `lhgp-verdict` 判定块（SPEC §12.4 通道 2） |
| 终态 | 调度器判定 | verifier 证据 → `acceptance.passed` → 合同终态 |

**不使用人工补写成功事件的 harness**：所有状态变迁都由调度器与验收路径产生。
集成测试 `tests/integration/test_local_no_model_example.py` 对此有硬断言
（`attempt/*` 与 `verification/*` 事件里不存在 `actor=user` 的记录）。

## 两条 stdout 协议通道 / The two stdout channels

一次性 CLI 程序无法回调 RPC，因此协议为它们留了两条 stdout 通道：

1. **执行者的完成声明**——独占一行的 JSON：

   ```json
   {"event":"attempt/finished","outcome":"succeeded","returncode":0}
   ```

   适配器持续扫 stdout 找这一行。**请用紧凑 JSON**（`json.dumps(..., separators=(",", ":"))`）；
   序列化器的空格差异已由适配器容错处理（见 `FINISHED_EVENT_RE`），但紧凑形式是契约形态。

   **推荐再带上凭据**：spawn 时注入的 per-attempt token 就在环境里
   （`LHGP_SESSION_TOKEN`），把它放进事件行能让这次完成在审计里标记为
   `completion_attested=true`——与「任何打印出事件行的回显」区分开。
   不带仍然被采信（存量 harness 不受影响）；带了但不对，该行会被拒绝。

2. **核验者的判定块**——stdout 末尾的围栏块：

   ````
   ```lhgp-verdict
   {"verdict": "succeeded", "checks": [{"check_id": "file-exists:result.txt", "outcome": "pass", "source": "..."}]}
   ```
   ````

   `check_id` 与合同 typed check 同名（`<kind>:<target>`），协议侧的确定性评估与
   本次观察据此对齐；确定性结果优先，模型观察不覆盖它。

## 排查出口 / Troubleshooting

失败时示例会打印这些命令（也可手动执行）：

```bash
lhgp --data-dir <data-dir> get lt-local-example     # 合同现状：为什么 blocked
lhgp --data-dir <data-dir> stats lt-local-example   # 成本台账与 attempt 分布
```

深入看事件流：

```bash
python -c "import sqlite3;db=sqlite3.connect(r'<data-dir>/state.db');\
print(*db.execute('select event_type,actor,payload_json from events order by event_id'),sep='\n')"
```

### 两个已知的、非缺陷的行为

- **每轮新建 runner ≈ 重启守护进程**：若你改成「循环调用 `run_daemon_loop(max_cycles=1)`」，
  在飞的 attempt 会被 reconcile 判为 detached（退出码不可回收）→ `failed`。
  这是 SPEC §11.3 对**跨进程崩溃恢复**的诚实边界（新进程拿不到旧进程的管道与退出码），
  不是 bug。示例因此只跑**一个连续会话**。
- **模拟时钟推不动子进程**：示例用注入时钟推进"合同时间"，但子进程需要**真实**时间退出；
  两者都要给（见 `SIMULATED_STEP_SECONDS` 与 `REAL_YIELD_SECONDS`）。

### 输出被截断？/ Truncated evidence?

若把 `budget.max_output_bytes` 调得很小，核验者的判定块（在 stdout 末尾）可能被
输出预算截掉。协议要求这种情况被**显式**记录为「判定块随预算截断丢失」
（`verdict_source_loss`，SPEC §12.4），而不是静默当作"verifier 没写"。遇到它：
调大 `max_output_bytes` 重跑。

## 目录 / Layout

```
run_example.py   驱动脚本（doctor → prepare → approve → 调度会话 → 断言）
worker.py        示例执行者：写 result.txt + 完成事件行
checker.py       示例核验者：读 result.txt + 判定块
example-data/    默认数据目录（生成物，已 gitignore）
```

`--data-dir` 可指向任意目录，示例之间互不影响。
