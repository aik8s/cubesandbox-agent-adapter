#!/usr/bin/env node

import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { chmod, mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import process from "node:process";

function fail(message) {
  throw new Error(message);
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
  for (const name of ["opencode-bin", "config", "model", "output"]) {
    if (!values[name]) fail(`--${name} is required`);
  }
  return values;
}

function run(command, args, env) {
  return new Promise((accept, reject) => {
    const child = spawn(command, args, { env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (value) => { stdout += value; });
    child.stderr.on("data", (value) => { stderr += value; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) reject(new Error(`OpenCode exited ${code}: ${stderr.trim()}`));
      else accept({ stdout, stderr });
    });
  });
}

function parseEvents(source) {
  return source.split(/\r?\n/).filter(Boolean).map((line, index) => {
    try {
      return JSON.parse(line);
    } catch (error) {
      throw new Error(`invalid OpenCode JSON event on line ${index + 1}`);
    }
  });
}

function parseToolOutput(event) {
  const raw = event?.part?.state?.output;
  if (typeof raw !== "string") fail(`missing output for ${event?.part?.tool || "tool"}`);
  try {
    return JSON.parse(raw);
  } catch {
    fail(`non-JSON output for ${event?.part?.tool || "tool"}`);
  }
}

function shortHash(value) {
  return createHash("sha256").update(String(value)).digest("hex").slice(0, 12);
}

async function main() {
  const values = parseArgs(process.argv.slice(2));
  const opencode = resolve(values["opencode-bin"]);
  const config = resolve(values.config);
  const output = resolve(values.output);
  const stateRoot = resolve(values["state-dir"] || dirname(output), "opencode-state");
  const prompt = [
    "Use only the cubesandbox trusted-task MCP tools.",
    "Call cube_task_plan with template trusted-auto and parameters message safe-opencode-acceptance.",
    "Submit the plan, poll status until terminal, fetch result, then fetch receipt.",
    "Do not print tokens, URLs, filesystem paths, command text, complete Plan/Task/Sandbox identifiers, or receipt payloads.",
    "Finish with seven short lines labeled OpenCode CubeSandbox acceptance, Client, Backend, Tools, Status, Cleanup, and Receipt.",
  ].join(" ");
  const env = {
    ...process.env,
    OPENCODE_CONFIG: config,
    XDG_CONFIG_HOME: resolve(stateRoot, "config"),
    XDG_DATA_HOME: resolve(stateRoot, "data"),
    XDG_CACHE_HOME: resolve(stateRoot, "cache"),
    XDG_STATE_HOME: resolve(stateRoot, "state"),
  };
  const versionRun = await run(opencode, ["--version"], env);
  const version = versionRun.stdout.trim().split(/\s+/)[0];
  const execution = await run(opencode, [
    "run",
    "--pure",
    "--format", "json",
    "--model", values.model,
    "--title", "CubeSandbox trusted task acceptance",
    prompt,
  ], env);
  const events = parseEvents(execution.stdout);
  const tools = events.filter((event) => event.type === "tool_use");
  const names = tools.map((event) => event.part?.tool);
  const allowed = new Set([
    "cubesandbox_cube_task_plan",
    "cubesandbox_cube_task_submit",
    "cubesandbox_cube_task_status",
    "cubesandbox_cube_task_result",
    "cubesandbox_cube_task_receipt",
  ]);
  if (names.some((name) => !allowed.has(name))) fail(`unexpected tool sequence: ${names.join(", ")}`);
  if (names[0] !== "cubesandbox_cube_task_plan" || names[1] !== "cubesandbox_cube_task_submit") {
    fail(`unexpected tool sequence: ${names.join(", ")}`);
  }
  if (!names.slice(2, -2).length || names.slice(2, -2).some((name) => name !== "cubesandbox_cube_task_status")) {
    fail(`missing status polling: ${names.join(", ")}`);
  }
  if (names.at(-2) !== "cubesandbox_cube_task_result" || names.at(-1) !== "cubesandbox_cube_task_receipt") {
    fail(`unexpected terminal tool sequence: ${names.join(", ")}`);
  }
  if (tools.some((event) => event.part?.state?.status !== "completed")) fail("one or more MCP tools did not complete");

  const plan = parseToolOutput(tools[0]);
  const status = parseToolOutput(tools.at(-3));
  const result = parseToolOutput(tools.at(-2));
  const receipt = parseToolOutput(tools.at(-1));
  if (plan.state !== "ready") fail(`plan state is ${plan.state}`);
  if (status.state !== "succeeded" || result.state !== "succeeded") fail("trusted task did not succeed");
  if (result.result?.cleanup !== "verified") fail("MicroVM cleanup was not verified");
  if (receipt.receipt?.signature?.alg !== "HS256") fail("unexpected receipt algorithm");

  const report = {
    title: "OpenCode CubeSandbox acceptance",
    generated_at: new Date().toISOString(),
    result: "PASS",
    client: `OpenCode ${version}`,
    transport: "local MCP stdio",
    backend: "CubeSandbox v0.7.1 real MicroVM",
    policy: "trusted-task-only",
    tools: names,
    state: result.state,
    cleanup: result.result.cleanup,
    receipt_alg: receipt.receipt.signature.alg,
    plan_ref: `plan#${shortHash(plan.plan_ref)}`,
    task_ref: `task#${shortHash(result.task_ref)}`,
    privacy: "Tokens, addresses, paths, command text, full identifiers, model credentials, and receipt payload are excluded.",
  };
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o644 });
  await chmod(output, 0o644);
  process.stdout.write(JSON.stringify({ result: report.result, client: report.client, tools: names.length, state: report.state, cleanup: report.cleanup, receipt_alg: report.receipt_alg }) + "\n");
}

await main();
