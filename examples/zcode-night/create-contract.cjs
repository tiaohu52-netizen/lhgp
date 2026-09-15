#!/usr/bin/env node
/**
 * create-contract.cjs — 从 JSON 样板起草合同（准备 + 可选批准）
 *
 * 用法：
 *   node create-contract.cjs contract.example.json            # 只 prepare（drafted）
 *   node create-contract.cjs contract.example.json --approve  # prepare 后 approve（active）
 *
 * 使用前把 template 里的 REPLACE_* 占位改掉；contract_id / goal_id 可自定。
 * LHGP 位置：--lhgp <路径> 或环境变量 LHGP_BIN 或 PATH 上的 lhgp。
 */

"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { loadFileConfig, discoverLhgp } = require("./_discovery.cjs");

const argv = process.argv.slice(2);
const file = argv.find((a) => !a.startsWith("--"));
const approve = argv.includes("--approve");
if (!file) {
  console.error("usage: node create-contract.cjs <contract.json> [--approve]");
  process.exit(64);
}

const cfg = loadFileConfig();
const lhgpBin = discoverLhgp(cfg);
if (!lhgpBin) {
  console.error("lhgp CLI not found (set LHGP_BIN or add lhgp to PATH)");
  process.exit(66);
}

const spec = JSON.parse(fs.readFileSync(path.resolve(file), "utf8").replace(/^\uFEFF/, ""));
const draftText = JSON.stringify(spec.draft);
if (draftText.includes("REPLACE_")) {
  console.error("template still contains REPLACE_* placeholders; edit them first");
  process.exit(65);
}

function rpc(method, params) {
  const b64 = Buffer.from(JSON.stringify(params), "utf8").toString("base64url");
  const r = spawnSync(lhgpBin, ["rpc-call", method, "--params-b64", b64], {
    encoding: "utf8",
    timeout: 60000,
    maxBuffer: 16 * 1024 * 1024,
  });
  if (r.status !== 0) {
    console.error(`rpc ${method} failed: ${(r.stderr || r.stdout || "").slice(0, 500)}`);
    process.exit(1);
  }
  return JSON.parse(r.stdout);
}

console.log("== contract/prepare ==");
const prep = rpc("contract/prepare", {
  contract_id: spec.contract_id,
  goal_id: spec.goal_id,
  draft: spec.draft,
});
const view = prep.result ?? prep;
console.log("prepared:", view.contract_id ?? spec.contract_id, view.state ?? "");

if (approve) {
  console.log("== contract/approve ==");
  const r = spawnSync(lhgpBin, ["approve", spec.contract_id], { encoding: "utf8", timeout: 60000 });
  const ok = /\bactive\b/.test(r.stdout || "");
  console.log(ok ? "approved: active" : (r.stdout || r.stderr || "").slice(0, 300));
  process.exit(ok ? 0 : 1);
}
