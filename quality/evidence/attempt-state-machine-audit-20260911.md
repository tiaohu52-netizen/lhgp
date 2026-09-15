# attempt 状态机强制面核查 —— 2026-09-11

核查者：审计整改第八轮
核查对象：`src/lhgp/contracts/state_machine.py::ATTEMPT_LEGAL_TRANSITIONS` 与
attempt 状态的全部写入点
起因：审计 B1（合同状态机在唯一写入口强制）落地后，发现 attempt 侧有对称的疑点——
`is_valid_attempt_transition` 在 `src/` 下**零调用**，`set_attempt_state` 也不校验。

## 1. 结论摘要

- **attempt 侧不是 B1 那样的缺陷，而是明写的设计**。`set_attempt_state` 的 docstring：
  「不在此处校验迁移合法性：状态机是纯函数（state_machine.py），调用方负责先判定；
  本模块只做忠实落库。」按 ABC 分类属 **B 类（设计如此）**，未据此改代码。
- 逐一核对全部写入点后：**没有任何一处写入表外的非法迁移**——但**必须允许自反写入**，
  否则最常见的恢复路径会断（见 §3）。
- 因此 attempt 状态机在代码层面是**建议性的**（advisory）：表存在、函数存在、
  但无人调用。这是一个**可选加固点**，不是已发生的错误。

## 2. 写入点全量清单（`grep 'UPDATE attempts'`）

`src/` 下共 10 处 `UPDATE attempts`，分布在 4 个文件：

| 位置 | 写入 | 前置条件 | 表内？ |
|---|---|---|---|
| `cli/runner.py:439` `set_attempt_state` | `→ running` | 刚 `register_attempt_handle`，attempt 处于 starting/admitted | ✓ |
| `cli/runner.py:855` 直写 | `→ payload["state"]`（终态） | 外部进程已收尾，attempt 在飞 | ✓ |
| `cli/runner.py:918` 直写 | `→ failed` | 收集/执行失败路径，attempt 在飞 | ✓ |
| `cli/runner.py:951` 直写 | `→ stale` | `_mark_stale`：进程失联 | ✓ |
| `cli/runner.py:1012` `set_attempt_state` | `→ cancelled` | 用户取消，attempt 在飞 | ✓ |
| `cli/runner.py:496` 直写 | 只写 `session_token_hash` | 不改 state | — |
| `promoter/reconcile.py:319` `set_attempt_state` | `→ running` | `_reattach`：确证同一外部 run 仍活着 | ✓ / **自反见 §3** |
| `promoter/reconcile.py:464` `set_attempt_state` | `→ state`（终态结算） | `_collect`：外部 run 已确认收尾 | ✓ |
| `promoter/reconcile.py:507` `mark_attempt_orphaned` | `→ orphaned` | `_orphan`：外部状态未知 | ✓ |
| `persistence/store.py:1993/2001` 直写 | 只写 `model_id` / `usage_json` | 不改 state | — |
| `persistence/attempts.py:236`（`register_attempt_handle`） | 只写句柄列 | 不改 state（docstring：state 由 `set_attempt_state` 单独负责，避免两处写状态） | — |
| `persistence/attempts.py:277`（`set_attempt_state`） | `→ 调用方指定的 state` | 忠实落库，不校验（§1） | 调用方保证 |
| `persistence/attempts.py:317`（`mark_attempt_orphaned`） | `→ orphaned` | 幂等：已有 `orphaned_at` 时不覆盖（重扫不能无限续期） | ✓ |

另有两处 `mark_attempt_orphaned` 调用点（`runner.py:604`、`reconcile.py:507`），都走
`attempts.py:317` 那条 `UPDATE attempts SET state='orphaned'`。注意 `reconcile._orphan`
里的调用是**无条件**的，只有**事件**被 `first_time` 守卫（`reconcile.py:506/514`）——
语义由函数的幂等性承担，而不是由调用点守卫。

## 3. 两个必须知道的边界（否则加固会踩雷）

### 3.1 自反写入是真实且必需的

`ATTEMPT_LEGAL_TRANSITIONS` 的**终态无出边、非终态也无自反边**：

```python
AttemptState.RUNNING: frozenset({WAITING, SUCCEEDED, FAILED, CANCELLED, STALE, ORPHANED})
```

而 `reconcile._reattach`（`reconcile.py:319`）在**确证外部 run 仍活着**时写
`state=RUNNING` —— 守护进程重启后接管一个**本来就处于 running** 的 attempt，正是这条
路径最常见的场景。也就是说：**如果照表 fail-closed 强制而不放行自反写入，守护进程
重启后的接管会直接抛错**。

这与合同侧 B1 的实测结论一致（src 侧 13 次自反写入、`tick._judge_verifier_outcomes`
靠自反写入更新 `acceptance_status`），所以 B1 的实现在写入口明确放行 `X→X`。

### 3.2 看似非法的 `orphaned → running` 实际不可达

`_reattach` 写 `→ running`，而 `ORPHANED` 在表里是终态。核查一度怀疑存在
`orphaned → running` 的非法迁移，**实测推翻**：

```python
# reconcile.py:173-177
# 已在宽限中：只做宽限维护或到期 fence（分支 3 续 / 分支 4）
if attempt.state == AttemptState.ORPHANED.value:
    return _sweep_orphan(...)      # 提前返回，永不进入 _reattach
```

宽限期内的 attempt 只走 `_sweep_orphan`（维持宽限或到期 fence），不会重新绑定为
running。所以表里 `ORPHANED: frozenset()` 与流程一致，**不需要补边**。

## 4. 若要加固（decision-ready，未实施）

与 B1 同配方即可，风险低（所有真实迁移已实测合法）：

1. 先在 `set_attempt_state` / `mark_attempt_orphaned` 内读当前 state，非法即抛
   （写库之前，字段不动），RPC 边界映射为 `STATE_FORBIDDEN`；
2. **必须同时放行自反写入**（§3.1），否则 `_reattach` 断；
3. 回归测试用 9×9 全枚举乘积断言「守卫判定 == 表 + 自反规则」，并做反向验证
   （摘掉接线必须变红）；
4. `ORPHANED` 保持无出边（§3.2），不要为了让某个路径通过而补边。

不实施的理由（当前的判断）：没有观测到任何非法迁移，而 `set_attempt_state` 的
「不校验」是**文档化的意图**（本模块只做忠实落库，判定在调用方）。在没有真实错误
证据的前提下改变它，收益是"未来调用方写错会被拦"，代价是给恢复路径增加一个新的
失败模式（§3.1 已经说明这个失败模式不是假想的）。是否实施属于产品决策，等明确授权。
