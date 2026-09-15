# LHGP 终端演示视频（promo/）

产物：**`promo/lhgp-demo.mp4`** — 1200x700 @ 30fps，约 61 秒，全离线录制，
无任何 API key、模型名、工具栈名或个人路径。

## 重新生成（两条命令）

```bash
# 方式 A：Windows 原生，无需 VHS（推荐，本视频即由此产出）
PYTHONPATH=~/vhs-tools/pylibs PYTHONUTF8=1 \
  .venv/Scripts/python.exe promo/render_demo.py

# 方式 B：VHS（需已安装 vhs + ttyd + ffmpeg；Windows 原生见下方坑，建议在 WSL 里跑）
vhs promo/demo.tape
```

`render_demo.py` 每次运行都会：清空 `promo/demo-data` → 实跑一遍真实 CLI
命令采集真实输出 → 逐帧合成终端画面 → ffmpeg 编码为 `promo/lhgp-demo.mp4`。
视频里的每一个字节都是真实命令输出（打字动画与停顿节奏是唯一合成的部分）。

### 一次性环境准备

```bash
# 1) 渲染依赖（装到仓库外的目录，不污染 .venv / uv.lock）
curl -sL -o ~/vhs-tools/pip.pyz https://bootstrap.pypa.io/pip/pip.pyz
.venv/Scripts/python.exe ~/vhs-tools/pip.pyz install --target ~/vhs-tools/pylibs pyte pillow

# 2) 中性数据目录（junction）：doctor 会打印解析后的绝对路径，
#    用 junction 指到 C:\lhgp-demo-data，视频里就不会出现本机仓库路径
mkdir -p C:/lhgp-demo-data
cmd //c mklink //J "promo\demo-data" "C:\lhgp-demo-data"
```

删除演示数据：清空 `C:\lhgp-demo-data` 内容即可（或直接删掉 junction 和该目录）。

## tape 叙事说明（`promo/demo.tape` / `render_demo.py` 共用同一份脚本）

| # | 命令 | 叙事 |
|---|------|------|
| 1 | `lhgp --data-dir promo/demo-data doctor` | 预检自检：解释器/存储/数据库/注册表/熔断全绿，`ALL SYSTEMS GO` |
| 2 | `prepare --deadline "2026-10-05T18:00:00"` | **故意**漏掉时区 → `VALIDATION_FAILED: deadline_at must carry an explicit timezone`。卖点：协议在守护你，含糊的合同活不过起草 |
| 3 | `prepare ... --deadline "...+08:00" --workload-hours 12` | 合同起草成功（drafted），冻结区目标、截止时间、工时预算入库 |
| 4 | `get lt-20260905-demo --decision-limit 1 --attempt-limit 1` | 查看合同全貌：state / deadline / 硬约束 / continuity 参数 |
| 5 | `approve lt-20260905-demo` | 人工批准，drafted → active（revision 升到 2） |
| 6 | `request-verification ... --reason "Deliverables ready for acceptance review"` | 验收请求被如实受理并写入审计（`verification_requested: true`） |
| 7 | `python -m longtask.cli.watch --data-dir promo/demo-data --contract lt-20260905-demo --follow --for 6` | **核心卖点**：合同生命周期事件流逐行滚动（prepared → approved → verification/requested）+ 心跳，全程可审计 |
| 8 | 清屏 + 口号 | "Contracts that outlive the session. Deadlines that enforce themselves." |

> 已知坑：顶层 CLI 派发 `watch` 子命令时重建 argv 丢掉了 `--data-dir`，
> `lhgp watch` 会去读默认数据目录。演示里用
> `python -m longtask.cli.watch --data-dir ...` 直连模块入口绕过；
> 修复该转发 bug 后可以把 tape 第 7 步换回 `lhgp watch ...`。

## Windows 原生 VHS 的坑

- `winget install charmbracelet.vhs` 装的是 vhs.exe + ffmpeg（Gyan.FFmpeg 作为依赖自动安装），
  **不含 ttyd**，需自行从 tsl0922/ttyd releases 下载 `ttyd.win32.exe` 放进 PATH。
- 即使齐了，`vhs` 在 Windows 原生环境渲染时会**无限卡住**（ttyd + gdigrab 抓屏在
  无头/服务会话下起不来）。实测 vhs 0.11.0 + ttyd 1.7.7 + ffmpeg 8.1.1 卡死。
- 因此推荐二选一：方式 A（本目录的 render_demo.py），或在 **WSL** 里：
  ```bash
  # Ubuntu WSL 内
  sudo apt-get update && sudo apt-get install -y ttyd ffmpeg
  curl -fsSL https://github.com/charmbracelet/vhs/releases -o /tmp/vhs.tar.gz  # 下载 linux 版并解压到 PATH
  vhs promo/demo.tape   # 在仓库根目录执行，产出 promo/lhgp-demo.mp4
  ```

## 文件清单

- `lhgp-demo.mp4` — 成品视频
- `demo.tape` — VHS 脚本（WSL/原生 VHS 渲染入口）
- `render_demo.py` — 无 VHS 依赖的等价渲染器（Windows 原生）
- `demo-data` — 演示数据 junction（指向 `C:\lhgp-demo-data`，临时、可删）
