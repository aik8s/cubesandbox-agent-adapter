# Security policy

## Supported versions

Only the latest tagged minor release receives security fixes while the project
is pre-1.0.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
vulnerability reporting flow on this repository. Include:

- affected version or commit;
- deployment topology;
- minimal reproduction;
- expected and observed security boundary;
- whether any token, tenant data or Sandbox was exposed.

Do not include live credentials, raw session identifiers or customer data.

## Deployment assumptions

The Adapter is a privileged execution broker. Treat it as part of the trusted
Agent control plane:

- authenticate every Runtime and add mTLS/workload identity in production;
- keep Cube credentials and traffic tokens out of Runtime/model context;
- restrict Adapter ingress to explicitly labelled Runtime workloads;
- restrict egress to CubeAPI, CubeProxy and DNS;
- keep the optional audit UI disabled;
- export JSONL audit events before local rotation or Pod loss;
- use one replica with memory state, or Redis-backed encrypted state for HA;
- deny host Shell/FS tools when a profile is supposed to be Cube-only;
- rotate the bearer token independently from the session HMAC key.
- use task-only principals without raw execution/file actions for production
  data workflows, and a different `approver` principal for review;
- keep the receipt HMAC key out of Agent credentials and retain old verification
  keys for the required receipt-audit period.

The Adapter implements tenant-scoped leases and quotas, Redis recovery, PTY and
streaming cancellation, named TaskTemplates, closed parameter/output schemas,
action scopes, one independent approver, and signed receipts. It does not
replace cluster admission policy, quorum/corporate approval systems, DLP, a
general rate limiter, hardware remote attestation, or deployment-specific
identity and network controls. Task parameters and approved result content are
stored in Adapter state; use encrypted Redis and apply access/retention policy.
