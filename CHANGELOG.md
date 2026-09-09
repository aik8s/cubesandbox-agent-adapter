# Changelog

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
