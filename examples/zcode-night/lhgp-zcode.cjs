#!/usr/bin/env node
/**
 * lhgp-zcode.cjs — LHGP ↔ ZCode 桥接执行器（通用样板 / generic bridge）
 *
 * 把「官方 ZCode 桌面端自带的无头 runtime」适配成 LHGP 执行器；本文件**不含
 * 任何个人机器信息**——ZCode runtime / node / lhgp / 数据目录的解析见
 * `_discovery.cjs`（配置 → 环境变量 → 自动探测）。
 *
 * registry.json 推荐条目（以 `node setup.cjs` 输出为准）：
 *   "launch": { "argv": ["node", "<本文件绝对路径>", "{task}"] }
 *
 * LHGP 在拉起时注入（本器依赖）：
 *   LHGP_CONTEXT_SNAPSHOT_PATH  active.md 快照（含 contract/attempt id；数据根也由它推导）
 *   LHGP_SESSION_TOKEN          attempt 级凭据（MCP 写回用；本器不直接使用）
 *   进程 cwd                    = 合同 workspace_root
 *
 * 能力：①快照解析 ids + attempt/status 取 lease.generation；②active.md 以
 * --attach 原生挂给 ZCode；③会话续跑（sessionId 持久化，同合同自动 --resume；
 * soft_guidance.zcode.resume=false 可关；max_turns 暂忽略——0.16.5 广告
 * --max-turns 但解析器拒绝，见 README 版本兼容注记）；④流式转发输出并解析
 * --json；⑤退出后自动 attempt/write-back（终态 + 真实 model_id + token 用量）；
 * ⑥派工时间窗防御（execution.dispatch_window，窗口外拒跑并写回 failed）；
 * ⑦供应商业务错误（含 BigModel 5 小时额度）识别入 note 与 zcode-runs.jsonl；
 * ⑧运行审计 start/exit 配对记录——被 attempt 时限硬杀也能留启动痕，不再整段失踪。
 *
 * 离线校验：LHGP_ZCODE_DRYRUN=1 只打印计划 JSON（不拉起 ZCode、不写回）。
 */

"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const {
  loadFileConfig,
  discoverZcodeRuntime,
  discoverNode,
  discoverLhgp,
  readModelId,
  dataRootFromSnapshot,
} = require("./_discovery.cjs");

const LOG_PREFIX = "[lhgp-zcode]";
function log(msg) {
  process.stderr.write(`${LOG_PREFIX} ${msg}\n`);
}

function makeRpc(lhgpBin) {
  return function rpc(method, params, { timeout = 60000 } = {}) {
    // 安全加固（2026-09-12 整改）：签发过凭据的 attempt 做写回时必须出示
    // session_token（与 attempts.session_token_hash 的 sha256 对应）。该凭据由
    // LHGP 在 spawn 时注入进程环境（LHGP_SESSION_TOKEN），这里统一随参数带上，
    // 覆盖 attempt/status 与 attempt/write-back。
    const token = process.env.LHGP_SESSION_TOKEN;
    const withToken = token ? { ...params, session_token: token } : params;
    const b64 = Buffer.from(JSON.stringify(withToken), "utf8").toString("base64url");
    const r = spawnSync(lhgpBin, ["rpc-call", method, "--params-b64", b64], {
      encoding: "utf8",
      timeout,
      maxBuffer: 32 * 1024 * 1024,
    });
    const out = (r.stdout || "").trim();
    if (r.status !== 0 || !out) {
      throw new Error(`rpc ${method} failed (rc=${r.status}): ${(r.stderr || out).slice(0, 400)}`);
    }
    const j = JSON.parse(out);
    return j.result ?? j;
  };
}

function findDeep(obj, key, depth = 0) {
  if (obj === null || typeof obj !== "object" || depth > 6) return undefined;
  if (Object.prototype.hasOwnProperty.call(obj, key)) return obj[key];
  for (const v of Object.values(obj)) {
    const got = findDeep(v, key, depth + 1);
    if (got !== undefined) return got;
  }
  return undefined;
}

function parseIds() {
  const snap = process.env.LHGP_CONTEXT_SNAPSHOT_PATH || "";
  const norm = snap.replace(/\\/g, "/");
  const m = norm.match(/contracts\/([^/]+)\/context\/attempts\/([^/]+)\/active\.md$/);
  return { contract_id: m ? m[1] : null, attempt_id: m ? m[2] : null, snapshot: snap || null };
}

function pickLastJson(text) {
  const starts = [];
  for (let i = 0; i < text.length; i++) if (text[i] === "{") starts.push(i);
  for (let k = starts.length - 1; k >= 0; k--) {
    try {
      const j = JSON.parse(text.slice(starts[k]));
      if (j && typeof j === "object") return j;
    } catch {
      /* keep scanning */
    }
  }
  return null;
}

function mapUsage(zusage) {
  if (!zusage || typeof zusage !== "object") return undefined;
  const out = {
    input_tokens: Number(zusage.inputTokens || 0),
    output_tokens: Number(zusage.outputTokens || 0),
  };
  const cr = Number(zusage.cacheReadTokens || 0);
  const cw = Number(zusage.cacheWriteTokens || 0);
  if (cr > 0) out.cache_read_tokens = cr;
  if (cw > 0) out.cache_write_tokens = cw;
  return out;
}

// 派工时间窗（与 LHGP tick 的 execution.dispatch_window 同一语义）：
// 本地墙钟；跨午夜 start>end；start==end / 非法值视为未配置。
function hhmmToMin(s) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || "").trim());
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  if (h > 23 || mi > 59) return null;
  return h * 60 + mi;
}
function windowInside(start, end, now) {
  const s = hhmmToMin(start);
  const e = hhmmToMin(end);
  if (s === null || e === null || s === e) return true;
  const t = now.getHours() * 60 + now.getMinutes();
  return s < e ? t >= s && t < e : t >= s || t < e;
}

function sessionPath(dataRoot, cid) {
  return path.join(dataRoot, "contracts", cid, "zcode-session.json");
}
function loadSession(dataRoot, cid) {
  try {
    const j = JSON.parse(fs.readFileSync(sessionPath(dataRoot, cid), "utf8"));
    return typeof j.session_id === "string" && j.session_id ? j : null;
  } catch {
    return null;
  }
}
function saveSession(dataRoot, cid, sessionId, attemptId) {
  try {
    fs.writeFileSync(
      sessionPath(dataRoot, cid),
      JSON.stringify(
        { session_id: sessionId, last_attempt_id: attemptId, updated_at: new Date().toISOString() },
        null,
        2
      ),
      "utf8"
    );
  } catch (e) {
    log("warn: cannot persist session: " + e.message);
  }
}
function appendRunLog(dataRoot, cid, entry) {
  try {
    fs.appendFileSync(
      path.join(dataRoot, "contracts", cid, "zcode-runs.jsonl"),
      JSON.stringify(entry) + "\n",
      "utf8"
    );
  } catch {
    /* best-effort */
  }
}

function main() {
  const cfg = loadFileConfig();
  const task = process.argv[2] || "";
  if (!task.trim()) {
    log("fatal: empty task prompt");
    process.exit(64);
  }

  const runtime = discoverZcodeRuntime(cfg);
  if (!runtime) {
    log(
      "fatal: ZCode runtime not found. Install ZCode Desktop, or set ZCODE_RUNTIME / " +
        "ZCODE_INSTALL_DIR / lhgp-zcode.config.json {zcode_runtime}."
    );
    process.exit(66);
  }
  const node = discoverNode(cfg);
  const lhgpBin = discoverLhgp(cfg);
  const ids = parseIds();
  const dataRoot = dataRootFromSnapshot(ids.snapshot, cfg);
  const modelId = readModelId(cfg);
  const dryRun = process.env.LHGP_ZCODE_DRYRUN === "1";

  const rpc = lhgpBin ? makeRpc(lhgpBin) : null;

  let generation = null;
  let contractZcfg = {};
  if (rpc && ids.contract_id && ids.attempt_id) {
    try {
      const st = rpc("attempt/status", { contract_id: ids.contract_id, attempt_id: ids.attempt_id });
      const g = findDeep(st, "generation");
      if (typeof g === "number") generation = g;
    } catch (e) {
      log("warn: attempt/status failed: " + e.message);
    }
    try {
      const cv = rpc("contract/get", { contract_id: ids.contract_id });
      const z = findDeep(cv, "zcode");
      if (z && typeof z === "object") contractZcfg = z;
      const execCfg = findDeep(cv, "execution");
      const win = execCfg && typeof execCfg === "object" ? execCfg.dispatch_window : null;
      if (win && typeof win === "object" && !windowInside(win.start, win.end, new Date())) {
        const msg = `outside dispatch window ${win.start}-${win.end}; refusing to run (defense-in-depth)`;
        log(msg);
        if (generation !== null) {
          try {
            rpc("attempt/write-back", {
              contract_id: ids.contract_id,
              attempt_id: ids.attempt_id,
              write_generation: generation,
              attempt_state: "failed",
              progress_note: "zcode wrapper: " + msg,
              model_id: modelId,
            });
          } catch (e) {
            log("warn: write-back failed: " + e.message);
          }
        }
        process.exit(75);
      }
    } catch (e) {
      log("warn: contract/get failed: " + e.message);
    }
  }

  const resumeDisabled = contractZcfg.resume === false;
  const stored = ids.contract_id && !resumeDisabled ? loadSession(dataRoot, ids.contract_id) : null;
  const zargs = [
    runtime,
    "--prompt",
    task,
    "--json",
    "--cwd",
    process.cwd(),
  ];
  // --max-turns 不传：ZCode 0.16.5 的 --help 广告了该选项（"Maximum model turns for
  // headless prompts"）但参数解析器拒绝它（2026-09-16 两种传参顺序实测均
  // "Unknown option" rc=1；2026-09-15 夜间派工三次快速失败同因）——runtime 文档与
  // 实现不一致。soft_guidance.zcode.max_turns 暂被忽略（字段保留，待 runtime 真正
  // 支持再把本注记换成传参逻辑）。
  if (ids.snapshot) zargs.push("--attach", ids.snapshot);
  if (stored && stored.session_id) zargs.push("--resume", stored.session_id);
  // resume 失效防护（2026-09-18）：应用升级/数据目录迁移会让存量 session 失效
  // （实测 "Error: Model creation failed" rc=1 in 1s），而 daemon 会按失败重派，
  // 连续快速失败把 max_dispatches 预算整批烧光（wav6-openpi-discipline-g1 实案）。
  // 预先算好不带 --resume 的降级参数，供 finish() 快速失败时重试一次。
  const storedResumeFrom = stored && stored.session_id ? stored.session_id : null;
  const resumeFlagIdx = storedResumeFrom ? zargs.indexOf("--resume") : -1;
  const zargsNoResume = resumeFlagIdx >= 0 ? zargs.slice(0, resumeFlagIdx) : zargs;

  if (dryRun) {
    process.stdout.write(
      JSON.stringify(
        {
          dry_run: true,
          node: node.path,
          zcode_runtime: runtime,
          lhgp_bin: lhgpBin,
          data_root: dataRoot,
          contract_id: ids.contract_id,
          attempt_id: ids.attempt_id,
          snapshot: ids.snapshot,
          generation,
          model_id: modelId,
          resume: stored ? stored.session_id : null,
          max_turns: "ignored (runtime 0.16.5 advertises --max-turns but its parser rejects it)",
          zcode_argv: [node.path, ...zargs],
        },
        null,
        2
      ) + "\n"
    );
    process.exit(0);
  }

  if (!lhgpBin) {
    log("warn: lhgp CLI not found; write-back/status disabled (set LHGP_BIN or add lhgp to PATH)");
  }

  const started = Date.now();
  let stdoutBuf = "";
  let stderrBuf = "";
  let activeResumeFrom = storedResumeFrom;
  let retriedWithoutResume = false;

  const spawnChild = (args, resumeFrom) => {
    const c = spawn(node.path, args, { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] });
    // 启动即落一条 start 记录：daemon 按 attempt 时限硬杀（Windows taskkill /F =
    // TerminateProcess，不可捕获）时 finish() 不会执行，start 记录是唯一留痕——
    // 审计侧按 event 配对即可发现「有启动无退出」的被杀运行，不再整段失踪。
    if (ids.contract_id) {
      appendRunLog(dataRoot, ids.contract_id, {
        at: new Date().toISOString(),
        event: "start",
        attempt_id: ids.attempt_id,
        pid: c.pid,
        model_id: modelId,
        resume_from: resumeFrom,
        max_turns: null,
      });
    }
    c.stdout.on("data", (d) => {
      stdoutBuf += d.toString("utf8");
      process.stdout.write(d);
    });
    c.stderr.on("data", (d) => {
      stderrBuf += d.toString("utf8");
      process.stderr.write(d);
    });
    c.on("exit", (code) => finish(code === null ? 1 : code));
    c.on("error", (err) => {
      log("fatal: spawn failed: " + err.message);
      finish(1);
    });
    return c;
  };

  let child = spawnChild(zargs, activeResumeFrom);

  let finished = false;
  const finish = (rc) => {
    if (finished) return;
    const seconds = Math.round((Date.now() - started) / 1000);
    const result = pickLastJson(stdoutBuf);
    const sessionId = result && typeof result.sessionId === "string" ? result.sessionId : null;
    // 快速失败降级：带 --resume 且秒败（≤15s）且没有产出任何 turn JSON 时，
    // 判定存量 session 失效，去掉 --resume 全新起跑重试一次（不重试配额类
    // 业务错误，那类失败重试同样失败）。成功后 saveSession 会写入新 sessionId，
    // 续跑链自愈；只降级一次，避免把失败变成死循环。
    const quotaLike = /使用上限|PROVIDER_BUSINESS_ERROR|quota|rate.?limit/i.test(stderrBuf);
    if (
      rc !== 0 &&
      activeResumeFrom &&
      !retriedWithoutResume &&
      !result &&
      !quotaLike &&
      seconds <= 15
    ) {
      retriedWithoutResume = true;
      activeResumeFrom = null;
      log(`resume session failed fast (rc=${rc} in ${seconds}s); retrying once without --resume`);
      stderrBuf += "\n[lhgp-zcode] retry without --resume after stale-session fast failure\n";
      child = spawnChild(zargsNoResume, null);
      return;
    }
    finished = true;
    const usage = result ? mapUsage(result.usage) : undefined;
    const quotaHit = /使用上限|PROVIDER_BUSINESS_ERROR|quota|rate.?limit/i.test(stderrBuf);
    const ok = rc === 0 && !!result;

    if (sessionId && ids.contract_id) {
      saveSession(dataRoot, ids.contract_id, sessionId, ids.attempt_id || "");
    }

    let note = `zcode headless rc=${rc} in ${seconds}s`;
    if (sessionId) note += `; session=${sessionId}`;
    if (result && typeof result.response === "string") {
      note += `; response≈${result.response.slice(0, 160).replace(/\s+/g, " ")}`;
    }
    if (quotaHit) note += "; PROVIDER_QUOTA/BUSINESS_ERROR detected (see stderr log)";

    if (ids.contract_id) {
      appendRunLog(dataRoot, ids.contract_id, {
        at: new Date().toISOString(),
        event: "exit",
        attempt_id: ids.attempt_id,
        rc,
        seconds,
        session_id: sessionId,
        resume_from: activeResumeFrom,
        model_id: modelId,
        usage: usage || null,
        quota_hit: quotaHit,
      });
    }

    if (rpc && ids.contract_id && ids.attempt_id && generation !== null) {
      try {
        rpc("attempt/write-back", {
          contract_id: ids.contract_id,
          attempt_id: ids.attempt_id,
          write_generation: generation,
          attempt_state: ok ? "succeeded" : "failed",
          progress_note: note.slice(0, 2000),
          model_id: modelId,
          ...(usage ? { usage } : {}),
        });
        log(`write-back sent (${ok ? "succeeded" : "failed"}, gen=${generation})`);
      } catch (e) {
        log("warn: write-back failed: " + e.message);
      }
    } else {
      log("note: skipped write-back (missing ids/generation/rpc)");
    }
    process.exit(rc);
  };

  // 子进程 exit/error 处理已在 spawnChild 内挂接；桥接自身收到可捕获信号时把
  // 当前子进程一起带走并补写退出记录（rc=143 视作被终止）；不可捕获的硬杀
  // （taskkill /F / 停电）由上面的 start 记录兜底留痕。
  // 桥接自身收到可捕获信号时把子进程一起带走并补写退出记录（rc=143 视作被终止）；
  // 不可捕获的硬杀（taskkill /F / 停电）由上面的 start 记录兜底留痕。
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"]) {
    process.on(sig, () => {
      log(`wrapper received ${sig}; terminating child and writing partial exit record`);
      try {
        child.kill();
      } catch {
        /* child may already be gone */
      }
      finish(143);
    });
  }
}

main();
