# Fail-closed durable audit

> v0.5.0 capability. It defaults to fail-closed `required` audit. Historical
> trusted-task captures validate task behavior, not the fault scenarios below.

v0.5.0 defaults to `CUBE_ADAPTER_AUDIT_MODE=required`. Each guarded operation
commits an intent before entering its implementation. Approval decisions, task
authorization, execution events and completion are committed synchronously.
Failed commits return `503 audit_unavailable`; no success/receipt is delivered.
Storage failure or unresolved operations block further guarded calls and readiness.
Completed audit history survives process restart; incomplete operations require
operator reconciliation, never automatic replay of workloads.

## Authority and delivery

The authority is `${CUBE_ADAPTER_AUDIT_LOG}.sqlite3`, not the JSONL export.
SQLite uses DELETE journaling, EXTRA synchronous commits and fullfsync. Events,
pending deliveries and operation completion share a transaction. A process-level
exclusive file lock prevents multiple writers; threads serialize commits.
The UI shows the latest 200 events, not a retention limit.

File, stdout and HTTP sinks are replicas. Pending deliveries survive restart and
retry independently. Delivery is **at-least-once**: collectors must deduplicate by
`event_id`. A crash after delivery but before acknowledgement can produce duplicates.
Acknowledgements never delete the authoritative event. Stdout is not proof that a
remote logging service has persisted data. Removing a sink leaves its backlog;
restoring the same sink name resumes it. Drain before changing destination identity.

Collector outages do not block operations while local commits remain durable.
Backlog can eventually fill the disk, at which point new operations fail closed.
There is no automatic authority pruning: monitor storage and delivery failures,
and define protected backups and retention before production deployment.
`/metrics` exposes `cube_adapter_audit_required`, `cube_adapter_audit_blocked`,
`cube_adapter_audit_pending_deliveries` and `cube_adapter_audit_incomplete_operations`.
The incomplete count includes currently active calls, not only recovery incidents.

## Deployment

Check out v0.5.0, keep `CUBE_ADAPTER_AUDIT_MODE=required` in `.env`, and use the
published multi-architecture image:

```sh
docker compose pull adapter
docker compose up -d --no-build adapter
curl --fail http://127.0.0.1:18080/healthz
```

Verify `version: 0.5.0`, `audit_mode: required` and `audit_ready: true`. Pin the
image digest in a controlled production deployment. Keep the entire audit volume, including SQLite
recovery files, not just JSONL. Existing JSONL history is preserved but is not
retroactively imported as strongly audited evidence.
New images provision the audit directory for UID/GID 65532. Back up existing
volumes and verify ownership on migration; unwritable authority fails startup.

The v0.5.0 Helm chart defaults to `audit.mode: required`,
`audit.persistence.enabled: true`, and `replicaCount: 1`. It creates an RWO PVC,
uses Recreate, and rejects ephemeral storage, multiple writers, and the known
incompatible v0.4.0 image tag. Select and validate a durable local/block
StorageClass; the chart cannot prove provider sync semantics. `best_effort` is
an explicit downgrade and may drop events. The generated audit PVC carries
`helm.sh/resource-policy: keep` by default, so uninstall leaves the authority
behind for an explicit retention decision.

## Recovery

Stop the Adapter and keep its volume. Run these commands using the same source
and mounted audit volume (the tool refuses to share the live process's lock):

```sh
python -m adapter.audit_admin --database /var/log/cube-adapter/audit.jsonl.sqlite3 pending
python -m adapter.audit_admin --database /var/log/cube-adapter/audit.jsonl.sqlite3 export
```

Inspect operation/parent IDs, opaque task and lease references, Redis state and
actual CubeSandbox resources. Preserve findings in a protected incident record.
If the external outcome is uncertain, keep execution blocked; do not blindly retry.
After inspection, reconcile each operation:

```sh
python -m adapter.audit_admin --database /var/log/cube-adapter/audit.jsonl.sqlite3 reconcile \
  --operation-id OPERATION_ID --note INCIDENT_EVIDENCE_REFERENCE
```

This appends an operator/evidence digest and `manually_reconciled`; it retains the
original intent, does not assert success, and does not change workload state.
Resolve every outstanding operation and repair storage before restarting.
The tool is protected by local filesystem permissions, not Agent credentials.
Never delete the database, lock or pending rows to bypass the safety barrier.

## Guarantee boundaries

- Single process on durable local/block storage with reliable sync/locking.
  No NFS, shared filesystem deployment or HA writers. PVC durability is a deployment responsibility.
- No cross-node replication, WORM, automatic backup, tamper-proof database or
  compliance certification. Disk/PVC loss, operator deletion and dishonest storage
  acknowledgements exceed this local durability guarantee.
- MicroVM calls and the journal cannot share one transaction. A crash/timeout may
  leave a durably recorded attempt with an unknown result, not complete outcome evidence.
- Fail-closed blocks new guarded calls, not code already running in a MicroVM.
  Shutdown disconnects SDK handles without an unaudited implicit kill in required mode.
- Durable Redis state is still needed for task/lease/approval recovery. In-memory
  state is lost on restart and can require manual backend inventory reconciliation.
- Guarded authentication/API calls, GC and stream start/end are audited; malformed
  traffic before HTTP parsing, health probes and every stream byte are not an access log.
  Deploy gateway logging and rate limits separately.

See [SQLite sync semantics](https://www.sqlite.org/pragma.html#pragma_synchronous)
and [network storage limitations](https://www.sqlite.org/useovernet.html).

## Tests

`python -m unittest discover -s adapter -p 'test_*.py'` covers pre-execution write
failure, transaction commit rollback, post-effect audit failure, approval/receipt
blocking, denied-event redaction, HTTP 503, concurrent writers, exclusive ownership,
abrupt exit/SIGKILL recovery, reconciliation, sink retry and restart replay.
These are local fault-injection/fake-backend tests, not live-cluster power-loss,
PVC failure or physical full-disk certification. Repeat acceptance in the target environment.

Local validation on 2026-09-09: all 48 Python 3.12 tests passed with a dedicated
Redis container; Ruff/Mypy passed. A Linux arm64 source image started non-root on
a read-only root filesystem with a persistent audit volume. Two committed auth
events survived container SIGKILL/restart. An offline synthetic pending intent
blocked the restarted API with 503; logged reconciliation and restart restored
`audit_ready: true`. Helm rejected ephemeral storage, multiple writers and the
incompatible v0.4.0 tag in required mode.

## Kubernetes acceptance record — 2026-09-09

The v0.5.0 release candidate was deployed as a single non-root amd64 Pod with
Recreate and a retained 1 GiB RWO local-block PVC. The real CubeSandbox
trusted-task suite passed 23/23 checks, including OpenClaw, DSH, Codex MCP and
Hermes Agent plugin paths. Each client completed plan, submit, status, result
and HS256 receipt with verified MicroVM cleanup.

Strong-audit checks then verified:

- 498 committed events remained 498 after deleting and recreating the Adapter Pod;
- a second non-root process was denied the live journal's exclusive writer lock;
- an offline synthetic pending intent, which made no backend call, restarted
  with `audit_ready=false`, one incomplete operation, blocked metrics and HTTP 503;
- offline reconciliation retained the original pending intent, appended
  `manually_reconciled`, did not replay work, and restored readiness with zero incomplete operations;
- an in-Pod comparison scanned 506 authority events against mounted tokens and
  HMAC keys; no secret value, Bearer literal or known raw task marker was found.

These are sanitized deterministic report captures generated from the recorded
results, not an interactive product console. Source data and renderers live in
`docs/assets/audit-durability-acceptance/results.json` and `tests/acceptance/`.

![v0.5.0 real MicroVM and four-client acceptance](assets/audit-durability-acceptance/01-runtime-clients.png)

![v0.5.0 persistent audit and Pod restart acceptance](assets/audit-durability-acceptance/02-persistence-restart.png)

![v0.5.0 fail-closed, reconciliation and privacy acceptance](assets/audit-durability-acceptance/03-fail-closed-recovery.png)
