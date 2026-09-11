# 安全审查与九项 Critical 修复（2026-09-05，总控）

## 背景

v0.1.0a0 发布后对全仓做四域并行深度审查（进程/租约生命周期、持久化数据
完整性、RPC/MCP 输入边界、调度与预算决策），发现 **11 个 Critical、20 个
Required**。本轮修复了其中危害最大的 9 项（全部经总控独立复现或静态坐实），
每项配回归测试。审查与修复均发生在 `5a0948d`（发布证据提交）之后。

## 修复清单

### 安全三连（RPC/MCP 边界）

| # | 缺陷 | 修复 | 回归测试 |
|---|---|---|---|
| S1 | `contract/approve` 无 actor 门禁，模型可自批准合同（SPEC §4.2 违反），链条可达任意命令执行 | 新增 `require_principal` 守卫（canonical+legacy 双侧 `_common`），接入 approve/pause/resume/cancel/arbitrate 五个 Principal 决定权方法；`goal/advance` 不设（有独立的验收绑定防线） | `test_principal_gate.py`（模型批准→AUTH_FAILED；CLI 用户路径不误伤） |
| S2 | `goal/prepare` 的 contract_id 裸 strip()，`lt-x/../../escaped` 可将投影写到数据根之外（DRAFTED 过期扫描自动触发=时间炸弹） | 安全 slug 校验下沉 `require_contract_id`（双树共用），拒绝路径分隔符/盘符/`..`；不强制 lt- 前缀（阶段合同沿用自定义 ID） | `test_contract_id_traversal.py`（7 种穿越形态全拒） |
| S3 | `attempt/write-back` 无调用方归属认证，模型可冒充执行者把合同直推 `complete`，绕过 verifier 与验收预算 | executor 角色 write-back 带完成态 contract_state → `STATE_FORBIDDEN` 拒接；verifier 裁决路径不受影响 | `test_write_back_completion_gate.py` |

### 数据完整性三连（持久化）

| # | 缺陷 | 修复 | 回归测试 |
|---|---|---|---|
| S4 | `ensure_schema` 每次启动无条件把 `complete` 改写成 `satisfied`（无事件无 CAS），且 tick 终态集合不含 `satisfied` → 已完成合同重启后可连锁成 `EXPIRED` | 一次性数据改写（complete→satisfied、on_track→not_due）加 `user_version<2` 门控；tick 两处跳过集合改用 `TERMINAL_STATES` | `test_schema_migration_regressions.py`（v2 complete 三次重开不变；v1 complete 一次性迁移） |
| S5 | 真实 v1 库 `ensure_schema` 直接 `OperationalError`（goal_id/request_id/role 等列的索引建在补列之前） | events(goal_id/request_id)、attempts(role/state) 索引移到 `_migrate_v1_to_v2` 之后；迁移补全 events 缺失列兜底 | 同上（真实 v1 结构库平滑升级） |
| S6 | `write_back(events=(), contract_state=...)` 的合法形态无 request_id 落点，幂等探测面为空，重放二次执行状态迁移 | 空 events 且带 request_id 时落 `attempt/write-back` 簿记事件（新事件类型） | `test_write_back_idempotency.py` |

### 调度与进程三连

| # | 缺陷 | 修复 | 回归测试 |
|---|---|---|---|
| S7 | verifier 派发直接 CAS 抢在跑 executor 的活租约（executor 被误判 stale、进程失管、同 workspace 双写）；kill switch 只拦 tick、verifier 旁路照常 spawn | `_dispatch_verifier` 检查活租约持有者非终态 → `dispatch/deferred` 推迟；入口统一 kill switch 检查（两条旁路都经过） | `test_verifier_dispatch_guard.py` |
| S8 | workspace 归一化只小写盘符，`D:/Data` vs `d:/data` 绕过排他；符号链接同目标绕过 | `os.path.realpath` + 全路径 `casefold()` | `test_contract_visibility.py` 新增 2 用例 |
| S9 | RUNNING attempt 租约中途消失 → `renew_lease` 抛 `LeaseFencedError` 击穿 poll 顶层杀死 daemon；`generation=None` → `int(None)` TypeError 同样击穿 | renew 包 try/except（fenced → 标 stale）；generation None 安全跳过续约 | `test_poll_lease_guard.py` |

## 测试面变化

- 新增回归测试 6 个文件、约 25 个用例；既有测试按修复后语义更新 3 处
  （`test_verifier.py` 活租约 fixture 改为收尾后路径、`test_rpc_lifecycle.py`
  非法 ID 用例改为真实穿越形态、`test_mcp_server.py` MCP 批准步骤改为
  AUTH_FAILED + 用户 CLI 批准）。
- 全量七道门：7/7（664 tests / 1 skipped）。
- 审查中同步发现并顺带记录（未修，见下）：双树 `_common.py` 的
  `RpcError` 类型在跨包异常链中会触发 `TypeError: super(type, obj)`
  （lhgp.rpc.errors vs longtask.rpc.errors 是两个类）——SPEC §19.3 第 6 步
  收敛双树时一并解决。

## 明确不在本轮（保持透明）

- 审查报告的 20 个 Required（request_id 幂等跨方法串扰、goal UPSERT 覆盖、
  events 无界增长、自适应休眠被过期决策点废掉、Windows L1 外的平台唤醒等）
  与全部 Optional/Nit 项：已在审查报告中留档，按 RELEASE-PLAN 后续轮次处理。
- macOS 身份模型（无 /proc 等价物）仍为已知缺口平台。

## 验证

- 本地七道门 7/7（664 tests，多次运行）。
- CI 三平台结果见提交后的 Actions 记录（Windows/Linux 阻断绿、macOS 非阻断）。

## 更正与后续状态（2026-09-11 审计追加）

历史记录保持原样不改写，此处追加实测更正与状态更新。

**更正 1（本文第 43-46 行的判断有误）**：原文称「双树 `_common.py` 的 `RpcError`
类型在跨包异常链中会触发 `TypeError: super(type, obj)`（lhgp.rpc.errors vs
longtask.rpc.errors 是两个类）」。实测**两者是同一个类对象**：

```text
lhgp.rpc.errors.RpcError is longtask.rpc.errors.RpcError  →  True
```

`src/longtask/rpc/errors.py` 是 3 行门面（`from lhgp.rpc.errors import *`），
不存在两个 `RpcError` 类，因此也不存在该跨包异常链问题。原文的风险描述不成立，
SPEC §19.3 收敛双树时**不需要**为此做额外处理。

**更正 2（第 50 行 deferred 清单中的一项已修）**：「request_id 幂等跨方法串扰」
已于 2026-09-11 修复：`_common.py` 增加跨方法 `request_id` 归属校验
（同一 `request_id` 被不同方法复用时拒绝，而非误判为幂等重放），两个命名空间的
实现与 `__all__` 同步，并由 `test_handler_common_parity.py` 按实现参数化钉住。

**更正 3（第 50 行其余项的状态）**：该清单其余条目（goal UPSERT 覆盖、events
无界增长、自适应休眠被过期决策点废掉、Windows L1 外的平台唤醒等）状态未变，
仍按 RELEASE-PLAN 后续轮次处理。

