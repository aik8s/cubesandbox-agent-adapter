# Office-network Agents and production CubeSandbox clusters

This guide defines the current boundary when OpenClaw or another Agent runs in
an office Kubernetes cluster while CubeSandbox runs in one or more production
networks.

## Current support

| Scenario | Status |
| --- | --- |
| Office Agent and Adapter using one production CubeSandbox | Supported when both CubeAPI and CubeProxy are reachable |
| Return production sandbox results to the Agent | Supported through Adapter responses |
| Direct Agent access to a sandbox IP | Neither required nor recommended |
| One Adapter Deployment per CubeSandbox cluster | Supported and recommended today |
| One Adapter process routing Profiles/tasks to several backends | **Not supported** |
| One OpenClaw plugin configuration dynamically selecting Adapters | **Not supported**; it has one `adapterUrl` and one `profile` |

The process and Helm chart accept one `CUBE_API_URL`, `CUBE_PROXY_NODE_IP`,
`CUBE_PROXY_PORT_HTTP`, `CUBE_API_KEY`, and default template. Profiles control
sandbox policy but do not identify a backend.

## Network path

```text
Office Agent
    -> HTTPS/mTLS Adapter API
Office Adapter
    -> CubeAPI: create, lifecycle, snapshots, volumes and template checks
    -> CubeProxy: commands, files, PTY and job output
Production CubeSandbox microVM
```

Allowlisting only CubeAPI is normally insufficient: creation may work while
execution and file operations fail because CubeProxy is unreachable. The Agent
does not need the sandbox IP. Adapter retrieves data-plane results through
CubeProxy and returns policy-filtered responses.

Object storage, databases and Redis used internally by CubeSandbox do not need
office-network exposure. Production data should be accessed by the sandbox with
a short-lived workload identity instead of being transported through Agent text.

## Recommended multi-cluster deployment

Run an independently configured Adapter release for each backend. Give each one
separate Cube credentials, TLS identity, state prefix/encryption key, audit
storage, profiles, templates, principals, alerts and egress policy. This avoids
cross-cluster reuse of sandbox, snapshot, volume and session identifiers.

One OpenClaw plugin instance currently targets one Adapter. Split Agents by
production domain or place a server-controlled routing gateway in front. Never
let the model submit arbitrary endpoints or credentials. MCP clients can
register multiple named servers, each pinned to one Adapter, with tool access
restricted by client and Adapter policy.

The stronger topology puts Adapter inside the production boundary and exposes
only its narrow authenticated API to the office network. If Adapter remains in
the office cluster, use private routing, fixed egress NAT, TLS/mTLS for CubeAPI
and CubeProxy, per-cluster credentials, explicit egress policy, fine-grained
principals, trusted TaskTemplates, approval, durable audit and output allowlists.

## Missing pieces for one-process multi-backend routing

Safe support requires more than a caller-provided `cluster` field:

- an operator-owned backend registry with endpoint, proxy, template, TLS,
  Secret reference and health data;
- Profile/TaskTemplate-to-`backend_id` routing with authorization;
- a per-backend `cubesandbox.Config` client pool used by every Sandbox,
  Template, Volume, Snapshot and reconnect operation;
- `backend_id` in leases, checkpoints, jobs, tasks, session idempotency keys and
  quota keys;
- backend identity in redacted audits and signed receipts;
- per-backend readiness, circuit breaking, GC and orphan reconciliation;
- Helm Secret, certificate rotation and NetworkPolicy support for every backend;
- a stable OpenClaw/MCP contract that does not delegate trust-boundary selection
  to model-generated input.

Current state records and readiness checks carry no backend identity, so the
existing implementation must not be treated as single-process multi-cluster
capable.
