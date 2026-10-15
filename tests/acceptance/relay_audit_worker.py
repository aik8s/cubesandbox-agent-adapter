#!/usr/bin/env python3
"""Test-only real PostgreSQL/Redis failure worker with an instrumented backend.

Run inside the candidate image on the image relay. CubeSandbox itself is not
mocked in the separate sr1 live suite; this worker isolates the audit boundary.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import requests
from cryptography.fernet import Fernet

sys.path.insert(0, "/app")
from adapter.audit_admin import verify_chain  # noqa: E402
from adapter.config import AdapterConfig  # noqa: E402
from adapter.core import CubeAdapter  # noqa: E402
from adapter.http_api import build_server  # noqa: E402
from adapter.postgres_audit import PostgresAuditJournal  # noqa: E402

ROOT = Path("/evidence")
DSN = "postgresql://postgres@127.0.0.1:34732/cube_adapter_test"


def save(name, value):
    fd = os.open(ROOT / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)


def wait(name):
    deadline = time.monotonic() + 120
    while not (ROOT / name).exists():
        if time.monotonic() > deadline:
            raise TimeoutError(name)
        time.sleep(0.2)


def main():
    calls = []
    schema = "relay_fault_" + uuid.uuid4().hex[:12]
    prefix = "relay-fault-" + uuid.uuid4().hex
    auth = uuid.uuid4().hex * 2
    key = Fernet.generate_key().decode()
    adapters, servers = [], []

    def factory(**_kwargs):
        calls.append(1)
        return SimpleNamespace(
            sandbox_id=uuid.uuid4().hex,
            traffic_access_token="synthetic",  # noqa: S106
            close=lambda: None,
            kill=lambda: None,
            get_info=lambda: {"state": "running"},
            commands=SimpleNamespace(
                run=lambda *_a, **_kw: SimpleNamespace(stdout="RECOVERED", stderr="", exit_code=0)
            ),
        )

    def start(index):
        config = AdapterConfig(
            bind="127.0.0.1",
            port=34910 + index,
            token=auth,
            template="instrumented",
            session_hmac_key="synthetic-key-for-isolated-fault-drill",
            replicas=2,
            audit_database_url=DSN,
            audit_database_schema=schema,
            audit_log=str(ROOT / f"audit-{index}.jsonl"),
            state_backend_url="redis://127.0.0.1:34779/0",
            state_prefix=prefix,
            state_encryption_key=key,
        )
        adapter = CubeAdapter(
            config,
            sandbox_factory=factory,
            template_checker=lambda *_a, **_kw: {"status": "READY"},
            start_gc=False,
        )
        server = build_server(config, adapter)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        adapters.append(adapter)
        servers.append(server)
        return config.port

    def stop():
        for server in servers:
            server.shutdown()
            server.server_close()
        servers.clear()
        for adapter in adapters:
            try:
                adapter.close()
            except Exception:
                pass  # Lost DB connections remain fenced; no implicit reconnect.
        adapters.clear()

    report = {
        "result": "FAIL",
        "backend": "instrumented factory (audit boundary only)",
        "checks": {},
    }
    stage = "start"
    try:
        ports = [start(0), start(1)]
        save("resources-private.json", {"schema": schema, "prefix": prefix})
        save("ready.json", {"ready": True})
        wait("check-offline")
        statuses = []
        for port in ports:
            response = requests.post(
                f"http://127.0.0.1:{port}/v1/leases/acquire",
                headers={"Authorization": "Bearer " + auth},
                json={"runtime": "mcp", "session_key": "offline", "profile": "offline-code"},
                timeout=10,
            )
            assert (
                response.status_code == 503
                and response.json()["error"]["code"] == "audit_unavailable"
            )
            statuses.append(response.status_code)
        assert not calls
        report["checks"]["offline_both_replicas_fail_closed"] = "PASS"
        report["outage"] = {"statuses": statuses, "backend_factory_calls": len(calls)}
        save("offline-checked.json", {"ready": True})
        wait("database-restarted")
        assert all(
            requests.get(f"http://127.0.0.1:{port}/readyz", timeout=10).status_code == 503
            for port in ports
        )
        report["checks"]["old_instances_remain_fenced"] = "PASS"
        stop()
        stage = "restart-acquire"
        port = start(2)
        response = requests.post(
            f"http://127.0.0.1:{port}/v1/leases/acquire",
            headers={"Authorization": "Bearer " + auth},
            json={"runtime": "mcp", "session_key": "after-restart", "profile": "offline-code"},
            timeout=10,
        )
        response.raise_for_status()
        lease = response.json()["lease_ref"]
        stage = "restart-exec"
        result = requests.post(
            f"http://127.0.0.1:{port}/v1/leases/{lease}/exec",
            headers={"Authorization": "Bearer " + auth},
            json={"command": "synthetic-factory-probe"},
            timeout=10,
        )
        result.raise_for_status()
        assert result.json()["stdout"] == "RECOVERED" and len(calls) == 1
        stage = "restart-release"
        requests.post(
            f"http://127.0.0.1:{port}/v1/leases/{lease}/release",
            headers={"Authorization": "Bearer " + auth},
            json={"action": "kill"},
            timeout=10,
        ).raise_for_status()
        report["checks"]["restart_restores_authorized_execution"] = "PASS"
        stop()
        journal = PostgresAuditJournal(DSN, (), schema=schema)
        try:
            assert journal.counts()[1] == 0
            report["events"] = journal.db.execute("SELECT count(*) FROM events").fetchone()[0]
            report["chain_tip"] = verify_chain(journal)["chain_tip"]
            report["incomplete_operations"] = 0
        finally:
            journal.close()
        report["result"] = "PASS"
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "stage": stage}
        if isinstance(error, requests.HTTPError) and error.response is not None:
            report["failure"]["status"] = error.response.status_code
            report["failure"]["code"] = error.response.json().get("error", {}).get("code")
    finally:
        stop()
        save("worker-results.json", report)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
