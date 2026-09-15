#!/usr/bin/env node
/**
 * setup.cjs — ZCode 夜间模式一键体检 / 接线（样板）
 *
 * 默认**只读**：探测并打印体检报告与待粘贴片段。
 * 显式开关才会写盘（写前自动备份原文件）：
 *   --wire-mcp        把 lhgp MCP 服务器合并进 ZCode 用户配置（~/.zcode/cli/config.json）
 *   --write-registry  把 zcode-desktop 执行器合并进 <数据根>/registry.json
 *   --fix-bom         去掉 ZCode 配置可能存在的 UTF-8 BOM（BOM 会让官方 runtime 误报"缺模型配置"）
 *   --json            机器可读输出
 *
 * 环境变量覆盖：LHGP_BIN / LHGP_DATA_DIR / ZCODE_RUNTIME / ZCODE_INSTALL_DIR /
 *               ZCODE_NODE / ZCODE_CONFIG / LHGP_ZCODE_CONFIG
 */

"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const {
  loadFileConfig,
  discoverZcodeRuntime,
  discoverNode,
  discoverLhgp,
  readZcodeConfig,
  dataRootFromSnapshot,
} = require("./_discovery.cjs");

const WRAPPER = path.resolve(__filename, "..", "lhgp-zcode.cjs");
const args = new Set(process.argv.slice(2));
const asJson = args.has("--json");

function discoverLhgpMcp(lhgpBin) {
  try {
    const which = process.platform === "win32" ? "where" : "which";
    const w = spawnSync(which, ["lhgp-mcp"], { encoding: "utf8", timeout: 15000 });
    if (w.status === 0 && w.stdout.trim()) return w.stdout.trim().split(/\r?\n/)[0].trim();
  } catch {}
  if (lhgpBin && path.isAbsolute(lhgpBin)) {
    const dir = path.dirname(lhgpBin);
    for (const n of ["lhgp-mcp.exe", "lhgp-mcp.cmd", "lhgp-mcp"]) {
      const p = path.join(dir, n);
      if (fs.existsSync(p)) return p;
    }
  }
  return null;
}

function nodeSatisfies(version) {
  const m = /^v(\d+)\.(\d+)/.exec(version || "");
  if (!m) return false;
  const major = Number(m[1]);
  const minor = Number(m[2]);
  return major > 22 || (major === 22 && minor >= 19);
}

function backupOnce(p) {
  const bak = `${p}.bak-${new Date().toISOString().replace(/[:.]/g, "-")}`;
  fs.copyFileSync(p, bak);
  return bak;
}

function main() {
  const cfg = loadFileConfig();
  const node = discoverNode(cfg);
  const runtime = discoverZcodeRuntime(cfg);
  const lhgpBin = discoverLhgp(cfg);
  const lhgpMcp = discoverLhgpMcp(lhgpBin);
  const zcfg = readZcodeConfig(cfg);
  const dataRoot = dataRootFromSnapshot("", cfg);
  const registryPath = path.join(dataRoot, "registry.json");
  let registry = null;
  try {
    registry = JSON.parse(fs.readFileSync(registryPath, "utf8"));
  } catch {}
  const hasEntry = !!(registry && (registry.agents || []).some((a) => a.id === "zcode-desktop"));

  let daemonRunning = null;
  if (lhgpBin) {
    try {
      const r = spawnSync(lhgpBin, ["status"], { encoding: "utf8", timeout: 20000 });
      daemonRunning = JSON.parse(r.stdout || "{}").running === true;
    } catch {}
  }

  const provider = zcfg.config && zcfg.config.provider ? Object.keys(zcfg.config.provider) : [];
  const apiKeyPresent = (() => {
    if (!zcfg.config || !zcfg.config.provider) return false;
    return Object.values(zcfg.config.provider).some(
      (p) => p && p.options && typeof p.options.apiKey === "string" && p.options.apiKey.length > 0
    );
  })();
  const modelMain = zcfg.config && zcfg.config.model ? zcfg.config.model.main : null;

  const report = {
    node: { path: node.path, version: node.version, ok: nodeSatisfies(node.version) },
    zcode_runtime: { path: runtime, ok: !!runtime },
    zcode_config: {
      path: zcfg.path,
      exists: zcfg.exists,
      has_bom: zcfg.hasBom,
      providers: provider,
      api_key_present: apiKeyPresent,
      model_main: modelMain,
    },
    lhgp_bin: { path: lhgpBin, ok: !!lhgpBin },
    lhgp_mcp: { path: lhgpMcp, ok: !!lhgpMcp },
    data_root: dataRoot,
    registry: { path: registryPath, has_zcode_desktop: hasEntry },
    daemon_running: daemonRunning,
  };

  const registrySnippet = {
    id: "zcode-desktop",
    kind: "subprocess",
    enabled: true,
    models: ["*"],
    cost_hint: "medium",
    launch: {
      argv: [node.path, WRAPPER, "{task}"],
      cwd: null,
      env_allowlist: [
        "PATH", "PATHEXT", "SystemRoot", "SystemDrive", "ComSpec", "windir",
        "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
        "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "TERM", "LANG",
        "LHGP_BIN", "LHGP_DATA_DIR", "ZCODE_RUNTIME", "ZCODE_INSTALL_DIR",
        "ZCODE_NODE", "ZCODE_CONFIG", "LHGP_ZCODE_CONFIG",
      ],
    },
    capabilities: {
      spawn: true,
      observe: true,
      cancel: true,
      notify: false,
      followup: false,
      steer: false,
      interrupt: false,
      context: "optional",
      sandbox: {
        file_effects: "workspace-write",
        network: "allow",
        process: "restricted",
        enforcement: "partial",
      },
      acceptance_evidence: true,
    },
    limits: { max_concurrent_attempts: 1, max_output_bytes: 1048576 },
  };

  // ── 写操作（显式开关）───────────────────────────────────────────────
  const actions = [];
  if (args.has("--fix-bom")) {
    if (zcfg.exists && zcfg.hasBom) {
      const raw = fs.readFileSync(zcfg.path, "utf8").replace(/^\uFEFF/, "");
      const bak = backupOnce(zcfg.path);
      fs.writeFileSync(zcfg.path, raw, "utf8");
      actions.push(`fix-bom: stripped BOM (backup: ${bak})`);
      report.zcode_config.has_bom = false;
    } else {
      actions.push("fix-bom: no BOM present, nothing to do");
    }
  }
  if (args.has("--wire-mcp")) {
    if (!zcfg.exists || !zcfg.config) {
      actions.push("wire-mcp: SKIPPED (ZCode config missing or unparseable)");
    } else if (!lhgpMcp) {
      actions.push("wire-mcp: SKIPPED (lhgp-mcp not found; install LHGP or set LHGP_BIN)");
    } else {
      const config = zcfg.config;
      config.mcp = config.mcp || {};
      config.mcp.servers = config.mcp.servers || {};
      if (config.mcp.servers.lhgp) {
        actions.push("wire-mcp: lhgp entry already present, left untouched");
      } else {
        config.mcp.servers.lhgp = {
          command: lhgpMcp,
          args: [],
          env: { LHGP_MCP_PROFILE: "executor" },
        };
        const bak = backupOnce(zcfg.path);
        fs.writeFileSync(zcfg.path, JSON.stringify(config, null, 2), "utf8");
        actions.push(`wire-mcp: added mcp.servers.lhgp -> ${lhgpMcp} (backup: ${bak})`);
      }
    }
  }
  if (args.has("--write-registry")) {
    try {
      const reg = registry && typeof registry === "object" ? registry : { agents: [] };
      reg.agents = Array.isArray(reg.agents) ? reg.agents : [];
      const i = reg.agents.findIndex((a) => a.id === "zcode-desktop");
      if (i >= 0) reg.agents[i] = registrySnippet;
      else reg.agents.push(registrySnippet);
      fs.mkdirSync(dataRoot, { recursive: true });
      const bak = fs.existsSync(registryPath) ? backupOnce(registryPath) : null;
      fs.writeFileSync(registryPath, JSON.stringify(reg, null, 2), "utf8");
      actions.push(
        `write-registry: upserted zcode-desktop into ${registryPath}${bak ? ` (backup: ${bak})` : ""}`
      );
    } catch (e) {
      actions.push("write-registry: FAILED " + e.message);
    }
  }

  if (asJson) {
    process.stdout.write(JSON.stringify({ report, registry_snippet: registrySnippet, actions }, null, 2) + "\n");
    return;
  }

  const mark = (ok) => (ok ? "[ok]" : "[!!]");
  console.log("=== ZCode 夜间模式 · 体检 ===");
  console.log(`${mark(report.node.ok)} node        ${report.node.path} (${report.node.version})`);
  console.log(`${mark(!!runtime)} ZCode runtime ${runtime || "未找到 → 安装 ZCode 桌面端，或设 ZCODE_RUNTIME / ZCODE_INSTALL_DIR"}`);
  console.log(
    `${mark(zcfg.exists)} ZCode 配置    ${zcfg.path}` +
      (zcfg.exists ? `  providers=[${provider.join(", ")}] apiKey=${apiKeyPresent ? "有" : "无"} model=${modelMain || "-"}` : "")
  );
  if (zcfg.exists && zcfg.hasBom) console.log("  [warn] 配置文件带 UTF-8 BOM：官方 runtime 会误报「Model config is missing」——运行 node setup.cjs --fix-bom 修复");
  if (zcfg.exists && !apiKeyPresent) console.log("  [warn] 未检测到 provider.apiKey：先在 ZCode 桌面端/CLI 配好模型访问（BigModel Coding Plan）");
  console.log(`${mark(!!lhgpBin)} lhgp        ${lhgpBin || "未找到 → 加入 PATH 或设 LHGP_BIN"}`);
  console.log(`${mark(!!lhgpMcp)} lhgp-mcp    ${lhgpMcp || "未找到（MCP 写回需要）"}`);
  console.log(`  data root   ${dataRoot}`);
  console.log(`${mark(hasEntry)} registry    ${registryPath}${hasEntry ? "（zcode-desktop 已登记）" : "（未登记 zcode-desktop）"}`);
  if (daemonRunning !== null) console.log(`${mark(daemonRunning)} daemon      ${daemonRunning ? "running" : "未运行 → lhgp start（建议加看门狗）"}`);
  console.log("");
  console.log("下一步：");
  console.log("  1) 接线： node setup.cjs --fix-bom --wire-mcp --write-registry");
  console.log("  2) 起草夜战合同（见 contract.example.json；工作窗口 execution.dispatch_window）");
  console.log("  3) 观察窗口外不派工、窗口内自动开工；wrapper 审计见 <data root>/contracts/<cid>/zcode-runs.jsonl");
  if (actions.length) {
    console.log("");
    console.log("=== 本次写操作 ===");
    for (const a of actions) console.log("  - " + a);
  }
}

main();
