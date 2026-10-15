# Changelog

## v0.6.0 — 2026-10-15

### Production execution features

- Route approved Profiles to independently configured CubeAPI/CubeProxy
  backends; pin live leases and approved plans to their execution environment.
- Add versioned internal-template presets, creation-time Secret-file injection,
  OpenClaw primary/fallback model validation, and fixed bootstrap/preflight hooks.
- Govern sandbox MCP through registered servers and closed argument schemas,
  bounded inputs/results, a killable deadline-bound transport and redacted audit.
- Add bounded lease renewal, restart inventory and independent byte cursors for
  stdout/stderr without automatically restarting commands.
- Add a Light approval/audit console, tenant-scoped durable history, search,
  pagination and export. Read-only roles cannot inherit runtime access.
- Add PostgreSQL multiwriter required-audit authority with fenced owners,
  transactional delivery claims, offline SQLite import and hash-chain archives.
  SQLite remains the compatible single-writer default. Database HA and backup
  retention remain operator responsibilities; no production migration is automatic.
- Upgrade the Python SDK to `cubesandbox==0.7.3`; expose six additional tools
  through the native plugins and MCP façade. Existing tool names remain valid.

### Fixed

- OpenClaw installation now merges all 25 registered tools into `tools.alsoAllow`,
  including the six new feature tools, while retaining existing entries. A
  manifest-derived regression test prevents the allowlist drifting again.
- Revalidate the backend sandbox before reusing a session lease. If the sandbox
  expired, create a fresh one instead of returning a dead lease; preserve any
  checkpoint-bearing lease for explicit cleanup. Status and direct execution
  now report a confirmed backend expiration without leaving an uncertain audit
  operation; release and garbage collection treat a confirmed missing sandbox
  as already deleted.

### Added

- Real two-cluster write acceptance on sr1 and gd18-ai-002, plus 50 verified
  Cube tool calls across OpenClaw, DSH, Hermes, Codex and OpenCode. New Light
  captures show four native Web UIs; Codex TUI capture remains pending terminal
  permission. Claude Code was not rerun in this acceptance round.
- A real internal-model reply through an injected OpenClaw preset in a
  disposable production-cluster sandbox (embedded CLI, not gateway/TUI).
- Reusable two-backend and OpenClaw preset runners, native transcript
  verification and negative tests that reject denied MCP calls even when the
  client process exits successfully.
- OpenCode V1/V2 MCP configuration generation, a one-command installer with a
  trusted-task-only default policy, and a redacted live-acceptance runner.
- Claude Code client guidance, a redacted live-acceptance runner, and native
  light-mode evidence for both the trusted-task flow and the Codex-equivalent
  acquire/exec/status/release path against the v0.5.0 release image.
- A real CubeSandbox v0.7.1 mounted-volume acceptance for snapshot, rollback,
  clone, referenced-snapshot deletion and complete temporary-resource cleanup.
- A 2026-09-23 repeat of the real mounted-volume snapshot suite with a redacted
  result and before/after resource-baseline verification.
- Production network and multi-cluster guidance that documents the v0.5.0
  one-Adapter/one-backend boundary and the required CubeAPI plus CubeProxy path.
- sr1 kube-prometheus-stack discovery for CubeMaster, TemplateCenter, Cubelet
  and per-sandbox resource metrics, plus a verified four-target Grafana
  dashboard.
- Refreshed light-mode evidence from the post-upgrade 30/30 Adapter/backend run.

### Changed

- v0.6.0 pins `cubesandbox==0.7.3`; the older v0.5.0 image keeps its
  original `cubesandbox==0.7.0` SDK pin.
- Reviewed the stable CubeSandbox v0.7.2 release, including source-only Python
  SDK changes, snapshot fixes, S3lvol work, OpenCode guidance and the Kubernetes
  `cubeNode.hostNetwork: true` default. The sr1 upgrade and narrow SDK canaries
  passed. Subsequent v0.7.3 backend and Adapter/client acceptance is recorded
  in the production-features guide.
- Verified the Adapter against the Python SDK installed from the v0.7.2 source
  tag: 47 Python tests passed, with only the unconfigured optional Redis
  integration test skipped.
- Updated the mounted-workspace checkpoint gate and documentation for v0.7.1
  external-reference restore semantics; explicit operator opt-in remains the
  safe default.
- Redacted public Plan, Task and Sandbox evidence references with stable hashes
  and added a public environment label to the acceptance renderer.
- Refreshed the Python 3.12 slim base image and install available Debian security
  updates during container builds.

### Upgrade notes

- Keep the audit volume, identity/HMAC keys and Profiles. SQLite remains a
  single-writer authority; PostgreSQL plus encrypted Redis is required for
  multiwriter `required` audit. No database migration or HA setup is automatic.
- Existing tool names remain compatible. Rerun the native-client installer for
  the new tools. New console, preset, MCP and keepalive features require opt-in.
- Retain default endpoints for old leases lacking a backend fingerprint, or
  clean them up first. Recreate old task plans without a policy binding.
- The Adapter must reach both CubeAPI and CubeProxy for each backend. API-only
  HTTPS ingress is not a data-plane tunnel.

See [configuration, acceptance evidence and upgrade guidance](docs/production-features.md).

## v0.5.0 — 2026-09-09

### Added

- Fail-closed `required` audit mode with a single-writer SQLite authority,
  synchronous intent/result commits, startup integrity checks and an exclusive
  process lock.
- Persistent at-least-once delivery outbox for JSONL, stdout and HTTP replicas,
  with stable `event_id` values for collector deduplication.
- Offline `adapter.audit_admin` inspection and reconciliation for operations
  left uncertain by a crash or post-effect audit failure.
- Audit readiness and Prometheus signals for blocked state, pending deliveries
  and incomplete operations.
- Fault-injection coverage for pre-execution and post-effect failures,
  transaction rollback, abrupt exit/SIGKILL, concurrent access, sink replay,
  redaction, approval mutation and receipt delivery.

### Changed

- New runtime, Docker Compose and Helm installations default to `required`
  audit. Helm creates an RWO PVC, restricts strong audit to one replica and uses
  `Recreate` updates.
- Audit event bodies remain redacted; request bodies and identities are stored
  only as keyed digests, while opaque Adapter references may be retained.
- The container provisions `/var/log/cube-adapter` for the non-root runtime.

### Upgrade notes

- v0.4.0 JSONL files remain export history but are not imported into the v0.5.0
  authority as retroactive strong-audit evidence.
- Keep the entire audit volume, including the `.sqlite3` database and recovery
  files. Do not use NFS or multiple Adapter writers.
- Existing multi-replica installs must choose one-replica `required` mode or
  explicitly set `audit.mode=best_effort`; the latter can lose audit records.
- A crash after an external effect can leave an operation uncertain. The
  Adapter blocks new guarded calls until an operator checks external state and
  records reconciliation; it never blindly replays the workload.

See [the strong-audit guide](docs/audit-durability.md) for guarantees,
limitations, deployment and recovery.
