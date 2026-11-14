import assert from "node:assert/strict";

import plugin from "./index.js";

const calls = [];
const originalFetch = globalThis.fetch;

globalThis.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  calls.push({ url, body, authorization: options.headers.authorization });
  if (url.endsWith("/v1/leases/acquire")) {
    return {
      ok: true,
      json: async () => ({ lease_ref: "lease_0123456789abcdefabcd", sandbox_ref: "12345678" }),
    };
  }
  return {
    ok: true,
    json: async () => ({ executor: "cubesandbox-microvm", stdout: "remote-ok\n" }),
  };
};

try {
  process.env.TEST_CUBE_TOKEN = "redacted-test-token";
  const registrations = [];
  plugin.register({
    pluginConfig: {
      adapterUrl: "http://adapter.test",
      tokenEnv: "TEST_CUBE_TOKEN",
      profile: "offline-code",
    },
    registerTool: (definition, options = {}) => registrations.push({ definition, options }),
  });
  assert.deepEqual(
    registrations.map(({ definition, options }) => options.name || definition.name),
    [
      "cube_exec",
      "cube_status",
      "cube_read",
      "cube_write",
      "cube_list",
      "cube_job_start",
      "cube_job_status",
      "cube_job_output",
      "cube_job_cancel",
      "cube_checkpoint",
      "cube_rollback",
      "cube_fork",
      "cube_release",
      "cube_task_plan",
      "cube_task_submit",
      "cube_task_status",
      "cube_task_result",
      "cube_task_cancel",
      "cube_task_receipt",
      "cube_mcp_list", "cube_mcp_call", "cube_renew", "cube_profiles", "cube_doctor", "cube_recover", "cube_job_resume",
      "cube_checkpoint_list", "cube_checkpoint_delete",
    ],
  );
  const exec = registrations[0].definition({ sessionKey: "openclaw-session-42" });
  const result = await exec.execute(
    "call-1",
    { command: "printf remote-ok" },
    new AbortController().signal,
  );
  assert.equal(result.details.executor, "cubesandbox-microvm");
  assert.equal(calls[0].body.runtime, "openclaw");
  assert.equal(calls[0].body.session_key, "openclaw-session-42");
  assert.equal(calls[1].body.command, "printf remote-ok");
  assert.equal(calls[0].authorization, "Bearer redacted-test-token");
  const taskPlan = registrations[13].definition({ sessionKey: "openclaw-session-42" });
  await taskPlan.execute(
    "call-2",
    { template: "train-logistic", parameters: { epochs: 10 } },
    new AbortController().signal,
  );
  assert.ok(calls[2].url.endsWith("/v1/tasks/plan"));
  assert.equal(calls[2].body.template, "train-logistic");
  const mcpCall = registrations.find(({ options, definition }) => (options.name || definition.name) === "cube_mcp_call").definition({ sessionKey: "same-session" });
  await mcpCall.execute("call-3", { profile: "production-code", name: "echo", arguments: { text: "test" } }, new AbortController().signal);
  assert.equal(calls[3].body.profile, "production-code");
  assert.ok(calls[4].url.endsWith("/mcp-call"));
  assert.equal(calls[4].body.profile, undefined);
  const doctor = registrations.find(({ options, definition }) => (options.name || definition.name) === "cube_doctor").definition({ sessionKey: "same-session" });
  await doctor.execute("call-4", {}, new AbortController().signal);
  assert.ok(calls[5].url.endsWith("/v1/doctor"));
  assert.equal(calls.length, 6);
  const checkpoint = registrations.find(({ options, definition }) => (options.name || definition.name) === "cube_checkpoint").definition({ sessionKey: "same-session" });
  await checkpoint.execute("call-5", { name: "proof", mode: "fs-only" }, new AbortController().signal);
  assert.equal(calls[7].body.mode, "fs-only");
  const checkpointList = registrations.find(({ options, definition }) => (options.name || definition.name) === "cube_checkpoint_list").definition({ sessionKey: "same-session" });
  await checkpointList.execute("call-6", {}, new AbortController().signal);
  assert.ok(calls[9].url.endsWith("/checkpoints/list"));
  const checkpointDelete = registrations.find(({ options, definition }) => (options.name || definition.name) === "cube_checkpoint_delete").definition({ sessionKey: "same-session" });
  await checkpointDelete.execute("call-7", { checkpoint_ref: "checkpoint_test" }, new AbortController().signal);
  assert.ok(calls[11].url.endsWith("/checkpoints/checkpoint_test/delete"));
  console.log("OpenClaw Cube Adapter plugin test: OK");
} finally {
  globalThis.fetch = originalFetch;
  delete process.env.TEST_CUBE_TOKEN;
}
