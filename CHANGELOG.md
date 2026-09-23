# Changelog

## Unreleased

### Added

- OpenCode V1/V2 MCP configuration generation, a one-command installer with a
  trusted-task-only default policy, and a redacted live-acceptance runner.
- Claude Code client guidance, a redacted live-acceptance runner, and native
  light-mode evidence for both the trusted-task flow and the Codex-equivalent
  acquire/exec/status/release path against the v0.5.0 release image.
- A real CubeSandbox v0.7.1 mounted-volume acceptance for snapshot, rollback,
  clone, referenced-snapshot deletion and complete temporary-resource cleanup.
- A 2026-09-23 repeat of the real mounted-volume snapshot suite with a redacted
  result and before/after resource-baseline verification.
- Production network and multi-cluster guidance that documents the current
  one-Adapter/one-backend boundary and the required CubeAPI plus CubeProxy path.
- sr1 kube-prometheus-stack discovery for CubeMaster, TemplateCenter, Cubelet
  and per-sandbox resource metrics, plus a verified four-target Grafana
  dashboard.
- Refreshed light-mode evidence from the post-upgrade 30/30 Adapter/backend run.

### Changed

- Reviewed CubeSandbox v0.7.1 backend compatibility while retaining the latest
  published `cubesandbox==0.7.0` Python SDK pin.
- Reviewed the v0.7.2-rc1 pre-release, including source-only Python SDK changes,
  snapshot fixes, S3lvol work, OpenCode guidance and the Kubernetes
  `cubeNode.hostNetwork: true` default; stable deployments remain on v0.7.1.
- Verified the Adapter against the Python SDK installed from the signed
  v0.7.2-rc1 source tag: 48 Python tests passed, with only the unconfigured
  optional Redis integration test skipped.
- Updated the mounted-workspace checkpoint gate and documentation for v0.7.1
  external-reference restore semantics; explicit operator opt-in remains the
  safe default.
- Redacted public Plan, Task and Sandbox evidence references with stable hashes
  and added a public environment label to the acceptance renderer.
- Refreshed the Python 3.12 slim base image and install available Debian security
  updates during container builds.

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
