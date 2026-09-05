# Trusted-execution examples

These examples let a local MCP-capable code agent start bounded training and
data-cleaning jobs in production-side CubeSandbox MicroVMs without moving the
production dataset or its credentials to the workstation.

They are reference building blocks, not a production policy by themselves:

- [`profiles.yaml`](profiles.yaml) defines two operator-owned profiles;
- [`task-templates.yaml`](task-templates.yaml) defines closed parameter schemas,
  fixed commands, approval gates, and allowlisted outputs;
- [`mcp-host.example.json`](mcp-host.example.json) binds each local MCP server
  entry to one fixed profile;
- [`prompts/`](prompts/) contains English and Chinese task prompts for a local
  code agent;
- [`tasks/`](tasks/) contains deterministic Python workloads with no third-party
  dependencies;
- [`fixtures/`](fixtures/) contains synthetic, non-sensitive validation data.

## Template contract

Create two CubeSandbox templates named `trusted-training` and
`trusted-data-cleaning`. Each should provide:

| Path/capability | Training | Data cleaning |
| --- | --- | --- |
| `/workspace` | Adapter-managed disposable volume | Adapter-managed disposable volume |
| `/datasets/training` | Approved dataset, read-only | Not required |
| `/datasets/cleaning` | Not required | Approved dataset, read-only |
| Public internet | Disabled | Disabled |
| Runtime identity | Optional narrow training submitter/artifact writer | Optional narrow data-output writer |

Bake the corresponding script from [`tasks/`](tasks/) into the immutable image
under `/opt/cube-tasks/`. Stage an approved input at `/workspace/input` through
the platform-owned template, read-only volume, or narrow internal data client;
do not let the Agent upload production input or replace the workload script.

For a local smoke test, copy the synthetic fixture into the corresponding
dataset path in a disposable template. Never use local test transfer steps for
real production data.

Load [`profiles.yaml`](profiles.yaml) as `CUBE_ADAPTER_PROFILES_FILE` and
[`task-templates.yaml`](task-templates.yaml) as
`CUBE_ADAPTER_TASK_TEMPLATES_FILE`. Grant the local Agent a task-only principal
and give a different production identity role `approver`; see
[`config/token-principals.example.json`](../../config/token-principals.example.json).
Then adapt the paths and certificates in
[`mcp-host.example.json`](mcp-host.example.json).

The task-only token must not include `exec:run`, `job:*`, `file:*`,
`artifact:*`, `pty:*`, or `checkpoint:*`. Even though the generic MCP facade
advertises those tools, the HTTP boundary rejects them. `allowed_task_templates`
prevents the model from selecting an unapproved named task.

## Output constraints

The Adapter reads only paths declared in the server-side template. Content is
returned only for `expose: content`; digest-only outputs return evidence without
the file body. A successful finalization validates JSON output schemas, kills
the MicroVM, and signs an Execution Receipt. Large datasets, checkpoints, or
weights should be written from inside the MicroVM to an approved destination
using a short-lived, scoped workload identity.

The approver uses the HTTP endpoint (not the Agent MCP server):

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  https://adapter.example.internal/v1/task-plans/plan_xxx

curl --fail-with-body -X POST \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H 'Content-Type: application/json' \
  https://adapter.example.internal/v1/task-plans/plan_xxx/approve \
  -d '{"decision":"approve"}'
```

Verify a saved receipt without sending it back to the Adapter:

```bash
../../scripts/verify_receipt.py --key-file /run/secrets/receipt-hmac-key receipt.json
```

See the full [Chinese security guide](../../docs/trusted-execution.zh-CN.md).
