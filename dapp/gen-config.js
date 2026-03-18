#!/usr/bin/env node
/**
 * Generate config.json from .env
 * Usage: node gen-config.js  or  cd dapp && node gen-config.js
 * Reads backend/.env or project root .env
 */
const fs = require("fs");
const path = require("path");

function loadEnv(filePath) {
  if (!fs.existsSync(filePath)) return {};
  const content = fs.readFileSync(filePath, "utf-8");
  const out = {};
  for (const line of content.split("\n")) {
    const m = line.match(/^\s*([^#=]+)=(.*)$/);
    if (m) out[m[1].trim()] = m[2].trim().replace(/^["']|["']$/g, "");
  }
  return out;
}

const root = path.resolve(__dirname, "..");
const envPaths = [
  path.join(root, "backend", ".env"),
  path.join(root, ".env"),
];
let env = {};
for (const p of envPaths) {
  if (fs.existsSync(p)) {
    env = loadEnv(p);
    break;
  }
}

const chainId = env.BSC_TESTNET === "true" || (env.CHAIN_ID && env.CHAIN_ID !== "56") ? 97 : 56;
const rpcUrl = env.QUICKNODE_HTTP_URL || env.BSC_RPC_URL || (chainId === 97 ? "https://data-seed-prebsc-1-s1.binance.org:8545/" : "https://bsc-dataseed.binance.org/");

const config = {
  councilAddress: env.KEEPER_COUNCIL_ADDRESS || "",
  controllerAddress: env.CONTROLLER_ADDRESS || "",
  chainId,
  rpcUrl: rpcUrl || "",
};

const outPath = path.join(__dirname, "config.json");
fs.writeFileSync(outPath, JSON.stringify(config, null, 2), "utf-8");
console.log("Generated", outPath);
console.log(JSON.stringify(config, null, 2));
