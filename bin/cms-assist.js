#!/usr/bin/env node
"use strict";

const { setTimeout: sleep } = require("node:timers/promises");
const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_TIMEOUT_MS = 15000;

async function main(argv) {
  loadDotEnv(path.resolve(process.cwd(), ".env"));

  const [command, ...rest] = argv;

  try {
    if (!command || command === "help" || command === "--help" || command === "-h") {
      printHelp();
      return 0;
    }

    if (command === "doctor") {
      return runDoctor(rest);
    }

    if (command === "run") {
      return runAssist(rest);
    }

    throw new UsageError(`Unknown command: ${command}`);
  } catch (error) {
    if (error instanceof UsageError) {
      console.error(`[ERROR] ${error.message}`);
      console.error("Run `cms-assist help` for usage.");
      return 2;
    }

    console.error(`[ERROR] ${error.message}`);
    return 1;
  }
}

function printHelp() {
  console.log(`cms-assist

Usage:
  cms-assist doctor
  cms-assist run --base-url <url> --token <token> --space <id> [--timeout <ms>] [--watch] [--mock]

Environment:
  CMS_BASE_URL
  CMS_API_TOKEN
  CMS_SPACE_ID
  CMS_TIMEOUT_MS
  CMS_MOCK=true
`);
}

function runDoctor(argv) {
  const flags = parseFlags(argv);
  const config = resolveConfig(flags);
  const problems = validateConfig(config);

  console.log("[cms-assist] doctor");
  console.log(`  node: ${process.version}`);
  console.log(`  mode: ${config.mock ? "mock" : "cms"}`);
  console.log(`  base_url: ${config.baseUrl || "(missing)"}`);
  console.log(`  space_id: ${config.spaceId || "(missing)"}`);
  console.log(`  timeout_ms: ${config.timeoutMs}`);
  console.log(`  token: ${maskToken(config.token)}`);

  if (problems.length) {
    for (const problem of problems) {
      console.log(`  problem: ${problem}`);
    }
    return 1;
  }

  console.log("  status: ready");
  return 0;
}

async function runAssist(argv) {
  const flags = parseFlags(argv);
  const config = resolveConfig(flags);
  const problems = validateConfig(config);

  if (problems.length) {
    throw new UsageError(problems.join("; "));
  }

  console.log(`[cms-assist] starting ${config.mock ? "mock" : "cms"} mode`);
  console.log(`[cms-assist] base_url=${config.baseUrl}`);
  console.log(`[cms-assist] space_id=${config.spaceId}`);
  console.log(`[cms-assist] timeout_ms=${config.timeoutMs}`);

  if (config.mock) {
    console.log("[cms-assist] mock CMS adapter is active");
  } else {
    await checkCmsReachable(config);
  }

  if (!config.watch) {
    console.log("[cms-assist] ready");
    return 0;
  }

  console.log("[cms-assist] watch mode is running. Press Ctrl+C to stop.");
  let tick = 0;
  while (true) {
    tick += 1;
    console.log(`[cms-assist] heartbeat ${tick}`);
    await sleep(5000);
  }
}

function parseFlags(argv) {
  const flags = {};

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) {
      throw new UsageError(`Unexpected argument: ${arg}`);
    }

    const key = arg.slice(2);
    if (key === "watch" || key === "mock") {
      flags[key] = true;
      continue;
    }

    const value = argv[i + 1];
    if (!value || value.startsWith("--")) {
      throw new UsageError(`Missing value for --${key}`);
    }

    flags[key] = value;
    i += 1;
  }

  return flags;
}

function resolveConfig(flags) {
  return {
    baseUrl: clean(flags["base-url"] || process.env.CMS_BASE_URL || ""),
    token: clean(flags.token || process.env.CMS_API_TOKEN || ""),
    spaceId: clean(flags.space || process.env.CMS_SPACE_ID || ""),
    timeoutMs: parseTimeout(flags.timeout || process.env.CMS_TIMEOUT_MS || DEFAULT_TIMEOUT_MS),
    watch: Boolean(flags.watch),
    mock: Boolean(flags.mock) || isTruthy(process.env.CMS_MOCK),
  };
}

function parseTimeout(value) {
  const parsed = Number(clean(value));
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new UsageError(`Invalid timeout: ${value}`);
  }
  return parsed;
}

function validateConfig(config) {
  const problems = [];
  if (!config.baseUrl) problems.push("CMS_BASE_URL/base-url is required");
  if (!config.token) problems.push("CMS_API_TOKEN/token is required");
  if (!config.spaceId) problems.push("CMS_SPACE_ID/space is required");

  try {
    if (config.baseUrl) new URL(config.baseUrl);
  } catch {
    problems.push("CMS_BASE_URL/base-url must be a valid URL");
  }

  return problems;
}

async function checkCmsReachable(config) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.timeoutMs);
  const url = new URL(config.baseUrl);

  try {
    const response = await fetch(url, {
      method: "HEAD",
      headers: {
        authorization: `Bearer ${config.token}`,
        "x-cms-space-id": config.spaceId,
      },
      signal: controller.signal,
    });

    console.log(`[cms-assist] cms responded with HTTP ${response.status}`);
  } finally {
    clearTimeout(timeout);
  }
}

function isTruthy(value) {
  return /^(1|true|yes|on)$/i.test(clean(value));
}

function maskToken(token) {
  if (!token) return "(missing)";
  if (token.length <= 8) return "********";
  return `${token.slice(0, 4)}...${token.slice(-4)}`;
}

function clean(value) {
  return String(value || "").trim();
}

function loadDotEnv(filePath) {
  if (!fs.existsSync(filePath)) {
    return;
  }

  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }

    const eq = line.indexOf("=");
    if (eq < 1) {
      continue;
    }

    const name = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith("\"") && value.endsWith("\"")) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }

    if (process.env[name] === undefined) {
      process.env[name] = value;
    }
  }
}

class UsageError extends Error {}

if (require.main === module) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}

module.exports = {
  parseFlags,
  resolveConfig,
  validateConfig,
  maskToken,
  main,
  loadDotEnv,
};
