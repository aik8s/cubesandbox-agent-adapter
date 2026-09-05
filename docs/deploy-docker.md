# Deploy the Adapter with Docker Compose

[简体中文](deploy-docker.zh-CN.md) | [Back to README](../README.md)

Use this path when CubeSandbox is already available and you want to run the
Adapter on a workstation or a single server. This guide uses the published
`ghcr.io/aik8s/cubesandbox-agent-adapter:v0.4.0` image for Linux amd64/arm64.
Docker Desktop on macOS can run the Linux container too.

## 1. Prepare the backend and tools

The Compose file deploys the Adapter and optional Redis. It does not install
CubeSandbox, create sandbox templates, or install Agent applications.
Prepare Git, Python 3 (for local configuration generation), Docker, and Compose v2:

```bash
docker version
docker compose version
```

Your CubeSandbox backend must already support sandbox creation. Obtain these
settings from its administrator; replace the example addresses:

| Setting | Example | Purpose |
| --- | --- | --- |
| `CUBE_API_URL` | `http://cube-api.example.internal:3000` | Sandbox management API |
| `CUBE_PROXY_NODE_IP` | `cube-proxy.example.internal` | Sandbox execution proxy; the variable accepts a hostname despite its name |
| `CUBE_PROXY_PORT_HTTP` | `80` | CubeProxy HTTP port |
| `CUBE_TEMPLATE_ID` | `agent-code` | Existing READY template with a shell and writable `/workspace` |
| `CUBE_API_KEY` | Supplied by the administrator | Required when CubeAPI authentication is enabled |

The backend must be reachable **from inside the Adapter container**. It can run
on bare metal or Kubernetes. The Adapter host does not need `/dev/kvm` or a
privileged container; virtualization requirements apply to CubeSandbox compute
nodes. If no backend exists, follow the [CubeSandbox deployment links](../README.md#cubesandbox-and-kubernetes)
first. Model-provider credentials belong to the Agent, and neither the Adapter
deployment nor the E2E below calls a model.

## 2. Check out the release and generate credentials

Run this on the Adapter host. All subsequent Compose commands use the repository root:

```bash
git clone --branch v0.4.0 --depth 1 https://github.com/aik8s/cubesandbox-agent-adapter.git
cd cubesandbox-agent-adapter
```

Generate three independent random keys in `.env` without printing them. This
refuses to overwrite an existing `.env`; preserve it across restarts and upgrades:

```bash
python3 - <<'PY'
import os
import secrets
from pathlib import Path

os.umask(0o077)
content = Path('.env.example').read_text()
content = content.replace('replace-with-openssl-rand-hex-32', secrets.token_hex(32))
content = content.replace('replace-with-an-independent-openssl-rand-hex-32', secrets.token_hex(32))
content = content.replace(
    '# CUBE_ADAPTER_RECEIPT_HMAC_KEY=replace-with-another-openssl-rand-hex-32',
    'CUBE_ADAPTER_RECEIPT_HMAC_KEY=' + secrets.token_hex(32),
)
with Path('.env').open('x') as target:
    target.write(content)
print('Created .env; edit the CubeSandbox endpoints before starting.')
PY
```

Edit the four backend settings in `.env`; uncomment and configure `CUBE_API_KEY`
if needed. The example `host.docker.internal:13000/13080` values are development
placeholders, not services created by this setup. Do not share `.env` or the
full output of `docker compose config`, which can expose credentials.

## 3. Choose reachable network addresses

| Backend location | Address selection |
| --- | --- |
| Remote Linux server | A server address and port resolvable and reachable from the container |
| Kubernetes | An administrator-provided reachable endpoint; external Docker normally cannot resolve `.svc` names or reach ClusterIPs |
| Same Docker network | Service names on a shared network; explicitly attach the services to that network |
| Docker Desktop host | `host.docker.internal`, provided the backend binding and firewall allow the connection |

`127.0.0.1` inside a container refers to that container. Native Linux Docker does
not necessarily define `host.docker.internal`; prefer a reachable host address.
If adding a `host-gateway` mapping, the backend must still listen on the host
interface it maps to. This does not expose a service bound only to host loopback.
Likewise, a default `kubectl port-forward` listens on host loopback and is not
automatically reachable from a Linux container.

## 4. Pull and start the published image

```bash
docker compose config --quiet
docker compose pull adapter
docker compose up -d --no-build adapter
docker compose ps
docker compose exec -T adapter python -m adapter.cube_adapter --version
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
```

The version should be `0.4.0`. `/healthz` checks liveness; `/readyz` checks backend
dependencies and templates. Wait for Compose to show `healthy` and readiness to
succeed before continuing. For startup failures:

```bash
docker compose logs --tail=100 adapter
```

The repository Compose file also defines `build:`. `--no-build` selects the
pulled image; use `scripts/dev-up.sh` or `up --build` for source development.
See the [official Compose option reference](https://docs.docker.com/reference/cli/docker/compose/up/).

## 5. Verify a real sandbox without a model

This uses Python and the Adapter Token already inside the container. The E2E
creates a sandbox, checks execution, file read/write, and an asynchronous job,
then attempts to destroy the sandbox in a `finally` block:

```bash
docker compose exec -T adapter sh -c '
  export CUBE_E2E_ADAPTER_URL=http://127.0.0.1:18080
  export CUBE_E2E_ADAPTER_TOKEN="$CUBE_ADAPTER_TOKEN"
  export CUBE_E2E_PROFILE=offline-code
  exec python -
' < tests/e2e_real.py
```

Success is exit code 0 with no exception; the script is silent on success. If
connectivity or release fails, inspect and reclaim the test sandbox in CubeSandbox.
A healthy Adapter container alone does not prove successful sandbox execution.

## 6. Connect one local client

For a client on the Compose host, export only the Adapter Token to a private
file. This refuses to overwrite an existing `docker.token`:

```bash
install -d -m 700 "$HOME/.config/cubesandbox-agent-adapter"
(
  umask 077
  set -C
  sed -n 's/^CUBE_ADAPTER_TOKEN=//p' .env > "$HOME/.config/cubesandbox-agent-adapter/docker.token"
)
```

This extraction expects the unquoted, single-line Token generated above. Keep
both copies synchronized. Choose one application already installed and
configured with its model provider:

```bash
# OpenClaw: restart its Gateway after installation
./scripts/install.sh openclaw \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$HOME/.config/cubesandbox-agent-adapter/docker.token"

# DSH: use the startup command printed by the installer
./scripts/install.sh dsh \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$HOME/.config/cubesandbox-agent-adapter/docker.token" \
  --profile web

# Hermes: start hermes -t cube-adapter after installation
./scripts/install.sh hermes \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$HOME/.config/cubesandbox-agent-adapter/docker.token"
```

For MCP hosts, adapt the [README MCP configuration](../README.md#mcp-stdio-facade)
with the loopback URL and the Token file above. Install `adapter/requirements.txt`
in the local MCP Python environment. Client installers require each application's CLI.

Ask the Agent to use CubeSandbox tools to run `printf hello`, inspect the result,
and call `cube_release(action=kill)`. Verify tool execution and cleanup. Manage
the client's host-tool permissions separately; installing an Adapter plugin does
not disable all host execution in every client.

## 7. Connect from another machine

Compose publishes only `127.0.0.1:18080` on its host. If the Adapter runs on a
server and the Agent runs on a laptop, keep an SSH tunnel open on the laptop:

```bash
ssh -N -L 18080:127.0.0.1:18080 user@adapter-server
```

Deliver only the **Adapter Token file** to the client through a trusted channel;
keep using `http://127.0.0.1:18080`. Do not copy the entire `.env`, which also
contains signing keys and potentially CubeAPI credentials. For shared ongoing
access, configure an authenticated HTTPS entry point. MCP rejects remote plain HTTP URLs.

## 8. Persistence, upgrades, and stopping

The default is one Adapter with in-memory state. Audit logs use a named Compose
volume, but this does not persist leases, tasks, approvals, or Receipts. Finish
tasks and release sandboxes before recreating the container.

For restart recovery, configure `CUBE_ADAPTER_STATE_BACKEND_URL=redis://redis:6379/0`
and an independent Fernet `CUBE_ADAPTER_STATE_ENCRYPTION_KEY` in `.env`.
A Fernet key is the URL-safe base64 encoding of 32 random bytes; the generation
example in [`.env.example`](../.env.example) uses Python's `cryptography` package.
Start Redis before recreating the Adapter:

```bash
docker compose --profile ha up -d --wait redis
docker compose --profile ha up -d --no-build adapter
```

Preserve the encryption key and Redis backups. Switching from memory to Redis
does not migrate existing in-memory records. `ha` is the Compose profile name;
a single-host Redis and Adapter are not cross-host high availability.

Before upgrading, finish tasks and back up `.env` and persistent data. Follow
the target release's instructions to update the checkout and pinned image in
`compose.yaml`, then run `pull adapter` and `up -d --no-build adapter`. Recheck
version, readiness, and E2E. Keep existing keys and check state compatibility
before rolling back an image.

```bash
docker compose stop adapter
# Resume
docker compose start adapter
# Remove this Compose project's containers/network; retain named volumes
docker compose down
```

If Redis was enabled, use `docker compose --profile ha down` to stop the whole
project. `down -v` deletes audit and Redis volumes; do not use it for routine
stops or upgrades.

## 9. Trusted tasks and troubleshooting

Training, cleaning, and independent approval require additional TaskTemplates,
Profiles, and separate identities. Simply uncommenting the task-template env
variable is insufficient: the default `config/profiles.yaml` lacks the example
`trusted-training` / `trusted-data-cleaning` Profiles. Prepare the corresponding
READY sandbox templates and bake workload scripts into **those templates**.
Mount Principal configuration read-only into the Adapter and ensure container
UID 65532 can read it. See [task examples](../examples/trusted-execution/README.md)
and the [trusted-execution guide](trusted-execution.md).

| Symptom | Check first |
| --- | --- |
| Image pull or x509 failure | Docker daemon registry access and CA trust; restricted networks can mirror the same digest and change the Compose image reference |
| Liveness succeeds, readiness fails | Container-side backend addresses, authentication, READY template, and logs |
| Client gets 401 | Token file matches the current `.env` |
| Loopback-only HTTP error | Use loopback locally, an SSH tunnel, or HTTPS remotely |
| Container healthy, task fails | Run step 5 and inspect CubeSandbox compute nodes and the template runtime |
| Host port occupied | Choose another published port, e.g. `127.0.0.1:18081:18080`, and update the client URL |
