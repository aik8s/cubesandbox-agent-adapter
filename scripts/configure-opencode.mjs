#!/usr/bin/env node

import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";
import process from "node:process";

const TRUSTED_TASK_TOOLS = [
  "cube_task_plan",
  "cube_task_submit",
  "cube_task_status",
  "cube_task_result",
  "cube_task_cancel",
  "cube_task_receipt",
];

function fail(message) {
  process.stderr.write(`error: ${message}\n`);
  process.exit(1);
}

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (!name.startsWith("--")) fail(`unexpected argument: ${name}`);
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) fail(`${name} requires a value`);
    values[name.slice(2)] = value;
    index += 1;
  }
  for (const name of ["config", "python", "repo", "adapter-url", "profile", "token-file", "format", "mode"]) {
    if (!values[name]) fail(`--${name} is required`);
  }
  if (!["v1", "v2"].includes(values.format)) fail("--format must be v1 or v2");
  if (!["trusted", "full"].includes(values.mode)) fail("--mode must be trusted or full");
  return values;
}

function requireAbsolute(name, value) {
  if (!isAbsolute(value)) fail(`--${name} must be an absolute path`);
}

function validateAdapterUrl(raw) {
  let value;
  try {
    value = new URL(raw);
  } catch {
    fail("--adapter-url must be an absolute HTTP(S) URL");
  }
  if (!["http:", "https:"].includes(value.protocol)) {
    fail("--adapter-url must be an absolute HTTP(S) URL");
  }
  if (value.protocol === "http:" && !["127.0.0.1", "::1", "[::1]", "localhost"].includes(value.hostname)) {
    fail("plain HTTP is allowed only for a loopback Adapter URL");
  }
}

async function loadConfig(path) {
  try {
    const source = await readFile(path, "utf8");
    const value = JSON.parse(source);
    if (!value || Array.isArray(value) || typeof value !== "object") {
      fail(`OpenCode config must contain a JSON object: ${path}`);
    }
    return value;
  } catch (error) {
    if (error?.code === "ENOENT") return {};
    if (error instanceof SyntaxError) {
      fail(`cannot safely merge non-JSON config ${path}; use a dedicated --config-file`);
    }
    throw error;
  }
}

function serverConfig(values, v2 = false) {
  const server = {
    type: "local",
    command: [values.python, "-m", "adapter.mcp_server"],
    environment: {
      PYTHONPATH: values.repo,
      CUBE_ADAPTER_URL: values["adapter-url"],
      CUBE_ADAPTER_PROFILE: values.profile,
      CUBE_ADAPTER_TOKEN_FILE: values["token-file"],
    },
  };
  if (v2) server.codemode = false;
  else server.enabled = true;
  return server;
}

function mergeV1(config, values) {
  const mcp = config.mcp && typeof config.mcp === "object" && !Array.isArray(config.mcp)
    ? { ...config.mcp }
    : {};
  mcp.cubesandbox = serverConfig(values);
  config.mcp = mcp;

  const existing = config.permission && typeof config.permission === "object" && !Array.isArray(config.permission)
    ? config.permission
    : {};
  const permission = Object.fromEntries(
    Object.entries(existing).filter(([name]) => !name.startsWith("cubesandbox_")),
  );
  permission["cubesandbox_*"] = values.mode === "trusted" ? "deny" : "ask";
  if (values.mode === "trusted") {
    for (const tool of TRUSTED_TASK_TOOLS) permission[`cubesandbox_${tool}`] = "allow";
  }
  config.permission = permission;
  return config;
}

function mergeV2(config, values) {
  const mcp = config.mcp && typeof config.mcp === "object" && !Array.isArray(config.mcp)
    ? { ...config.mcp }
    : {};
  const servers = mcp.servers && typeof mcp.servers === "object" && !Array.isArray(mcp.servers)
    ? { ...mcp.servers }
    : {};
  servers.cubesandbox = serverConfig(values, true);
  mcp.servers = servers;
  config.mcp = mcp;

  const existing = Array.isArray(config.permissions) ? config.permissions : [];
  const permissions = existing.filter((rule) => {
    return !rule || typeof rule !== "object" || !String(rule.action || "").startsWith("cubesandbox_");
  });
  permissions.push({
    action: "cubesandbox_*",
    resource: "*",
    effect: values.mode === "trusted" ? "deny" : "ask",
  });
  if (values.mode === "trusted") {
    for (const tool of TRUSTED_TASK_TOOLS) {
      permissions.push({ action: `cubesandbox_${tool}`, resource: "*", effect: "allow" });
    }
  }
  config.permissions = permissions;
  return config;
}

async function main() {
  const values = parseArgs(process.argv.slice(2));
  for (const name of ["config", "python", "repo", "token-file"]) requireAbsolute(name, values[name]);
  validateAdapterUrl(values["adapter-url"]);
  values.config = resolve(values.config);
  values.python = resolve(values.python);
  values.repo = resolve(values.repo);
  values["token-file"] = resolve(values["token-file"]);

  const config = await loadConfig(values.config);
  if (!config.$schema) config.$schema = "https://opencode.ai/config.json";
  const merged = values.format === "v2" ? mergeV2(config, values) : mergeV1(config, values);
  const output = `${JSON.stringify(merged, null, 2)}\n`;

  await mkdir(dirname(values.config), { recursive: true, mode: 0o700 });
  const temporary = `${values.config}.tmp-${process.pid}`;
  await writeFile(temporary, output, { encoding: "utf8", mode: 0o600 });
  await chmod(temporary, 0o600);
  await rename(temporary, values.config);
  await chmod(values.config, 0o600);
  process.stdout.write(`${values.config}\n`);
}

await main();
