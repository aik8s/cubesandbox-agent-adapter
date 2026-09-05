import { readFile } from "node:fs/promises";

export const name = "cube-adapter-tools";
export const inject = ["tools", "systemPrompt"];

async function settings(config) {
  const adapterUrl = String(config.adapterUrl || process.env.CUBE_ADAPTER_URL || "http://127.0.0.1:18080").replace(/\/$/, "");
  if (!/^https?:\/\//.test(adapterUrl)) throw new Error("Cube Adapter URL must use HTTP or HTTPS");
  const tokenEnv = String(config.tokenEnv || "CUBE_ADAPTER_TOKEN");
  let token = process.env[tokenEnv];
  if (!token && config.tokenFile) token = (await readFile(String(config.tokenFile), "utf8")).trim();
  if (!token) throw new Error(`${tokenEnv} or tokenFile is not configured`);
  return { adapterUrl, token, profile: String(config.profile || "offline-code") };
}

async function request(config, path, body, signal) {
  const { adapterUrl, token } = await settings(config);
  const response = await fetch(`${adapterUrl}${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`Cube Adapter ${response.status}: ${payload?.error?.message || "request failed"}`);
  return payload;
}

function sessionKey(exec) {
  return String(exec.agent?.id || exec.rootCallId || "dsh-agentless");
}

async function acquire(config, exec) {
  return request(
    config,
    "/v1/leases/acquire",
    {
      runtime: "dsh",
      session_key: sessionKey(exec),
      profile: String(config.profile || "offline-code"),
    },
    exec.signal,
  );
}

const output = {
  schema: { description: "Redacted Cube Adapter JSON response." },
  render: (_args, value) => [{ type: "text", text: JSON.stringify(value, null, 2) }],
};

const object = (properties, required = []) => ({
  type: "object",
  additionalProperties: false,
  properties,
  required,
});

function tool(name, description, parameters, run, requiresLease = true) {
  return {
    name,
    description,
    parameters,
    output,
    timeoutMs: 125000,
    async execute(args, exec) {
      const lease = requiresLease ? await acquire(this?.config || {}, exec) : null;
      return run(lease, args, exec);
    },
  };
}

export function apply(ctx, config = {}) {
  ctx.systemPrompt.section({
    name: "tool:cube-adapter",
    order: 104,
    text: "Use cube_task_* for approved trusted tasks. Use the other cube_* tools only when the identity allows general execution. Approval always uses a separate production identity.",
  });

  const register = (definition) => {
    const bound = { ...definition, execute: definition.execute.bind({ config }) };
    return ctx.tools.register(bound);
  };
  const definitions = [
    [
      "cube_exec",
      "Execute a shell command inside this DSH session's isolated CubeSandbox MicroVM.",
      object(
        {
          command: { type: "string" },
          cwd: { type: "string" },
          timeout_ms: { type: "integer", minimum: 1 },
        },
        ["command"],
      ),
      (lease, args) => [`/v1/leases/${lease.lease_ref}/exec`, args],
    ],
    [
      "cube_status",
      "Inspect this DSH session's CubeSandbox lease state and TTL.",
      object({}),
      (lease) => [`/v1/leases/${lease.lease_ref}/status`, {}],
    ],
    [
      "cube_read",
      "Read a UTF-8 file from this DSH session's CubeSandbox.",
      object({ path: { type: "string" } }, ["path"]),
      (lease, args) => [`/v1/leases/${lease.lease_ref}/read`, args],
    ],
    [
      "cube_write",
      "Write a UTF-8 file inside this DSH session's CubeSandbox.",
      object({ path: { type: "string" }, content: { type: "string" } }, ["path", "content"]),
      (lease, args) => [`/v1/leases/${lease.lease_ref}/write`, args],
    ],
    [
      "cube_list",
      "List a directory inside this DSH session's CubeSandbox.",
      object({ path: { type: "string" } }),
      (lease, args) => [`/v1/leases/${lease.lease_ref}/list`, { path: args.path || "/workspace" }],
    ],
    [
      "cube_job_start",
      "Start a durable asynchronous command.",
      object({ command: { type: "string" }, cwd: { type: "string" } }, ["command"]),
      (lease, args) => [`/v1/leases/${lease.lease_ref}/jobs`, args],
    ],
    [
      "cube_job_status",
      "Inspect an asynchronous CubeSandbox job.",
      object({ job_ref: { type: "string" } }, ["job_ref"]),
      (_lease, args) => [`/v1/jobs/${encodeURIComponent(args.job_ref)}/status`, {}],
    ],
    [
      "cube_job_output",
      "Read a page of asynchronous job output.",
      object(
        {
          job_ref: { type: "string" },
          offset: { type: "integer", minimum: 0 },
          max_bytes: { type: "integer", minimum: 1, maximum: 1048576 },
        },
        ["job_ref"],
      ),
      (_lease, { job_ref, ...body }) => [`/v1/jobs/${encodeURIComponent(job_ref)}/output`, body],
    ],
    [
      "cube_job_cancel",
      "Cancel an asynchronous CubeSandbox job.",
      object({ job_ref: { type: "string" } }, ["job_ref"]),
      (_lease, args) => [`/v1/jobs/${encodeURIComponent(args.job_ref)}/cancel`, {}],
    ],
    [
      "cube_checkpoint",
      "Create a policy-gated CubeSandbox checkpoint.",
      object({ name: { type: "string" } }),
      (lease, args) => [`/v1/leases/${lease.lease_ref}/checkpoints`, args],
    ],
    [
      "cube_rollback",
      "Roll this DSH lease back to a checkpoint.",
      object({ checkpoint_ref: { type: "string" } }, ["checkpoint_ref"]),
      (lease, args) => [
        `/v1/leases/${lease.lease_ref}/checkpoints/${encodeURIComponent(args.checkpoint_ref)}/rollback`,
        {},
      ],
    ],
    [
      "cube_fork",
      "Fork a new lease from a checkpoint.",
      object({ checkpoint_ref: { type: "string" }, branch: { type: "string" } }, ["checkpoint_ref"]),
      (lease, { checkpoint_ref, ...body }) => [
        `/v1/leases/${lease.lease_ref}/checkpoints/${encodeURIComponent(checkpoint_ref)}/fork`,
        body,
      ],
    ],
    [
      "cube_release",
      "Pause or destroy this DSH session's CubeSandbox lease.",
      object({ action: { type: "string", enum: ["pause", "kill"] } }),
      (lease, args) => [
        `/v1/leases/${lease.lease_ref}/release`,
        { action: args.action || "pause" },
      ],
    ],
    [
      "cube_task_plan",
      "Plan a server-owned, schema-validated trusted task without executing it.",
      object({ template: { type: "string" }, parameters: { type: "object" } }, ["template", "parameters"]),
      (_lease, { template, parameters }) => ["/v1/tasks/plan", { template, parameters }],
      false,
    ],
    [
      "cube_task_submit",
      "Submit an independently approved trusted task plan.",
      object({ plan_ref: { type: "string" } }, ["plan_ref"]),
      (_lease, { plan_ref }) => [`/v1/task-plans/${encodeURIComponent(plan_ref)}/submit`, {}],
      false,
    ],
    [
      "cube_task_status",
      "Read trusted task state without raw process output.",
      object({ task_ref: { type: "string" } }, ["task_ref"]),
      (_lease, { task_ref }) => [`/v1/tasks/${encodeURIComponent(task_ref)}/status`, {}],
      false,
    ],
    [
      "cube_task_result",
      "Validate allowlisted outputs, destroy the MicroVM, and return a receipt.",
      object({ task_ref: { type: "string" } }, ["task_ref"]),
      (_lease, { task_ref }) => [`/v1/tasks/${encodeURIComponent(task_ref)}/result`, {}],
      false,
    ],
    [
      "cube_task_cancel",
      "Cancel and finalize a trusted task.",
      object({ task_ref: { type: "string" } }, ["task_ref"]),
      (_lease, { task_ref }) => [`/v1/tasks/${encodeURIComponent(task_ref)}/cancel`, {}],
      false,
    ],
    [
      "cube_task_receipt",
      "Return the signed receipt for a finalized trusted task.",
      object({ task_ref: { type: "string" } }, ["task_ref"]),
      (_lease, { task_ref }) => [`/v1/tasks/${encodeURIComponent(task_ref)}/receipt`, {}],
      false,
    ],
  ];
  const disposers = definitions.map(([toolName, description, parameters, endpoint, requiresLease]) =>
    register(
      tool(toolName, description, parameters, async (lease, args, exec) => {
        const [path, body] = endpoint(lease, args);
        return request(config, path, body, exec.signal);
      }, requiresLease !== false),
    ),
  );
  return () => disposers.reverse().forEach((dispose) => dispose());
}
