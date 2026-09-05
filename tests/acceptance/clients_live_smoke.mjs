import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import openclawPlugin from "../../plugins/openclaw/index.js";
import { apply as applyDsh } from "../../plugins/dsh/index.js";

const adapterUrl = process.env.CUBE_ADAPTER_URL || "http://127.0.0.1:19080";
const tokenEnv = "CUBE_ACCEPT_AGENT_TOKEN";
assert.ok(process.env[tokenEnv], `${tokenEnv} is required`);

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function runFlow(name, invoke) {
  const suffix = `${name}-${Date.now().toString(36)}`.toLowerCase();
  const plan = await invoke("cube_task_plan", {
    template: "trusted-auto",
    parameters: { message: `safe-${suffix}` },
  });
  assert.equal(plan.state, "ready");
  const submitted = await invoke("cube_task_submit", { plan_ref: plan.plan_ref });
  let status = submitted;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    status = await invoke("cube_task_status", { task_ref: submitted.task_ref });
    if (!["starting", "running"].includes(status.state)) break;
    await delay(250);
  }
  assert.equal(status.state, "succeeded");
  const result = await invoke("cube_task_result", { task_ref: submitted.task_ref });
  assert.equal(result.state, "succeeded");
  assert.equal(result.result.cleanup, "verified");
  const receipt = await invoke("cube_task_receipt", { task_ref: submitted.task_ref });
  assert.equal(receipt.receipt.signature.alg, "HS256");
  return {
    client: name,
    state: result.state,
    cleanup: result.result.cleanup,
    receipt_alg: receipt.receipt.signature.alg,
    task_ref: submitted.task_ref,
  };
}

const openclawRegistrations = [];
openclawPlugin.register({
  pluginConfig: { adapterUrl, tokenEnv, profile: "trusted-acceptance" },
  registerTool: (definition, options = {}) =>
    openclawRegistrations.push({ definition, options }),
});
assert.equal(openclawRegistrations.length, 19);
const openclawTools = new Map(
  openclawRegistrations.map(({ definition, options }) => [
    options.name || definition.name,
    definition({ sessionKey: "trusted-acceptance-openclaw" }),
  ]),
);
const openclaw = await runFlow("openclaw", async (name, params) => {
  const result = await openclawTools
    .get(name)
    .execute(`acceptance-${name}`, params, new AbortController().signal);
  return result.details;
});

const dshTools = [];
const disposeDsh = applyDsh(
  {
    systemPrompt: { section: () => {} },
    tools: {
      register: (definition) => {
        dshTools.push(definition);
        return () => {};
      },
    },
  },
  { adapterUrl, tokenEnv, profile: "trusted-acceptance" },
);
assert.equal(dshTools.length, 19);
const dshToolMap = new Map(dshTools.map((tool) => [tool.name, tool]));
const dsh = await runFlow("dsh", (name, params) =>
  dshToolMap.get(name).execute(params, {
    agent: { id: "trusted-acceptance-dsh" },
    rootCallId: `acceptance-${name}`,
    signal: new AbortController().signal,
  }),
);
disposeDsh();

const result = {
  result: "PASS",
  clients: [openclaw, dsh],
  tools_per_client: 19,
  trusted_task_tools_per_client: 6,
};
if (process.env.CUBE_ACCEPT_CLIENT_OUTPUT) {
  await mkdir(dirname(process.env.CUBE_ACCEPT_CLIENT_OUTPUT), { recursive: true });
  await writeFile(process.env.CUBE_ACCEPT_CLIENT_OUTPUT, `${JSON.stringify(result, null, 2)}\n`);
}
console.log(JSON.stringify(result, null, 2));
