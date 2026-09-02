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
  console.log("OpenClaw Cube Adapter plugin test: OK");
} finally {
  globalThis.fetch = originalFetch;
  delete process.env.TEST_CUBE_TOKEN;
}
