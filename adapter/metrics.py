"""Prometheus metrics kept separate from the public response model."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class AdapterMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "cube_adapter_operations_total",
            "Adapter operations by action and outcome.",
            ["action", "outcome", "runtime", "profile"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "cube_adapter_operation_duration_seconds",
            "Adapter operation latency.",
            ["action"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 60, 180),
            registry=self.registry,
        )
        self.active_leases = Gauge(
            "cube_adapter_active_leases",
            "Persisted leases visible to this Adapter state backend.",
            registry=self.registry,
        )
        self.active_jobs = Gauge(
            "cube_adapter_active_jobs",
            "Jobs not yet observed as complete.",
            registry=self.registry,
        )
        self.state_ready = Gauge(
            "cube_adapter_state_backend_ready",
            "Whether the configured state backend is reachable.",
            registry=self.registry,
        )
        self.audit_dropped = Counter(
            "cube_adapter_audit_events_dropped_total",
            "Audit events dropped because the async sink queue was full.",
            registry=self.registry,
        )
        self.audit_failures = Counter(
            "cube_adapter_audit_sink_failures_total",
            "Audit sink delivery failures by sink type.",
            ["sink"],
            registry=self.registry,
        )
        self.gc_actions = Counter(
            "cube_adapter_gc_actions_total",
            "Lease garbage-collection outcomes.",
            ["outcome"],
            registry=self.registry,
        )

    def observe(
        self,
        action: str,
        outcome: str,
        duration_seconds: float,
        *,
        runtime: str = "unknown",
        profile: str = "unknown",
    ) -> None:
        self.requests.labels(action, outcome, runtime, profile).inc()
        self.duration.labels(action).observe(max(0.0, duration_seconds))

    def render(self) -> bytes:
        return generate_latest(self.registry)
