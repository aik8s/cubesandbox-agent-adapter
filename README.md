# cubesandbox-agent-adapter

[English](README.md) | [简体中文](README.zh-CN.md)

[Project website](https://aik8s.github.io/cubesandbox-agent-adapter/) · [Website publishing](docs/website.md)

Community integration that routes OpenClaw, DeepSeek Harness (DSH), Hermes
Agent and MCP hosts such as Codex into policy-controlled
[CubeSandbox](https://github.com/TencentCloud/CubeSandbox) MicroVMs.

```text
OpenClaw Tool Plugin ──────────┐
DSH Cordis Plugin ─────────────┤
Hermes Tool Plugin ────────────┼─ authenticated HTTP ─→ Adapter ─→ Cube SDK ─→ MicroVM
Codex / MCP host ─→ MCP stdio ─┘                              │
                                                            └─ redacted JSONL audit
```

The Adapter is the only component that holds Cube connection settings, full
Sandbox IDs and traffic tokens. Runtime plugins receive opaque leases and
return only short Sandbox references to the model.

> **Project status:** v0.4.0 is a production-oriented reference implementation.
> It supports durable encrypted leases, multi-tenant policy, and approved
> trusted-task workflows, but still requires deployment-specific hardening and
> acceptance tests in every target CubeSandbox environment.

## Start here

Follow this order: **prepare CubeSandbox → deploy the Adapter → connect one
Agent → execute and release a sandbox**. The installers set up the Adapter or
client plugins; CubeSandbox, a READY sandbox template, and the Agent application
must already exist. Configure the model provider in the Agent; the Adapter does
not need a model API key.

| Your starting point | Deployment path |
| --- | --- |
| CubeSandbox is available; use a workstation or single server | [Docker Compose guide](docs/deploy-docker.md): published image, no Kubernetes required |
| Kubernetes and CubeSandbox are available | [Kubernetes installation](#quick-start-kubernetes-adapter): requires kubectl, Helm, and cluster access |
| No CubeSandbox backend yet | Follow the CubeSandbox deployment links below, prepare a READY template, and verify sandbox creation before installing the Adapter |
| Basic execution works; add training, cleaning, and approval | [Trusted execution](docs/trusted-execution.md) and [task examples](examples/trusted-execution/): additional configuration is required |

Both deployment paths need a CubeAPI URL, a CubeProxy host/port, and a READY
template name reachable/usable from the Adapter. Configure CubeAPI credentials
if backend authentication is enabled.

## CubeSandbox and Kubernetes

CubeSandbox is the sandbox execution system; Kubernetes is an optional platform
for deploying and operating that system. CubeSandbox does **not** require
Kubernetes: its official [one-click and bare-metal deployment](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/bare-metal-deploy.md)
runs the control and compute services on Linux under systemd, with supporting
containers where needed.

Kubernetes becomes valuable when a deployment has multiple control or compute
nodes and needs standard scheduling, declarative configuration, health probes,
rolling operations, Secret delivery, NetworkPolicy and observability. The
official [Kubernetes deployment](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/kubernetes/index.md)
uses Deployments for control-plane services and DaemonSets for compute nodes.
It does not remove the underlying virtualization, kernel, storage and network
requirements, and upstream still documents compute-node upgrade limitations.

This repository follows the same separation: the Adapter can run directly or
with Compose against any reachable CubeAPI/CubeProxy. Its Helm chart adds
Kubernetes lifecycle, policy, Secret and HA integration. Kubernetes is an
operations choice, not an SDK dependency.

## Controlled execution from local agents into production

A common enterprise pattern keeps the developer's preferred code agent on an
office-network workstation while datasets, GPUs, internal APIs, and production
databases remain isolated in the production network. Deploying the Adapter on
the production side provides one authenticated, policy-controlled entry point;
generated code runs in a CubeSandbox MicroVM instead of on the laptop or a
production node.

```text
local code agent -> production gateway -> Adapter -> named task + schema
                                      -> independent approval -> fixed profile
                                                              -> MicroVM
```

Production credentials, full Sandbox IDs, mounted data, and workload identity
stay on the production side. Named tasks enforce a closed JSON Schema, fixed
command arguments, profile, approval gate, output allowlist, cleanup, and a
signed Execution Receipt. Action scopes let the Agent use `task:*` without
access to raw `exec`, job, file, artifact, PTY, or checkpoint endpoints.
“Trusted execution” here is a controlled, isolated, and auditable execution
plane, not a hardware-attested confidential-computing TEE.

Runnable examples cover a local agent starting an asynchronous training job and
cleaning read-only production data. They include fixed-profile MCP entries,
prompts, dependency-free task scripts, and synthetic fixtures:

- [Trusted-execution design and security boundaries](docs/trusted-execution.md)
- [Training and data-cleaning templates](examples/trusted-execution/)

## Hands-on evidence

The following screenshots come from real OpenClaw, DSH, Hermes Agent and Codex
sessions connected to a Kubernetes-hosted CubeSandbox deployment. They are
evidence from a functional lab run, not a benchmark or a production-readiness
claim.

OpenClaw called only the Adapter's `cube_exec` and `cube_release` tools. The
result identifies `cubesandbox-microvm` as the executor and returns only the
short Sandbox reference `45a28df5`:

![OpenClaw calling CubeSandbox Adapter tools](docs/assets/readme/openclaw-direct-result.jpg)

DSH performed the same path through its Cordis Plugin. Its trace records the
`cube_exec` and `cube_release` calls and the short reference `f795f7fc`:

![DSH CubeSandbox tool trace](docs/assets/readme/dsh-direct-trace.jpg)

While that DSH command was running, CubeSandbox WebUI showed the matching
`f795f7...` MicroVM as `running`:

![A running MicroVM in CubeSandbox WebUI](docs/assets/readme/cubesandbox-live-sandbox.jpg)

The Adapter audit page correlates OpenClaw and DSH actions by runtime, request
ID and short Sandbox reference while omitting commands, output, tokens and full
Sandbox IDs:

![Redacted Cube Adapter audit events](docs/assets/readme/adapter-audit.jpg)

### Hermes Agent native plugin

The integration is a standalone Hermes native Tool Plugin. The official Hermes
0.20.6 dashboard shows `cube-adapter-tools` as a user plugin with no built-in
tool override and with status `enabled`:

![Hermes Agent CubeSandbox plugin enabled](docs/assets/readme/hermes-plugin-enabled.jpg)

The tested Hermes session contains eight messages and three tool calls. Hermes'
default tool compression may present deferred plugin tools through
`tool_describe` and `tool_call`; the calls still resolve to the plugin's
`cube_exec` and `cube_release` handlers:

![Hermes Agent CubeSandbox session tool history](docs/assets/readme/hermes-session-tools.jpg)

During the 60-second command, CubeSandbox WebUI showed the matching
`3b287c...` MicroVM as `running`:

![Hermes-triggered MicroVM running in CubeSandbox](docs/assets/readme/hermes-cubesandbox-live.jpg)

After completion, the redacted audit page records the same `3b287c8f` short
reference across `acquire`, `exec` and `release`, with runtime `hermes`, an
`ok` outcome and zero active leases:

![Hermes CubeSandbox acquire exec release audit trail](docs/assets/readme/hermes-adapter-audit.jpg)

### v0.3.0 four-client application acceptance

On 2026-09-02, the v0.3.0 Adapter was deployed to a Kubernetes acceptance
environment and exercised from the actual OpenClaw, DSH, Hermes Agent and Codex
applications. These are light-theme product screenshots, not reconstructed
evidence cards. The Adapter deployment was `1/1` ready with zero container
restarts; every run ended with `cube_release(action=kill)`, and the Adapter
reported zero active leases afterward.

| Client | Verified path | Result |
| --- | --- | --- |
| OpenClaw | Tool Plugin → Adapter → CubeSandbox | `cube_exec`, status and release succeeded |
| DSH | Cordis Plugin → Adapter → CubeSandbox | `cube_exec`, status and release succeeded |
| Hermes Agent | Native Tool Plugin → Adapter → CubeSandbox | the four surfaced core tools completed execution and release; `cube_exec` supplied the status probe |
| Codex | MCP stdio facade → Adapter → CubeSandbox | acquire, exec, status and release succeeded |

OpenClaw executed `OPENCLAW_APP_CUBESANDBOX_OK`, inspected the lease and destroyed
the MicroVM from its real Control UI:

![OpenClaw application using CubeSandbox in light mode](docs/assets/v0.3-acceptance/10-openclaw-application.jpg)

DSH executed `DSH_APP_CUBESANDBOX_OK` through the Cordis Plugin and completed the
same status and cleanup path from DSH Web:

![DSH application using CubeSandbox in light mode](docs/assets/v0.3-acceptance/11-dsh-application.png)

Hermes Agent executed `HERMES_APP_CUBESANDBOX_OK` from its official dashboard.
The isolated installation used for this screenshot surfaced the four core tools
`cube_exec`, `cube_read`, `cube_write` and `cube_release`, so the model used a
second `cube_exec` as its status probe. The current source declares the
19-tool plugin surface, but this image proves only the captured execution and
release path rather than every Hermes tool:

![Hermes Agent application using CubeSandbox in light mode](docs/assets/v0.3-acceptance/12-hermes-application.png)

Codex used the Adapter's stdio MCP facade with only `cube_acquire`, `cube_exec`,
`cube_status` and `cube_release` enabled. It executed
`CODEX_APP_CUBESANDBOX_OK`, observed the running lease and destroyed it:

![Codex application using CubeSandbox through MCP in light mode](docs/assets/v0.3-acceptance/13-codex-application.png)

#### Trusted-task feature through four real clients

The newer policy-controlled flow was also exercised on 2026-09-04 from all four
client applications. Each run used only `cube_task_plan` → `cube_task_submit` →
`cube_task_status` → `cube_task_result` → `cube_task_receipt` and finished with
a `succeeded` task, verified MicroVM cleanup, and an HS256 receipt.

![OpenClaw trusted-task run in its native light UI](docs/assets/trusted-execution-apps/01-openclaw-trusted-task.jpg)

![DSH trusted-task run in its native light UI](docs/assets/trusted-execution-apps/02-dsh-trusted-task.jpg)

![Codex trusted-task run through MCP in its native light TUI](docs/assets/trusted-execution-apps/03-codex-trusted-task.png)

![Hermes trusted-task run in its native light Dashboard](docs/assets/trusted-execution-apps/04-hermes-trusted-task.jpg)

In the Hermes capture, “6 tools” means one `tool_describe` discovery call plus
the five trusted-task calls. Detailed acceptance scope and backend evidence are
in the [trusted-execution guide](docs/trusted-execution.md).

The screenshots expose no bearer token, gateway address, full Sandbox ID or
private cluster identifier. Complete Plan/Task/Sandbox identifiers and the raw
Hermes signed-receipt payload are also visibly redacted. Model-provider
credentials remained environment references and were not written into the
repository.

For the full environment, commands, limitations and evidence, read the
following Chinese articles on [aik8s.run](https://aik8s.run/):

- [CubeSandbox Kubernetes deployment requirements and production assessment](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-kubernetes/)
- [CubeSandbox Kubernetes hands-on deployment](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-kubernetes-practice/)
- [CubeSandbox with OpenClaw and DSH: enterprise execution-plane practice](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-openclaw-dsh-enterprise-practice/)
- [Agent Sandbox selection and architecture analysis](https://aik8s.run/ai-k8s/rag-agent/agent-sandbox-selection/)

## What is included

- authenticated Python Adapter using `cubesandbox==0.7.0`;
- fail-closed declarative profiles with persistent-volume and checkpoint gates;
- OpenClaw, DSH and Hermes plugins with 19 compatible execution, file, async
  job, checkpoint, and trusted-task tools, plus an MCP facade for Codex;
- DSH Cordis Plugin and a patch that disables common host Shell/FS tools;
- Hermes Agent native Tool Plugin with official Plugin Doctor validation;
- one-command installers for Kubernetes, OpenClaw, DSH and Hermes Agent;
- Docker Compose for local development;
- Helm chart, plain Kubernetes manifest, tests and release workflows;
- Redis-backed encrypted recovery and multi-replica distributed locking;
- per-tenant bearer, OIDC and TLS/mTLS authentication;
- Prometheus metrics, dependency-aware readiness and pluggable audit sinks;
- an official-SDK MCP stdio facade;
- append-only, redacted JSONL audit events;
- server-enforced training/data-cleaning TaskTemplates with JSON Schema,
  action scopes, independent approval, output policy, cleanup, and signed
  Execution Receipts.

The current release and issue assessment is tracked in
[CubeSandbox upstream status](docs/cubesandbox-upstream.md).

## Prerequisites

Before installation, verify that:

1. CubeSandbox is running and a template alias such as `agent-code` is READY;
2. the Adapter can reach CubeAPI and CubeProxy;
3. the target Runtime can reach the Adapter;
4. `kubectl` and `helm` are installed for Kubernetes deployment;
5. `openclaw`, `dsh` or `hermes` is installed for the corresponding plugin.
6. local Python development and the MCP facade use Python 3.10 or newer.

The installer never installs CubeSandbox itself.

The Hermes path was tested with Hermes Agent 0.20.6 on macOS Apple Silicon and
a CubeSandbox 0.7.0 Kubernetes lab cluster.

## Quick start: Kubernetes Adapter

Clone the repository:

```bash
git clone --branch v0.4.0 --depth 1 https://github.com/aik8s/cubesandbox-agent-adapter.git
cd cubesandbox-agent-adapter
```

Deploy the Adapter in one command:

```bash
./scripts/install.sh adapter \
  --context <kube-context> \
  --cube-api-url http://cube-api.cube-system.svc:3000 \
  --cube-proxy-host cube-proxy.cube-system.svc \
  --cube-proxy-port 80 \
  --template agent-code
```

The command:

- creates `agent-runtime` if needed;
- creates `cube-adapter-auth` with independent random bearer and HMAC keys if
  the Secret does not already exist;
- installs the Helm chart with one Adapter replica;
- waits for the Deployment to become ready;
- never prints either secret.

Override the image, namespace or Secret when needed:

```bash
./scripts/install.sh adapter \
  --namespace my-agent-runtime \
  --release cube-adapter \
  --secret existing-adapter-secret \
  --image ghcr.io/aik8s/cubesandbox-agent-adapter:v0.4.0 \
  --cube-api-url https://cube-api.example.internal \
  --cube-api-port 443 \
  --cube-proxy-host cube-proxy.example.internal \
  --cube-proxy-port 443 \
  --template agent-code
```

The default NetworkPolicy accepts clients with this Pod label in the same
namespace:

```yaml
cubesandbox-agent-adapter-client: "true"
```

If CubeAPI or CubeProxy is outside `cube-system`, adapt the chart's egress
policy before installing. `--disable-network-policy` is intended only when an
equivalent policy is managed elsewhere.

## Quick start: OpenClaw

For a local OpenClaw using a port-forwarded Adapter:

```bash
kubectl -n agent-runtime port-forward \
  service/cube-agent-adapter-cubesandbox-agent-adapter 18080:18080
```

In another terminal, one command installs the plugin, exports the existing
bearer token to a mode-0600 file, merges the plugin/tool allowlists and validates
the OpenClaw configuration:

```bash
./scripts/install.sh openclaw \
  --adapter-url http://127.0.0.1:18080 \
  --namespace agent-runtime \
  --token-from-secret cube-adapter-auth
```

Restart the OpenClaw Gateway afterward. The installer merges rather than
replaces existing `plugins.allow` and `tools.alsoAllow` values.

When OpenClaw runs in Kubernetes, mount the same Secret into the Runtime as a
read-only file and use its in-container path:

```bash
./scripts/install.sh openclaw \
  --adapter-url http://cube-agent-adapter-cubesandbox-agent-adapter.agent-runtime.svc:18080 \
  --token-file /var/run/secrets/cube-adapter/token
```

For untrusted profiles, deny host `exec/read/write` tools separately. Loading
the plugin does not automatically turn OpenClaw's other execution backends off.

## Quick start: DSH

Install the Cordis Plugin and generate a mode-0600 patch in one command:

```bash
./scripts/install.sh dsh \
  --adapter-url http://127.0.0.1:18080 \
  --namespace agent-runtime \
  --token-from-secret cube-adapter-auth \
  --profile web
```

The generated patch disables `tool-bash`, `tool-pwsh`, `tool-fs`,
`tool-fs-search` and `tool-str-replace-editor`, then registers the Cube tool
suite. Start DSH with the command printed by the installer, or install and start
in one command:

```bash
./scripts/install.sh dsh \
  --adapter-url http://127.0.0.1:18080 \
  --token-from-secret cube-adapter-auth \
  --start
```

DSH's local `file:` installation copies the package into its plugin store.
Re-run the install command after changing the plugin source; restarting DSH
alone does not refresh a stale installed copy.

## Quick start: Hermes Agent

Install and configure the standalone native plugin in one command:

```bash
./scripts/install.sh hermes \
  --adapter-url http://127.0.0.1:18080 \
  --namespace agent-runtime \
  --token-from-secret cube-adapter-auth
```

The installer downloads the plugin from this repository, enables it while
explicitly denying built-in tool overrides, writes the Adapter URL, token-file
path and `offline-code` profile to Hermes config, then runs the official Plugin
Doctor in CI mode. Start a new restricted session afterward:

```bash
hermes -t cube-adapter
```

The plugin registers all 19 general execution and trusted-task tools.
With Hermes tool compression enabled, these tools may initially appear through
the built-in `tool_describe` / `tool_call` catalog instead of being copied into
the model prompt directly; this is expected and was covered by the real test.

Loading the plugin does not globally disable Hermes' host terminal or file
tools. Use the `cube-adapter` toolset for untrusted work and enforce the same
restriction in the profile or gateway policy used by production sessions.

## Docker deployment and local development

For a first installation, follow the [Docker Compose deployment guide](docs/deploy-docker.md).
It uses the v0.4.0 published image and covers credentials, network addresses,
health checks, real sandbox acceptance, client integration, and upgrades.
Docker runs the Adapter; an existing CubeSandbox backend is still required.

After configuring `.env` as described in the guide, start the published image:

```bash
docker compose pull adapter
docker compose up -d --no-build adapter
```

The development flow below rebuilds an image after source changes. A first
installation does not require a local build.

Start the Adapter with generated secrets and a local Docker image:

```bash
./scripts/dev-up.sh \
  --cube-api-url http://host.docker.internal:13000 \
  --cube-proxy-host host.docker.internal \
  --cube-proxy-port 13080 \
  --template agent-code

curl -fsS http://127.0.0.1:18080/healthz
```

The script creates a mode-0600 `.env`, refuses to overwrite it by default and
binds the Adapter only to `127.0.0.1`. Use `--force` only when intentionally
rotating both development secrets. Compose stores audit data in a named volume.
Add `--profile ha` and configure Redis state variables in `.env` when testing
restart recovery.

For manual Docker setup:

```bash
cp .env.example .env
# Replace both secret placeholders and edit the Cube endpoints.
chmod 600 .env
docker compose up -d --build
```

## API contract

The API includes health/readiness/metrics, tenant-scoped leases, typed file and
binary artifact operations, synchronous execution, durable jobs with SSE and
cancellation, interactive PTYs, persistent workspaces, gated checkpoints, and
approved trusted tasks (`plan -> approve -> submit -> result/receipt`). See the
complete [OpenAPI contract](docs/openapi.yaml).

Every `POST` requires `Authorization: Bearer …`. `acquire` is idempotent per
`(runtime, HMAC-SHA-256(session_key))`. The HMAC key is deliberately independent
from the bearer token so routine bearer rotation does not change pseudonymous
session correlation.

The model cannot choose a template, CIDR, public-traffic setting or lifecycle
policy. Paths are restricted to `/workspace` and `/tmp`; request, command, file,
output and timeout sizes are bounded.

## MCP stdio facade

The MCP process is intentionally a local stdio client of the authenticated
Adapter API. A typical MCP host configuration is:

```json
{
  "mcpServers": {
    "cubesandbox": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "adapter.mcp_server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/cubesandbox-agent-adapter",
        "CUBE_ADAPTER_URL": "https://adapter.example.internal",
        "CUBE_ADAPTER_PROFILE": "offline-code",
        "CUBE_ADAPTER_TOKEN_FILE": "/run/secrets/cube-adapter/token",
        "CUBE_ADAPTER_CA_FILE": "/run/secrets/cube-adapter/ca.crt"
      }
    }
  }
}
```

Plain HTTP is accepted only for loopback Adapter URLs, preventing accidental
remote bearer-token disclosure. For an mTLS-only Adapter, omit the token
variables and set `CUBE_ADAPTER_CLIENT_CERT_FILE` plus
`CUBE_ADAPTER_CLIENT_KEY_FILE`; `CUBE_ADAPTER_CA_FILE` continues to verify the
server certificate. The host-owned `CUBE_ADAPTER_PROFILE` is deliberately not
exposed as a model-selected tool argument.

The facade also exposes `cube_task_plan`, `cube_task_submit`,
`cube_task_status`, `cube_task_result`, `cube_task_cancel`, and
`cube_task_receipt`. Approval is intentionally not an Agent MCP tool; a
separate `approver` identity uses the authenticated HTTP endpoint.

## Audit

Audit rows contain runtime, keyed session digest, policy, action, request ID,
short Sandbox reference, duration, outcome and command/path digests. They omit:

- bearer and traffic tokens;
- raw session keys;
- full Sandbox IDs;
- command text and file contents;
- stdout and stderr.

The optional `/audit` HTML page is disabled by default and is only a demo. Send
JSONL events to a durable, access-controlled pipeline in real deployments.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `CUBE_ADAPTER_TOKEN` | one auth method | shared Runtime-to-Adapter bearer token |
| `CUBE_ADAPTER_TOKENS_FILE` | one auth method | per-tenant bearer principals JSON |
| `CUBE_ADAPTER_OIDC_JWKS_URL` | one auth method | OIDC JWKS endpoint |
| `CUBE_ADAPTER_HMAC_KEY` | yes | independent session pseudonymization key |
| `CUBE_ADAPTER_RECEIPT_HMAC_KEY` | no | dedicated receipt key; falls back to the session HMAC key |
| `CUBE_TEMPLATE_ID` | yes | platform-owned READY template alias |
| `CUBE_API_URL` | yes | CubeAPI address visible to the Adapter |
| `CUBE_API_KEY` | when enabled | CubeAPI credential, never exposed to plugins |
| `CUBE_PROXY_NODE_IP` | yes | CubeProxy host visible to the Adapter |
| `CUBE_PROXY_PORT_HTTP` | yes | CubeProxy HTTP port |
| `CUBE_ADAPTER_AUDIT_LOG` | no | JSONL path; defaults to local file |
| `CUBE_ADAPTER_AUDIT_UI` | no | set `1` only on a protected test network |
| `CUBE_ADAPTER_PROFILES_FILE` | no | declarative operator-owned YAML profiles |
| `CUBE_ADAPTER_TASK_TEMPLATES_FILE` | no | server-owned task schemas, commands, approvals, and output policies |
| `CUBE_ADAPTER_STATE_BACKEND_URL` | HA/recovery | `redis://` or `rediss://` URL |
| `CUBE_ADAPTER_STATE_ENCRYPTION_KEY` | with Redis | Fernet key for persisted records |
| `CUBE_ADAPTER_AUDIT_SINKS` | no | comma list of `file`, `stdout`, `http` |
| `CUBE_ADAPTER_TLS_CERT_FILE` | no | HTTPS server certificate |
| `CUBE_ADAPTER_TLS_CLIENT_CA_FILE` | no | require and validate mTLS clients |
| `CUBE_ADAPTER_SANDBOX_TIMEOUT` | no | Sandbox timeout; default 300 seconds |
| `CUBE_ADAPTER_MAX_COMMAND_SECONDS` | no | command cap; default 120 seconds |

In token principals or OIDC claims, `allowed_actions` / `cube_actions` accepts
exact actions or family wildcards such as `task:*`. `allowed_task_templates` /
`cube_task_templates` restricts named tasks. Omission preserves the compatible
allow-all behavior; an explicit empty list denies every action or task.

For an OIDC-only or mTLS-only Helm deployment, set
`auth.sharedTokenEnabled=false`. OIDC requires both issuer and audience; mTLS
subject authentication requires a verified client CA. The bundled Runtime
plugins currently use bearer/OIDC tokens; mTLS-only mode is for MCP or custom
clients that present a certificate.

When NetworkPolicy is enabled, use `networkPolicy.extraIngress` for an external
Prometheus scraper and `networkPolicy.extraEgress` for external OIDC JWKS or
HTTP audit collectors. Same-namespace Redis egress is generated automatically;
set `redisNamespace` and optionally `redisPodSelector` for a remote namespace.

`cubesandbox==0.7.0` does not read `CUBE_PROXY_SCHEME`; do not copy that variable
from another SDK without checking the SDK version in use.

## Test and verify

```bash
python3 -m venv .venv
.venv/bin/pip install -r adapter/requirements-dev.txt
PYTHON=.venv/bin/python make test
PYTHON=.venv/bin/python make typecheck
make docker-build
```

An end-to-end acceptance test should prove that the same short `sandbox_ref`
appears in the Agent tool result, CubeSandbox's live list and the Adapter audit,
then prove that the live list returns to zero after `cube_release(action=kill)`.

## Upgrade and uninstall

Pull a release, re-run the appropriate installer, then restart the Runtime:

```bash
git pull --ff-only
./scripts/install.sh openclaw --adapter-url <url> --token-file <path>
```

Remove the Kubernetes release without deleting a separately managed Secret:

```bash
helm uninstall cube-agent-adapter -n agent-runtime
```

Disable the OpenClaw integration before removing its package:

```bash
openclaw plugins disable cube-adapter-tools
```

Review and remove the generated DSH patch from
`${XDG_CONFIG_HOME:-$HOME/.config}/cubesandbox-agent-adapter` when uninstalling.

Disable the Hermes integration before removing its plugin directory:

```bash
hermes plugins disable cube-adapter-tools
```

## Security and current limits

- Memory state remains the zero-dependency default and is intentionally
  single-replica. Redis state is required when `replicaCount > 1`; records are
  encrypted and operations use renewable distributed locks.
- PTY, SSE output, async cancellation, tenant quotas, and one independent task
  approver are implemented. Quorum approval, external approval callbacks, and
  a general-purpose rate limiter are not.
- CubeSandbox v0.7 does not support snapshots with volume/host mounts; profiles
  reject that combination unless an operator explicitly enables the gate.
- Profile `network` fields are operator-owned configuration, never model input.
- The DSH integration exposes `cube_*` tools; it is not yet a transparent native
  `shell/fs/pty` provider.
- OpenClaw does not currently expose a stable generic fourth sandbox backend;
  this project uses its documented Tool Plugin interface.
- Hermes host terminal/file tools remain independent of this plugin; use a
  restricted toolset or profile when CubeSandbox must be the only executor.
- The MCP facade is stdio-only by default so it does not create a second
  unauthenticated network listener.
- Use per-tenant tokens or OIDC plus TLS/mTLS, restrictive network policy and
  centralized audit before production use.

See [SECURITY.md](SECURITY.md) for reporting and deployment guidance.

## CubeSandbox ecosystem

CubeSandbox has an official, open
[Agent integration guide program](https://github.com/TencentCloud/CubeSandbox/issues/244).
It asks contributors to claim a framework, provide a runnable demo and document
Cube-specific isolation, timeout and mount controls. Hermes Agent fits its
`Others` category; this repository supplies the standalone plugin, bilingual
README, real screenshots and runnable test path needed for an upstream guide.

Additional official channels are
[GitHub Discussions](https://github.com/tencentcloud/CubeSandbox/discussions),
the [Cube 100 production-user program](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/cube100.md)
and the Discord link maintained in the upstream README.

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md)
and keep product-specific integrations behind the shared Adapter contract.

This is a community project and is not an official Tencent Cloud, CubeSandbox,
OpenClaw, DeepSeek or Nous Research project.

## License

Apache-2.0. See [LICENSE](LICENSE).
