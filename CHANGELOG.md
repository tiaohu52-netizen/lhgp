# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); version numbers
follow [SemVer](https://semver.org/spec/v2.0.0.html); dates in ISO 8601.

## [0.1.0a8] - 2026-09-07

memory-and-wiki Phase 1：协议内 wiki 合并发布版。

### Added

- **协议内 wiki**（`docs/wiki/`）—— git-tracked 纯 markdown vault,Obsidian 守则
  （wikilinks + 反链 + 层级 tags + frontmatter schema + `^blockid` 锚点）。
  **不是数据库**:wiki 是源码,可被 git diff / IDE 搜索 / 静态站生成器读。
- **种子页 11 个**:`glossary.md`(协议术语表)+ `index.md`(主入口 MOC)+
  `playbook/index.md`(playbook MOC)+ 8 个 playbook
  (add-event-type / schema-migration / sql-binding / add-mcp-tool /
  add-cli-subcommand / write-test-first / quality-gate / daemon-tick-hook),
  从 P6 28 个 commit 提炼。
- **索引生成器**（`scripts/build_wiki_index.py`）—— 扫 frontmatter + 双向 wikilink,
  产 `docs/wiki/.index.json` (schema_version 1)。`outgoing_raw` 兜底未解析引用
  (MOC 引用未来页) 与 `outgoing`(已解析) 分两路 —— AI 拿到的信号"未解析 =
  待写"是真实的。
- **CLI**:`lhgp wiki list / read / search / show-graph`,纯 read-only,
  不启 daemon 也能用;通过 `.index.json` 工作。
- **双命名空间 facade**:`src/lhgp/wiki.py`(canonical)+ `src/longtask/wiki.py`(< 5 行 re-export)。
- **Phase 2: protocol memory 子系统**（`lhgp/memory/`）—— SQLite `memories`
  表(schema v3→v4, 3 索引: scope+kind+created_at、expires_at 偏、
  source_contract_id 偏);`Memory`/`MemoryKind`/`MemoryScope` + `MemoryIndex`
  (global / domain / project 三层, capacity 合同, 单条 truncate fallback);
  `lhgp memory add / list / search / show / expire` CLI;`compile_context_snapshot`
  在 deadline 快照之后 / handover 之前接入 1/10 `max_bytes` budget;
  `record_evaluation` auto-mine hook:rating ≥ 4 + 有 comments → PATTERN
  (score 0.6/0.7),REJECT + 有 comments → GOTCHA (score 0.7, expires 365d),
  失败 logging.warning 不污染 evaluation 写入。
- **Phase 3: flowgen 代码/手写流程图**（`lhgp/flow/`）—— `walk_source` 抽
  Python 源(def/class/method + self.x 与 Class() 本地解析 + 动态调用落 external,
  自循环 / 重复边丢弃);`render_mermaid` 出 `flowchart TD` 块(Mermaid 词法
  安全的 id 冲突解决);`render_excalidraw` 出可粘贴的 JSON 场景
  (确定性网格布局);`wiki_parser` 抽 `## flow` mermaid 段 + `list_flow_pages`
  扫目录(隐藏目录 / 编码错误跳过);`lhgp flow ast / wiki / list-flow-pages` CLI。
- **新增 wiki 页**:`playbook/flowgen.md` —— 三个用法的取舍 + CLI 速查 +
  wiki `## flow` 约定 + 含一段手写流程图。
- **独立 verifier 子代理复核后的 P0 ship-block 修复**(commit `ccbc781`):
  - `expire_due` 实际无调度,过期记忆永远累积。`daemon_loop._expire_due_memories()`
    每 tick 跑在 `_enforce_deadlines` 之后,best-effort,n>0 时
    `append_event(MEMORY_EXPIRED)` 落审计账本。`EventType.MEMORY_EXPIRED`
    加进枚举。
  - auto-mine 启发式从 `"topic:" in comments` 翻 scope 但没加
    `topic/<domain>` 标签,`MemoryIndex.retrieve` 永远找不到,DOMAIN
    记忆是死信。改 anchored 正则 `r"^\s*topic:\s*([a-zA-Z][\w\-]+)"`,
    翻转 scope 同时把 `topic/<domain>` 写进 tags。
- **verifier 5 条 non-blocking 改进**(commit `29efbb2`):
  - `walk_source` 现在处理装饰器(`FunctionDef`/`AsyncFunctionDef`/
    `ClassDef` 的 `decorator_list` 都进 call visit)与嵌套类
    (`ClassDef` 递归,qualified name 索引,`self.Outer` 解析到嵌套类)。
  - Mermaid `_safe_id` 强制 ASCII(Mermaid 词法 `[A-Za-z_]`,Python
    `str.isalnum()` 默认接受非 ASCII Unicode,会卡老版渲染器)。
  - Excalidraw 箭头 `width`/`height` 改用 bounding box(`abs(delta)`),
    `points` 数组带符号,back-edge 不再产出 `width=0 / height<0`。
  - `skills/flowgen/SKILL.md` 加 FAQ 行:第二次写 `## flow` 没生效
    是因为 parser 只取第一段,删旧的再写。
  - `tests/unit/test_lhgp_namespace.py` 补 `lhgp.memory` / `lhgp.flow`
    ↔ `longtask.memory` / `longtask.flow` 的 facade identity 测试
    (P6 模式对称,任何 facade 改名都抓得到)。
- **新增 skill**:`skills/flowgen/`(SKILL.md + MANIFEST.json 0.1.0a1)——
  教模型用 `lhgp flow` 三种入口。

### Quality

- 7/7 quality gate 全过(ruff format / lint / arch / deps / claims / mypy / pytest+coverage)。
- 测试 +77 (P1 110 + P2 44 + P3 33 = 187 / 890 → 967)。
- 覆盖率 75.35% (target ≥ 70%)。
- Phase 1 验证报告(子代理)发现 3 个 ship-block 项,已在 0.1.0a8 修复:
  1. CHANGELOG 入口(本条)
  2. ARCHITECTURE.md 真身位置地图 wiki 行(见下)
  3. 索引 `_resolve_link` 加 basename fallback + frontmatter `related` 进
     outgoing + 隐藏目录排除
- P2 修 5 个 production bug:from_db_row plain-tuple column index (-1 → 5)
  静默把 `tags_json="4"` 解析成 4、`_opt_dt` naive 视为 UTC、index domain
  过滤 `topic/<domain>` 标签(top_n 之前的下标剥离)、
  `make_pattern_memory(kind=, scope=)` 让 auto-mine 不再硬编码 PATTERN。

## [0.1.0a7] - 2026-09-07

反馈回路 + 多合同管理 + deadline 强制（P6）合并发布版。

### Added

- **反馈回路**（`lhgp/feedback`）：`user_evaluations` + `acceptance_diffs`
  两张表。1-5 评分、verdict、comments；diff 走 workspace walk 算
  before/after 文件指纹。6 个新事件类型：`USER_EVALUATION_SUBMITTED` /
  `ACCEPTANCE_DIFF_COMPUTED`。
- **多合同管理**（`lhgp/portfolio`）：`portfolio_summary`（全量合同 +
  by_state / by_deadline / by_acceptance 计数 + 最新 evaluation join）、
  `trace_contract`（单合同事件流 + 最新 eval / diff）。事件类型
  `PORTFOLIO_SUMMARY_VIEWED`。
- **Deadline 强制**（`lhgp/enforcement`）：NORMAL / WARNING(30%) /
  URGENT(5%) / BREACHED 四级，`DeadlineEnforcer` 返
  `EnforcementAction`（republish / notify_user / request_verification /
  lock_new_attempts）。daemon_loop 每 tick 顶部调
  `_enforce_deadlines`，breached 落 `DEADLINE_BREACH_LOCKED` + rebuild
  projection；相同 level 重扫不重复落事件（幂等）。`MIN_ESCALATION_WINDOW`
  60s 守门。事件类型 `DEADLINE_LEVEL_ESCALATED` /
  `DEADLINE_BREACH_LOCKED`。
- **自动模板演化**（`lhgp/learning`）：`extract_template_signals` 从
  evaluation + diff 矿信号，`TemplateEvolver` 把高质量合同
  （`overall ≥ 0.7`）自动写 `templates/auto-<cid>-r<rev>.json`，同 stem
  碰撞自动加 `-n2` / `-n3` 后缀（永不覆盖）。事件类型
  `TEMPLATE_EVOLVED`。
- **建议通道**：`suggest_draft_improvements` 在 prepare 阶段读历史
  signals 返非破坏性 advisory。
- **Schema v3**：`_migrate_v2_to_v3` 幂等加 2 张新表 + 9 索引；不回填
  历史事件（保 revision CAS 干净）。`STORE_SCHEMA_VERSION = 3`，
  `StoreConfig.schema_version` 默认 3。`_check_schema_version` 高于本
  版本只读拒写（fail-closed）。
- **新工具**（6 个，`lhgp_*`）：`submit_evaluation` / `compute_diff` /
  `evolve_templates` / `portfolio` / `trace` / `deadline_report`（总数
  41 → 47）。
- **新 CLI 命令**：`feedback submit|diff`、`evolve-templates`、
  `portfolio`、`trace`、`deadline-report`。
- **双命名空间 facade**：`longtask/feedback.py` / `learning.py` /
  `portfolio.py` / `enforcement.py` 各自 re-export 自 `lhgp/`，身份
  钉住（`is` 测试）。

### Fixed

- `_enforce_deadlines` 在 `total_seconds ≤ 0` 时 `continue` 跳过 breached
  合同 → 改用 `None` 透传让 `compute_deadline_level` 看 `remaining ≤ 0`
  必返 BREACHED。
- `compute_acceptance_diff` summary 字段 `len(changes) - len(created)`
  把 deleted 算成 modified → 独立维护 `modified` 列表，summary 改用三类
  分别 count。
- `_acceptance_pass_rate` 在 1 列 cursor（返回 plain tuple）上
  `r["event_type"]` TypeError 被 `except Exception: continue` 静默吞
  → 改成 Row/tuple 双兼容；正信号只认 synthetic "passed"/"satisfied"
  是死代码（真实事件名里无），加 `EventType.CONTRACT_SATISFIED` 作为
  真实正信号。
- `extract_template_signals` vocabulary 循环同样 `r["payload_json"]` 失
  败被静默吞，导致 `vocab` 永远空、auto-evolve 模板
  `acceptance.checks` 退化成默认 `structure-valid` → 同样改双兼容。

### Quality

- 7/7 quality gate 绿（format / lint / arch / deps / claims / mypy /
  pytest+coverage）。测试 775 → 885（+110，5 个新 P6 单测 + 1 个集成
  + 4 个 namespace identity）。
- coverage ≥ 73% 维持。

## [0.1.0a6] - 2026-09-06

审计全部清零 + E1-E4 全部增强（PR #15-#19），合并发布版。

### Added

- **E1 Deadline 校准**：按合同内主导执行器的成功样本（≥2）校准
  forecast p50/p90；低样本降级链保留。
- **E2 多合同公平调度**：紧迫档位降序派工 + 饥饿检测（连续 5 tick
  未派工自动提前）+ per-tick 容量记账（可选上限）。
- **E3 提案审批闭环**：`lhgp proposal-apply` 命令 + 提案结构校验
  （stages/id/contract_id/≤20）+ 冻结区强制（executor/model/budget/
  authority 字段一律拒绝——计划提案不得走私执行权限）。
- **E4 模板库**：code-change / research / release-check 三内置模板
  随 wheel 分发。
- **新工具**（39 个）：brief（接手包）、board（风险看板）、stats
  （成本台账）、propose_plan（提案通道）；diff / prune-events /
  template / proposals / proposal-apply CLI 命令。
- **macOS 完全支持**：进程身份模型（ps etime/state），CI 升阻断。

### Fixed

- 12 项 Required（幂等归属校验 / Goal 身份保护 / 决策事务化 /
  doctor quick_check / 迁移 WAL checkpoint / 并发限额生效 / verifier
  作用域统一 / reconcile 补派验收 / stale 释租约 / fence 终止 /
  PID 身份 / 宽限配置接线）。
- 6 项深化（投影原子写 / 快照体积上限 / token 0600 / quiet hours
  验证 / patch 错误语义 / 假 request_id 声明）。
- pytest 8.4.2 → 9.0.3（PYSEC-2026-1845）。

## [0.1.0a5] - 2026-09-06

**macOS 全面支持**：darwin 进程身份模型落地，macOS CI 升级为阻断平台。

### Added

- macOS 进程探测：libproc `proc_pidinfo(PROC_PIDTBSDINFO)` 替代方案改为
  `/bin/ps`（etimes/state 机器可读字段）——跨版本无偏移依赖，
  僵尸判活、身份比对、优雅终止在 macOS 与 Linux/Windows 行为一致。
- 接手包 `lhgp brief`、风险看板 `lhgp board`、成本台账 `lhgp stats`、
  修订差异 `lhgp diff`、事件清理 `lhgp prune-events`、内置模板库
  `lhgp template`（39 个 MCP 工具，+4：brief/board/stats/propose_plan）。
- Goal 计划提案通道：`lhgp_propose_plan` 只落 goal/proposed 事件，
  模型可提议、不越权直写（ADR-004 规则 6 配套）。

### Changed

- macOS 从「实验性/非阻断」升级为完全支持；CI 三平台全部阻断绿。

## [0.1.0a4] - 2026-09-05

合并发布版：包含 a1–a3 全部内容 + MCP 工具面收口（PR #12、#13）。
此前三个增量 Release 已并入本版本，Releases 页只保留当前完整版；
细粒度历史见本文件各版本段。

### Added

- MCP 工具面收口（35 个工具）：dispatch 层强制 inputSchema
  （required/类型/未知键三查，修复「参数静默丢弃」根因）；
  `list_contracts` 暴露 cursor 翻页；新增 `goal/prepare` 工具
  （返回 admission offer、支持 goal_id/stage_id——激活
  advance_goal/goal_contract_draft）；schema↔handler 一致性回归
  （AST 全量断言，approve CAS 类 bug 进不了主干）。

### Changed

- 16 个工具描述重写为 if-then 场景式（何时用/传什么/返回什么/错误含义）；
  SKILL 新增「场景 → 工具决策表」。

### Fixed

- `attempt_status` 对不存在的 attempt 返回 UNKNOWN_ATTEMPT（原假 ok）。
- `contract_id` 非字符串直接 VALIDATION_FAILED（原静默 str 化）。

## [0.1.0a3] - 2026-09-05

多 Agent 协作开发扩展 + MCP 工具面加固（PR #11，34 工具全量审计后修复，
三平台 CI 实测通过）。

### Added

- **多 Agent 协作开发协议**（docs/AGENT-COLLAB.md）：基于本仓库被多个
  agent 反复改动的真实失败史，把「无验收标准/谎报完成/环境盲区/无证据链/
  破坏半径失控」五类失败映射到 LHGP 机制，附命令级操作手册。
- **改动合同模板**（templates/code-change-contract.json）：可直接
  `lhgp prepare --file` 的代码改动合同，验收命令为 pytest + quality_gate，
  由独立 verifier 重跑判定。

### Fixed

- MCP `prepare_contract`：字符串 acceptance_checks 在边界 fail-closed
  （此前 "file-exists" 会被逐字符拆成 11 个垃圾检查冻进合同）。
- `goal/update` 收归 Principal（ADR-004 规则 6：模型只能提议、不得直写
  权威计划）；工具描述如实标注。
- `attach_to_executor` 无活租约时进度显式报 `progress_error`，不再静默
  丢弃让模型误以为已存。
- 审计归属修正：verification/interrupt 事件用真实派生 actor，模型动作
  不再记成用户动作。
- MCP `approve` 工具转发 `expected_revision`（CAS 键名与 handler 一致）。
- SKILL 文档：修正跑不通的 arbitrate/patch 命令示例，补 approve/
  update_goal 的 user-only 指引。

## [0.1.0a2] - 2026-09-05

Deadline 可靠性与合同防篡改（PR #10，分支 hardening/deadline-contract-guards，
GitHub CI 三平台实测通过）。

### Security

- `contract/patch` 现在要求 Principal（user）——模型客户端修订合同一律
  AUTH_FAILED（RPC-C1 审查的遗漏面）。
- `budget` 加入 FROZEN_FIELDS：patch 试图改预算从静默忽略升级为显式拒绝。

### Fixed

- 过期的 `next_decision_at` 原样返回（调用方钳 0 立即唤醒）：一个 blocked
  合同的过期决策点不再让 daemon 回退整周期休眠、吞掉 active 合同的未来
  唤醒点——deadline 唤醒稳定性。
- 档 3（steer）无活租约时立即重派（DESIGN §6.2）：deadline 剩余窗口不再
  浪费在无会话可干预的空提醒上——deadline 灵活性；预算不足如实交用户。
- 验证预算耗尽 → 合同转 `blocked(need-user)`：daemon 不再把不可验收的
  合同一轮轮重派烧光执行预算（防破坏性跟进）。

## [0.1.0a1] - 2026-09-05

安全加固补丁：发布后四域深度审查（进程/租约、持久化、RPC/MCP 边界、调度
预算）发现的 9 项 Critical 修复，全部附回归测试。详见
[security-hardening-2026-09-05](docs/evidence/security-hardening-2026-09-05.md)。

### Security

- `contract/approve|pause|resume|cancel|arbitrate` 现在要求 Principal
  （actor=user）——模型客户端自批准合同返回 AUTH_FAILED 并指引 CLI 通道
  （SPEC §4.2）；此前模型可起草+自批准+指定验收命令形成任意命令执行链。
- `goal/prepare` 与所有入口的 contract_id 统一为安全 slug 校验（拒绝路径
  分隔符/`..`），堵住投影写到数据根之外的路径穿越。
- 执行者 `attempt/write-back` 不得把合同直推完成态（STATE_FORBIDDEN）；
  完成结论只能由 verifier 裁决路径写入。

### Fixed

- `ensure_schema` 的一次性历史改写（complete→satisfied 等）加版本门控，
  不再每次启动改写 daemon 刚写入的合法终态；tick 终态跳过集合改用
  `TERMINAL_STATES`，已完成合同不再被重新仲裁成 EXPIRED。
- 真实 v1 结构库的平滑升级（迁移列索引移到补列之后 + events 缺列兜底）。
- `write_back(events=())` 形态补 request_id 簿记事件，幂等重放不再二次
  执行状态迁移。
- verifier 派发不再抢夺在跑 executor 的活租约（dispatch/deferred 推迟），
  且纳入 kill switch 管辖。
- workspace 排他归一化升级为 realpath + casefold，大小写/符号链接别名
  不再绕过。
- `poll_attempts` 对 fenced 租约/缺失 generation 防护，单个 attempt 的
  租约异常不再击穿 daemon 进程。

## [0.1.0a0] - 2026-09-05

首次公开发布（Developer Preview）。内部开发者预览切割于 2026-09-01；
2026-09-05 完成产品定位统一、发布阻断项修复（B1/B2）与候选制品验证后发布。

### Changed

- Product name set to **限期合同中枢 / Deadline Contract Hub** (ADR-004 revision);
  "multi-agent task contract runtime" is retained as the category description only.
- Position LHGP as a multi-agent task contract runtime with an LLM-independent
  scheduling core; retain protocol names, package identity and compatibility.
- Align English/Chinese README and specification framing; distinguish multiple
  contracts and submitted-plan progression from optional future semantic planning.
- Replace the incomplete automatic-execution quickstart with an isolated,
  non-LLM contract lifecycle smoke. Add ADR-004, an executable release-first plan
  and an audit recording known blockers. This is documentation work, not a
  runtime fix or a new release.

### Added

- MCP tool safety annotations (`readOnlyHint`, `destructiveHint`,
  `openWorldHint`) and strict JSON argument validation.
- Read-only `notifications` CLI audit command with status/limit filters and
  goal filtering and opt-in payload output; the MCP notification tool remains
  payload-redacted by default.
- LHGP dual-track entrypoint polish (`lhgp --version`, `lhgp-mcp`) and updated
  plugin/roadmap documentation.
- Release artifacts now validate embedded plugin metadata (strict SemVer,
  `lhgp` identity, and canonical `lhgp-mcp` command) during CI.
- Canonical `lhgp` Python namespace facades now cover the runtime layers,
  while legacy `longtask` imports remain compatible during migration.
- Persistence schema helpers and notification outbox exports are now explicit
  under `lhgp.persistence`, with lazy loading preserved for legacy import order.
- Canonical RPC and persistence package APIs now expose stable protocol,
  schema, event, notification, and executor-side entry points with lazy
  loading where legacy import order requires it.
- `contract/get` now exposes contract-scoped `decision_history` and
  `attempt_history`, with bounded `decision_limit` / `attempt_limit` controls
  across RPC, CLI, MCP, and model skills.

### Fixed

- POSIX liveness probes now recognize zombie processes via `/proc` state and
  reap daemon children with `waitpid(WNOHANG)` before escalating a stop to
  SIGTERM, so daemon shutdown is graceful again on Linux instead of always
  being reported as forced.
- Linux process start times now come from `/proc/<pid>/stat` field 22 (stable
  after exit) instead of `st_ctime` (which changes when a process exits), and
  `reattach` binds provably-terminated runs whose pid has been fully reaped,
  per the documented branch-2 settlement semantics.
- CI runs the gate on macOS as a known-gap, non-blocking platform for this
  preview (the identity model has no `/proc` equivalent there yet).
- CLI and MCP `--help` no longer crash with `UnicodeEncodeError` on non-UTF-8
  consoles (e.g. cp1252); unencodable help characters are replaced.
- The `executor/health` integration test no longer depends on a
  machine-installed `codex` CLI (hermetic fixture uses the interpreter).
- mypy analysis is pinned to the Windows platform (the authoritative
  typecheck platform) so the three-OS CI matrix typechecks identically, and an
  unused Rust cache step was removed from CI.
- Timeout cancellation failures now orphan the attempt and retain its lease for
  reconcile grace/fencing instead of releasing execution control before the
  external process is known to have stopped.
- Verifier `attempt/started` events no longer consume the executor
  `max_dispatches` budget; execution and verification budgets remain separate.
- Detached daemon and subprocess test lifecycle cleanup, including parent
  pipe closure after child exit.
- MCP canonical aliases now retain their safety annotations and report the
  correct server identity when launched through `lhgp-mcp`.
- Schema startup reconciliation now repairs partially upgraded v2 databases,
  including missing external-handle capability columns, before daemon scans.
- Doctor output and README quickstart commands now use the canonical LHGP name.
- Claims evidence is re-anchored to the latest verified implementation commit;
  the full 7-gate suite remains green with 574 tests at 81.47% coverage.
- Unavailable L1 wakeup channels now emit one deduplicated `wakeup/degraded`
  event per daemon lifecycle instead of writing noise on every tick.
- Failed wakeup task disarms remain tracked and are retried on the next daemon
  tick, preventing stale OS scheduler entries after transient errors.
- dogfood v5 now preflights `request-verification` for blocked deliveries and
  falls back to a revision contract only after an explicit protocol refusal.
- Verification request consumption is now recorded by request event ID, so a
  terminal verifier cannot cause the same user request to be dispatched again.
- `contract/get` and MCP `get_contract` now expose bounded verification history,
  making request, consumption, and verifier-start state directly model-readable.
- `lhgp doctor` now checks launch executables for enabled registry entries and
  reports actionable missing-CLI diagnostics before dispatch.
- MCP now exposes read-only `lhgp_doctor` / `longtask_doctor` aliases so models
  can run the same preflight diagnostics before selecting an executor.
- L1 RTC wakeup targets now use the earliest available decision, wakeup, or
  deadline-safety point; a later safety margin can no longer postpone an
  earlier `next_decision_at`, and past decision points are clamped to an
  immediate wakeup.
- The Windows L1 adapter now arms one-shot Task Scheduler entries that call
  authenticated `daemon/wake`; fired signals are audited and consumed before
  the next decision tick, while non-Windows platforms remain explicit
  `wakeup/degraded` fallbacks. Its date serialization follows the actual
  Windows `schtasks.exe` `yyyy/mm/dd` parser, verified by a real create/query/
  delete smoke.
- L1 RTC wakeup targets now use the earliest available decision, wakeup, or
  deadline-safety point; a later safety margin can no longer postpone an
  earlier `next_decision_at`.

### Developer Preview release (2026-09-01 cut)

The reference implementation is now feature-complete for the scope declared
in [DESIGN.md v0.7](DESIGN.md). All 7 quality gates (`format / lint / arch /
deps / claims / typecheck / test+coverage`) pass on Windows; CI matrix
expands to ubuntu + macOS.

#### Protocol surface (DESIGN §5, §11.1)

- 24 JSON-RPC methods over the control plane, dispatched through
  `src/longtask/rpc/handlers.py` + `src/longtask/rpc/executor_api.py`.
- MCP server thin layer (`longtask-mcp` script entry, `src/longtask/mcp_server.py`)
  exposing core `longtask_*` tools plus LHGP aliases and audit/control
  extensions to any MCP-compatible agent harness.
- §17 `longtask-contract` skill (`skills/longtask-contract/SKILL.md`) teaching
  models how to draft contracts, write handovers, and avoid common pitfalls.

#### Daemon lifecycle (DESIGN §3.3, §15.2)

- `longtaskd` start / stop over a real detached subprocess (Windows:
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, POSIX: `start_new_session`).
- Graceful stop via `daemon.stop` flag, hard kill only on grace expiry.
- `longtask-mcp` (stdio JSON-RPC 2.0) ships as the model-facing companion.

#### Attempt lifecycle (DESIGN §3.4, §5.1, §5.2, §7, §10)

- `AttemptRunner` (`src/longtask/cli/runner.py`) drives `start_attempt` +
  `poll_attempts`: prepare re-validation, spawn, heartbeat lease renewal,
  terminal collect, and stale marking.
- Dead-lease reclaim path on redispatch (§7): `reclaim_lease` not bare
  `acquire_lease`.
- §5.2 cross-check verifier: `_dispatch_verifier` auto-derives a verifier
  attempt (`role: VERIFIER`) with a candidate ≠ executor; the
  `_judge_verifier_outcomes` tick-end hook then promotes
  `contract.completed` on verifier `attempt/succeeded` or sends the
  contract back to `ACTIVE` on `attempt/failed`.

#### Ephemeral context (DESIGN §4.1)

- `compile_context_snapshot` materializes `context/attempts/<id>/active.md`
  and `scratch.md` per attempt; the `ContextPolicy` parses
  `contract.context.{required,max_bytes,expires_after_minutes}`.
- Capacity contract is fail-closed: overflow raises `CapacityRefusedError`
  and persists `context/capacity-refused` before refusing attempt start.
- Handover digest is auto-merged into `task_prompt` so re-dispatched
  attempts inherit verification-failure context (closes the
  v1 real-run "second attempt wasted" gap found in internal runs).

#### Persistence, budget, contracts

- Budget hardness (§6.3) via `attempt/started` event count, not a static
  draft field. `max_dispatches` consumed on every successful dispatch;
  exhausted → tier 5 `blocked(need-user)`.
- Frozen-zone immutability enforced on `patch`; only `soft_guidance` /
  `acceptance` / `workload_estimate` are mutable.
- `request_id` idempotency on every state-mutating RPC (§11.3).

#### Executor-side RPC (DESIGN §11.2)

- `attempt/status` returns the attempt's event history + lease posture.
- `lease/renew` renews with fencing (lease gen / holder match); mismatches
  return `LEASE_FENCED`.
- `attempt/write-back` writes progress + attempt state with the same
  fencing; `request_id` is honored for retry semantics.

#### Layered wakeup (DESIGN §6.4, ADR-0002)

- L0 power guard via `SetThreadExecutionState` on Windows, port-injectable.
- L1 plan-task registration with `max(next_wakeup, deadline - margin)`.
- New event types: `wakeup/sleep-guard`, `wakeup/rtc-armed`,
  `wakeup/rtc-fired`, `wakeup/degraded`.
- L2 / L3 require external infrastructure and remain `accepted_debt` (see
  `quality/claims.json#strict-deadline-wakeup-design`).

### Known limitations (accepted debt, not blockers for v0.1.0a0)

- L2 / L3 wakeup not implemented (cloud + relay, external infra).
- Headless harness subprocess lifetime vs. `Popen` handle alignment
  (observed in internal real runs).
- `control/spawn` is vocabulary-only; external RPC to `AttemptRunner`
  not yet exposed.
- Agent harnesses (Claude Desktop etc.) each need their own `lhgp-mcp`
  registration in their MCP client config — out of protocol scope.

### Documentation & examples

- Internal real-execution archives (raw event chains, cross-check
  verifier catching a wrong unit-test assertion, and an 8-step MCP
  lifecycle trace) were retained privately during the preview period.

### Internal (commit-level highlights)

- `911862d` — §4.1 ephemeral context
- `2b34348` — executor-side RPC
- `84f0b46` — §5.2 verifier auto-dispatch
- `7405173` — §17 skill
- `79c0f78` — MCP server thin layer
- `5 prior commits` (daemon lifecycle, layered wakeup, end-to-end runs)
