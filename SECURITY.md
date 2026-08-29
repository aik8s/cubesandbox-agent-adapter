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
- run exactly one replica until durable ownership and fencing exist;
- deny host Shell/FS tools when a profile is supposed to be Cube-only;
- rotate the bearer token independently from the session HMAC key.

The current reference implementation is not a complete tenant boundary. It
does not yet implement tenant quotas, authorization callbacks, durable leases,
cross-process recovery, PTY, streaming cancellation or owner fencing.
