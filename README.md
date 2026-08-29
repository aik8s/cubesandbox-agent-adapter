# cubesandbox-agent-adapter

Community integration that routes OpenClaw and DeepSeek Harness (DSH) tool
calls into policy-controlled [CubeSandbox](https://github.com/TencentCloud/CubeSandbox)
MicroVMs.

```text
OpenClaw Tool Plugin ─┐
                     ├─ authenticated HTTP ─→ Adapter ─→ Cube SDK ─→ MicroVM
DSH Cordis Plugin ───┘                              │
                                                   └─ redacted JSONL audit
```

The Adapter is the only component that holds Cube connection settings, full
Sandbox IDs and traffic tokens. Runtime plugins receive opaque leases and
return only short Sandbox references to the model.

> **Project status:** v0.1.0 is a working reference implementation, not a
> production-ready multi-tenant control plane. Read [Security and current
> limits](#security-and-current-limits) before exposing it to untrusted users.

## What is included

- authenticated Python Adapter using `cubesandbox==0.7.0`;
- fail-closed `offline-code` policy;
- OpenClaw Tool Plugin: `cube_exec`, `cube_read`, `cube_write`, `cube_release`;
- DSH Cordis Plugin and a patch that disables common host Shell/FS tools;
- one-command installers for Kubernetes, OpenClaw and DSH;
- Docker Compose for local development;
- Helm chart, plain Kubernetes manifest, tests and release workflows;
- append-only, redacted JSONL audit events.

## Prerequisites

Before installation, verify that:

1. CubeSandbox is running and a template alias such as `agent-code` is READY;
2. the Adapter can reach CubeAPI and CubeProxy;
3. the target Runtime can reach the Adapter;
4. `kubectl` and `helm` are installed for Kubernetes deployment;
5. `openclaw` or `dsh` is installed for the corresponding plugin.

The installer never installs CubeSandbox itself.

## Quick start: Kubernetes Adapter

Clone the repository:

```bash
git clone https://github.com/aik8s/cubesandbox-agent-adapter.git
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
  --image ghcr.io/aik8s/cubesandbox-agent-adapter:v0.1.0 \
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
`tool-fs-search` and `tool-str-replace-editor`, then registers the four Cube
tools. Start DSH with the command printed by the installer, or install and start
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

## Local Docker development

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
rotating both development secrets. The Compose audit directory is ephemeral.

For manual Docker setup:

```bash
cp .env.example .env
# Replace both secret placeholders and edit the Cube endpoints.
chmod 600 .env
docker compose up -d --build
```

## API contract

```text
GET  /healthz
POST /v1/leases/acquire
POST /v1/leases/{lease_ref}/exec
POST /v1/leases/{lease_ref}/read
POST /v1/leases/{lease_ref}/write
POST /v1/leases/{lease_ref}/release
```

Every `POST` requires `Authorization: Bearer …`. `acquire` is idempotent per
`(runtime, HMAC-SHA-256(session_key))`. The HMAC key is deliberately independent
from the bearer token so routine bearer rotation does not change pseudonymous
session correlation.

The model cannot choose a template, CIDR, public-traffic setting or lifecycle
policy. Paths are restricted to `/workspace` and `/tmp`; request, command, file,
output and timeout sizes are bounded.

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
| `CUBE_ADAPTER_TOKEN` | yes | Runtime-to-Adapter bearer token |
| `CUBE_ADAPTER_HMAC_KEY` | yes | independent session pseudonymization key |
| `CUBE_TEMPLATE_ID` | yes | platform-owned READY template alias |
| `CUBE_API_URL` | yes | CubeAPI address visible to the Adapter |
| `CUBE_PROXY_NODE_IP` | yes | CubeProxy host visible to the Adapter |
| `CUBE_PROXY_PORT_HTTP` | yes | CubeProxy HTTP port |
| `CUBE_ADAPTER_AUDIT_LOG` | no | JSONL path; defaults to local file |
| `CUBE_ADAPTER_AUDIT_UI` | no | set `1` only on a protected test network |
| `CUBE_ADAPTER_SANDBOX_TIMEOUT` | no | Sandbox timeout; default 300 seconds |
| `CUBE_ADAPTER_MAX_COMMAND_SECONDS` | no | command cap; default 120 seconds |

`cubesandbox==0.7.0` does not read `CUBE_PROXY_SCHEME`; do not copy that variable
from another SDK without checking the SDK version in use.

## Test and verify

```bash
python3 -m venv .venv
.venv/bin/pip install -r adapter/requirements.txt
PYTHON=.venv/bin/python make test
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

## Security and current limits

- Lease ownership is in process memory; the chart enforces one replica.
- Restart recovery, durable encrypted traffic-token storage and owner fencing
  are not implemented.
- PTY, streaming output, cancellation, tenant quotas and approval callbacks are
  not implemented.
- The DSH integration exposes `cube_*` tools; it is not yet a transparent native
  `shell/fs/pty` provider.
- OpenClaw does not currently expose a stable generic fourth sandbox backend;
  this project uses its documented Tool Plugin interface.
- Bearer auth should be combined with mTLS/workload identity, authorization,
  rate limits and centralized audit before production use.

See [SECURITY.md](SECURITY.md) for reporting and deployment guidance.

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md)
and keep product-specific integrations behind the shared Adapter contract.

This is a community project and is not an official Tencent Cloud, CubeSandbox,
OpenClaw or DeepSeek project.

## License

Apache-2.0. See [LICENSE](LICENSE).
