/**
 * _discovery.cjs — ZCode / node / LHGP 位置探测（样板共享模块）
 *
 * 解析顺序：本目录 lhgp-zcode.config.json（可用 LHGP_ZCODE_CONFIG 指定）→
 * 环境变量 → 自动探测（Windows：运行中进程 / 卸载注册表 / 常见目录；
 * macOS / Linux：常见路径）。样板内不写死任何个人机器路径。
 */

"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

function loadFileConfig() {
  const p = process.env.LHGP_ZCODE_CONFIG
    ? path.resolve(process.env.LHGP_ZCODE_CONFIG)
    : path.join(__dirname, "lhgp-zcode.config.json");
  try {
    return JSON.parse(fs.readFileSync(p, "utf8").replace(/^\uFEFF/, ""));
  } catch {
    return {};
  }
}

function firstFile(candidates) {
  for (const c of candidates) {
    if (c && typeof c === "string" && fs.existsSync(c)) return c;
  }
  return null;
}

function win32PowerShell(script) {
  // 统一以 UTF-8 输出，避免中文路径在 cp936 下被错误解码
  const prelude = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;";
  const r = spawnSync(
    "powershell.exe",
    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", prelude + script],
    { encoding: "utf8", timeout: 20000 }
  );
  return (r.stdout || "").trim();
}

function win32RuntimeCandidatesFromHost() {
  const out = [];
  if (process.platform !== "win32") return out;
  try {
    const exe = win32PowerShell(
      "(Get-Process ZCode -ErrorAction SilentlyContinue | Where-Object {$_.Path} | Select-Object -First 1 -ExpandProperty Path)"
    );
    if (exe) out.push(path.dirname(exe));
  } catch {}
  try {
    const raw = win32PowerShell(
      "$k='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*';" +
        "(Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object {$_.DisplayName -match 'ZCode'} | Select-Object -First 1 | ForEach-Object { $_.UninstallString; $_.DisplayIcon })"
    );
    for (const line of raw.split(/\r?\n/)) {
      const m = /"([^"]+)"/.exec(line) || /^(\S.*?)(?:\s+\/\w+)?$/.exec(line.trim());
      if (m && m[1]) out.push(path.dirname(m[1].trim()));
    }
  } catch {}
  return out;
}

function commonZcodeDirs() {
  const home = process.env.USERPROFILE || process.env.HOME || "";
  const dirs = [];
  const local = process.env.LOCALAPPDATA;
  const pf = process.env.ProgramFiles;
  const pf86 = process.env["ProgramFiles(x86)"];
  if (local) dirs.push(path.join(local, "Programs", "ZCode"), path.join(local, "ZCode"));
  if (pf) dirs.push(path.join(pf, "ZCode"));
  if (pf86) dirs.push(path.join(pf86, "ZCode"));
  if (process.platform === "darwin") dirs.push("/Applications/ZCode.app/Contents/Resources");
  if (process.platform === "linux") dirs.push(path.join(home, ".local", "share", "ZCode"), "/opt/ZCode");
  return dirs;
}

function discoverZcodeRuntime(cfg = {}) {
  const direct = firstFile([
    cfg.zcode_runtime,
    process.env.ZCODE_RUNTIME,
    process.env.ZCODE_INSTALL_DIR &&
      path.join(process.env.ZCODE_INSTALL_DIR, "resources", "glm", "zcode.cjs"),
    process.env.ZCODE_INSTALL_DIR && path.join(process.env.ZCODE_INSTALL_DIR, "glm", "zcode.cjs"),
    process.env.ZCODE_HOME && path.join(process.env.ZCODE_HOME, "resources", "glm", "zcode.cjs"),
  ]);
  if (direct) return direct;
  const dirs = [
    ...(process.env.ZCODE_INSTALL_DIR ? [process.env.ZCODE_INSTALL_DIR] : []),
    ...win32RuntimeCandidatesFromHost(),
    ...commonZcodeDirs(),
  ];
  const subpaths = [
    ["resources", "glm", "zcode.cjs"],
    ["glm", "zcode.cjs"],
  ];
  for (const d of dirs) {
    for (const s of subpaths) {
      const p = path.join(d, ...s);
      if (fs.existsSync(p)) return p;
    }
  }
  return null;
}

function discoverNode(cfg = {}) {
  const explicit = firstFile([cfg.node, process.env.ZCODE_NODE]);
  const chosen = explicit || process.execPath;
  let version = "";
  try {
    version = spawnSync(chosen, ["--version"], { encoding: "utf8", timeout: 15000 }).stdout.trim();
  } catch {}
  return { path: chosen, version };
}

function probeLhgp(candidate) {
  if (!candidate) return false;
  try {
    const r = spawnSync(candidate, ["--version"], { encoding: "utf8", timeout: 15000 });
    return r.status === 0;
  } catch {
    return false;
  }
}

function discoverLhgp(cfg = {}) {
  const cands = [cfg.lhgp_bin, process.env.LHGP_BIN, "lhgp", "lhgp.exe", "lhgp.cmd"].filter(Boolean);
  for (const c of cands) {
    if (firstFile([c]) || !path.isAbsolute(c)) {
      if (probeLhgp(c)) return c;
    }
  }
  return null;
}

function zcodeConfigPath(cfg = {}) {
  if (cfg.zcode_config) return path.resolve(cfg.zcode_config);
  if (process.env.ZCODE_CONFIG) return path.resolve(process.env.ZCODE_CONFIG);
  const home = process.env.USERPROFILE || process.env.HOME || "";
  return path.join(home, ".zcode", "cli", "config.json");
}

function readZcodeConfig(cfg = {}) {
  const p = zcodeConfigPath(cfg);
  let raw = "";
  try {
    raw = fs.readFileSync(p, "utf8");
  } catch {
    return { path: p, exists: false, hasBom: false, config: null };
  }
  const hasBom = raw.startsWith("\uFEFF");
  let config = null;
  try {
    config = JSON.parse(raw.replace(/^\uFEFF/, ""));
  } catch {}
  return { path: p, exists: true, hasBom, config };
}

function readModelId(cfg = {}) {
  const { config } = readZcodeConfig(cfg);
  const main = config && config.model && config.model.main;
  return typeof main === "string" && main ? main : "*";
}

function dataRootFromSnapshot(snapshot, cfg = {}) {
  const norm = (snapshot || "").replace(/\\/g, "/");
  const m = norm.match(/^(.*)\/contracts\/[^/]+\/context\/attempts\/[^/]+\/active\.md$/);
  if (m) return m[1].replace(/\//g, path.sep);
  if (cfg.data_root) return cfg.data_root;
  if (process.env.LHGP_DATA_DIR) return process.env.LHGP_DATA_DIR;
  const home = process.env.USERPROFILE || process.env.HOME || "";
  return path.join(home, ".lhgp");
}

module.exports = {
  loadFileConfig,
  firstFile,
  discoverZcodeRuntime,
  discoverNode,
  discoverLhgp,
  zcodeConfigPath,
  readZcodeConfig,
  readModelId,
  dataRootFromSnapshot,
};
