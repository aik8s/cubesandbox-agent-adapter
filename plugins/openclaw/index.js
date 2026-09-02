import { readFile } from "node:fs/promises";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

const DEFAULT_URL = "http://127.0.0.1:18080";

async function settings(config) {
  const adapterUrl = String(config.adapterUrl || process.env.CUBE_ADAPTER_URL || DEFAULT_URL).replace(/\/$/, "");
  if (!/^https?:\/\//.test(adapterUrl)) throw new Error("Cube Adapter URL must use HTTP or HTTPS");
  const tokenEnv = String(config.tokenEnv || "CUBE_ADAPTER_TOKEN");
  let token = process.env[tokenEnv];
  if (!token && config.tokenFile) token = (await readFile(String(config.tokenFile), "utf8")).trim();
  if (!token) throw new Error(`${tokenEnv} or tokenFile is not configured`);
  return { adapterUrl, token, profile: String(config.profile || "offline-code") };
}

function sessionKey(toolContext) {
  return String(
    toolContext.sessionKey ||
      toolContext.agentId ||
      `openclaw:${toolContext.workspaceDir || "default"}`,
  );
}

async function request(config, toolContext, path, body, signal) {
  const { adapterUrl, token } = await settings(config);
  const response = await fetch(`${adapterUrl}${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`Cube Adapter ${response.status}: ${payload?.error?.message || "request failed"}`);
  }
  return payload;
}

async function acquire(config, toolContext, signal) {
  return request(
    config,
    toolContext,
    "/v1/leases/acquire",
    {
      runtime: "openclaw",
      session_key: sessionKey(toolContext),
      profile: String(config.profile || "offline-code"),
    },
    signal,
  );
}

function dynamicTool(definition, execute) {
  return {
    ...definition,
    factory: ({ config, toolContext }) => ({
      ...definition,
      execute: async (_toolCallId, params, signal) => {
        const lease = await acquire(config, toolContext, signal);
        const result = await execute(config, toolContext, lease, params, signal);
        return {
          content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
          details: result,
        };
      },
    }),
  };
}

const object = (properties, required = []) => ({
  type: "object",
  additionalProperties: false,
  properties,
  required,
});

const definitions = [
  {
    name: "cube_exec",
    label: "CubeSandbox Exec",
    description: "Execute a shell command inside this conversation's isolated CubeSandbox MicroVM.",
    parameters: object(
      {
        command: { type: "string", description: "Shell command to run inside the MicroVM." },
        cwd: { type: "string", description: "Absolute /workspace or /tmp working directory." },
        timeout_ms: { type: "integer", minimum: 1, maximum: 120000 },
      },
      ["command"],
    ),
    call: (lease, params) => [`/v1/leases/${lease.lease_ref}/exec`, params],
  },
  {
    name: "cube_status",
    label: "CubeSandbox Status",
    description: "Inspect this conversation's lease state, TTL, jobs, and checkpoint counts.",
    parameters: object({}),
    call: (lease) => [`/v1/leases/${lease.lease_ref}/status`, {}],
  },
  {
    name: "cube_read",
    label: "CubeSandbox Read",
    description: "Read a UTF-8 file from this conversation's CubeSandbox /workspace or /tmp.",
    parameters: object({ path: { type: "string" } }, ["path"]),
    call: (lease, params) => [`/v1/leases/${lease.lease_ref}/read`, params],
  },
  {
    name: "cube_write",
    label: "CubeSandbox Write",
    description: "Write a UTF-8 file inside this conversation's CubeSandbox /workspace or /tmp.",
    parameters: object(
      { path: { type: "string" }, content: { type: "string" } },
      ["path", "content"],
    ),
    call: (lease, params) => [`/v1/leases/${lease.lease_ref}/write`, params],
  },
  {
    name: "cube_list",
    label: "CubeSandbox List",
    description: "List a directory inside this conversation's CubeSandbox.",
    parameters: object({ path: { type: "string", default: "/workspace" } }),
    call: (lease, params) => [
      `/v1/leases/${lease.lease_ref}/list`,
      { path: params.path || "/workspace" },
    ],
  },
  {
    name: "cube_job_start",
    label: "CubeSandbox Job Start",
    description: "Start a durable asynchronous command and return a job reference.",
    parameters: object(
      { command: { type: "string" }, cwd: { type: "string" } },
      ["command"],
    ),
    call: (lease, params) => [`/v1/leases/${lease.lease_ref}/jobs`, params],
  },
  {
    name: "cube_job_status",
    label: "CubeSandbox Job Status",
    description: "Inspect an asynchronous CubeSandbox job.",
    parameters: object({ job_ref: { type: "string" } }, ["job_ref"]),
    call: (_lease, params) => [`/v1/jobs/${encodeURIComponent(params.job_ref)}/status`, {}],
  },
  {
    name: "cube_job_output",
    label: "CubeSandbox Job Output",
    description: "Read a page of asynchronous job output.",
    parameters: object(
      {
        job_ref: { type: "string" },
        offset: { type: "integer", minimum: 0 },
        max_bytes: { type: "integer", minimum: 1, maximum: 1048576 },
      },
      ["job_ref"],
    ),
    call: (_lease, { job_ref, ...body }) => [
      `/v1/jobs/${encodeURIComponent(job_ref)}/output`,
      body,
    ],
  },
  {
    name: "cube_job_cancel",
    label: "CubeSandbox Job Cancel",
    description: "Cancel an asynchronous job and its process group.",
    parameters: object({ job_ref: { type: "string" } }, ["job_ref"]),
    call: (_lease, params) => [`/v1/jobs/${encodeURIComponent(params.job_ref)}/cancel`, {}],
  },
  {
    name: "cube_checkpoint",
    label: "CubeSandbox Checkpoint",
    description: "Create a policy-gated microVM checkpoint.",
    parameters: object({ name: { type: "string" } }),
    call: (lease, params) => [`/v1/leases/${lease.lease_ref}/checkpoints`, params],
  },
  {
    name: "cube_rollback",
    label: "CubeSandbox Rollback",
    description: "Roll this conversation's lease back to a checkpoint.",
    parameters: object({ checkpoint_ref: { type: "string" } }, ["checkpoint_ref"]),
    call: (lease, params) => [
      `/v1/leases/${lease.lease_ref}/checkpoints/${encodeURIComponent(params.checkpoint_ref)}/rollback`,
      {},
    ],
  },
  {
    name: "cube_fork",
    label: "CubeSandbox Fork",
    description: "Fork a new isolated lease from a checkpoint.",
    parameters: object(
      { checkpoint_ref: { type: "string" }, branch: { type: "string" } },
      ["checkpoint_ref"],
    ),
    call: (lease, { checkpoint_ref, ...body }) => [
      `/v1/leases/${lease.lease_ref}/checkpoints/${encodeURIComponent(checkpoint_ref)}/fork`,
      body,
    ],
  },
  {
    name: "cube_release",
    label: "CubeSandbox Release",
    description: "Pause or destroy this conversation's CubeSandbox lease.",
    parameters: object({ action: { type: "string", enum: ["pause", "kill"] } }),
    call: (lease, params) => [
      `/v1/leases/${lease.lease_ref}/release`,
      { action: params.action || "pause" },
    ],
  },
];

export default defineToolPlugin({
  id: "cube-adapter-tools",
  name: "CubeSandbox Adapter Tools",
  description: "Run untrusted Agent work in a policy-controlled CubeSandbox MicroVM.",
  configSchema: object({
    adapterUrl: { type: "string", description: "Cube Adapter base URL." },
    tokenEnv: { type: "string", description: "Environment variable containing the bearer token." },
    tokenFile: { type: "string", description: "Read-only file containing the bearer token." },
    profile: { type: "string", description: "Platform-owned policy profile." },
  }),
  tools: (tool) =>
    definitions.map((definition) =>
      tool(
        dynamicTool(definition, (config, toolContext, lease, params, signal) => {
          const [path, body] = definition.call(lease, params);
          return request(config, toolContext, path, body, signal);
        }),
      ),
    ),
});
