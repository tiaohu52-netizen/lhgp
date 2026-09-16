# ZCode 夜间模式 / ZCode Night Mode（LHGP 样板）

把 **ZCode 桌面端自带的官方无头 runtime** 接入 LHGP，并用 LHGP 的**派工时间窗**
（`execution.dispatch_window`）让任务只在指定时段干活——默认 **23:00–08:00**，
适合夜间闲时批量跑活、避开白天额度/计费高峰。样板**不含任何个人机器路径**：
runtime / node / lhgp / 数据目录全部自动探测，可复制给任意用户。

## 前置条件

| 项 | 要求 |
|---|---|
| ZCode 桌面端 | 已安装（官方版），并在其中登录 **BigModel / Z.AI Coding Plan** |
| Node.js | ≥ 22.19（官方 runtime 的运行要求） |
| LHGP | 支持 `execution.dispatch_window` 的版本（本样板同批引入）；daemon 已 `lhgp start`（建议配看门狗，见下） |
| 独立验收执行器 | LHGP 要求 verifier 的 executor id 与执行者不同——若只有 ZCode 一个执行器，可用同一命令注册两个 id（执行器 `zcode-desktop` + 验证器条目），或使用你已有的其它执行器 |

## 快速开始（4 步）

```bash
# 0. 前置：把 lhgp 加入 PATH，或设置 LHGP_BIN
# 1. 体检（只读，不动任何文件）
node setup.cjs

# 2. 接线（写前自动备份）：
#    --fix-bom         修掉 ZCode 配置可能存在的 UTF-8 BOM
#                      （BOM 会让官方 runtime 误报 "Model config is missing"）
#    --wire-mcp        把 lhgp MCP 服务器写进 ~/.zcode/cli/config.json
#                      （执行器借此用 lhgp_write_back 回写终态/模型/用量）
#    --write-registry  把 zcode-desktop 执行器写进 <数据根>/registry.json
node setup.cjs --fix-bom --wire-mcp --write-registry

# 3. 起草夜战合同：编辑 contract.example.json（workspace / 验证执行器 id / 任务），然后
node create-contract.cjs contract.example.json --approve

# 4. 观察：白天不派工（dispatch/deferred 落账）；23:00 自动开工
lhgp watch --contract <你的合同 id>
```

夜间产物与审计：

- 产物：合同 `workspace_root` 内（按你的任务描述）；
- wrapper 审计：`<数据根>/contracts/<合同 id>/zcode-runs.jsonl`（`event:"start"/"exit"` 配对记录：rc / 用时 / 会话 id / model_id / token 用量 / 配额命中。wrapper 被 attempt 时限硬杀时 finish 不会执行、只有 start 记录——按「有 start 无 exit」即可发现被杀运行，2026-09-15 起生效；旧行无 event 字段视作 exit）；
- 会话续跑：`<数据根>/contracts/<合同 id>/zcode-session.json`（删除即回到全新会话）；
- 验收：按合同 `acceptance`（机器闸 + 你本人签名 `lhgp contract user-confirm <id>`）。

版本兼容注记：`soft_guidance.zcode.max_turns` 暂被 wrapper 忽略——ZCode 0.16.5
的 `--help` 广告了 `--max-turns` 选项，但参数解析器拒绝它（2026-09-16 两种传参
顺序实测均 "Unknown option" rc=1；2026-09-15 夜间派工三次快速失败同因），
runtime 文档与实现不一致。wrapper 一律不传该选项，回合上限依赖模型自然结束；
待 runtime 真正支持后再接线。

## 组件

| 文件 | 职责 |
|---|---|
| `lhgp-zcode.cjs` | 桥接执行器：快照解析 ids → 取租约代次 → `--attach` 快照 + `--prompt` 任务 → 会话续跑 → 解析 `--json` → 自动 `attempt/write-back`（终态+真实 model_id+用量）→ 配额/错误识别；含窗口防御与 `LHGP_ZCODE_DRYRUN=1` 离线校验 |
| `_discovery.cjs` | 位置探测（配置 → 环境变量 → 自动探测）：ZCode runtime / node / lhgp / lhgp-mcp / 数据根 |
| `setup.cjs` | 一键体检与接线（只读默认；`--fix-bom/--wire-mcp/--write-registry` 才写盘，写前备份） |
| `create-contract.cjs` | 从 JSON 样板起草合同（`contract/prepare`，`--approve` 追加批准） |
| `contract.example.json` | 夜战合同样板（`execution.dispatch_window`、authority、验收、playbook） |
| `watchdog.ps1` | daemon 看门狗（探活 + 自动拉起 + 时间线日志；Windows 计划任务注册命令见文件头） |

## 覆盖点（环境变量 / 配置文件）

机器本地覆盖写 `lhgp-zcode.config.json`（与本目录同放，**已 gitignore**；或用
`LHGP_ZCODE_CONFIG` 指定其它路径），键：`zcode_runtime` / `node` / `lhgp_bin` /
`data_root` / `zcode_config`。环境变量同名覆盖：`ZCODE_RUNTIME` / `ZCODE_INSTALL_DIR` /
`ZCODE_NODE` / `LHGP_BIN` / `LHGP_DATA_DIR` / `ZCODE_CONFIG`。

## 夜间模式语义（值得知道的四件事）

1. **窗口外不派工**：LHGP 把 `next_decision_at` 钉到窗口起点，白天不烧 dispatch
   预算；同窗口只落一条 `dispatch/deferred`（审计可查）。wrapper 侧还有一层防御
   （窗口外拒跑并写回 failed，退出码 75）。
2. **跨午夜窗口**：`start > end`（如 23:00–08:00）表示「当晚开、次日关」，
   本地墙钟判断；`start == end` 视为未配置。
3. **写回与用量**：执行器完成时通过 MCP `lhgp_write_back` 回写；wrapper 兜底自动
   回写（含从 ZCode 配置读取的真实 `model_id` 与 token 用量）——记录即事实。
4. **额度节奏**：BigModel Coding Plan 有 5 小时窗口额度；额度耗尽时 ZCode 报
   `PROVIDER_BUSINESS_ERROR`，wrapper 会写进 progress_note 与运行日志。
   夜间开工一般落在新窗口，长任务请给足 `max_dispatches`。

## 安全与隐私

- 不读取、不打印任何 API Key：`setup.cjs` 只报告「apiKey 是否存在」；
- 所有写操作先备份原文件（`*.bak-<时间戳>`）；
- 探测仅在本机进行（进程路径 / 卸载注册表 / 常见目录），不联网。

## 已知边界

- 无头会话与桌面端**共享** `~/.zcode/cli/config.json` 的模型访问配置（桌面端加密
  凭据不可导出，需该配置里有可用的 provider/key）；
- 验证器独立性：同机只注册一个 ZCode 执行器时，需另配一个 verifier 执行器 id
  （可用同一命令注册第二条目；独立性弱于异构模型，按需权衡）；
- 本样板以 Windows 为完整验证路径；macOS / Linux 的 ZCode 安装目录自动探测为
  常见路径 + 环境变量覆盖（如发现新布局，欢迎补 `_discovery.cjs` 的候选表）。
