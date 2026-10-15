#!/usr/bin/env python3
"""Disposable cross-cluster acceptance for the current-source Adapter.

The private configuration must contain ``backends`` and ``profiles`` mappings.
Use two independently deployed control planes, not two names for one backend.
Only leases owned by this isolated Adapter are removed. No production Adapter,
network configuration, template or audit database is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parents[2]


def private_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)


def inventory(backend: dict, resource: str) -> list[dict]:
    headers = {"X-API-Key": backend["api_key"]} if backend.get("api_key") else {}
    response = requests.get(
        backend["api_url"].rstrip("/") + "/" + resource, headers=headers, timeout=30
    )
    response.raise_for_status()
    value = response.json()
    return value if isinstance(value, list) else value.get(resource, value.get("items", []))


def sandbox_ids(backend: dict) -> set[str]:
    return {str(item["sandboxID"]) for item in inventory(backend, "sandboxes")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=34980)
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    settings = yaml.safe_load(args.config.read_text())
    backends, profiles = settings["backends"], settings["profiles"]
    if len(backends) != 2 or len(profiles) != 2:
        parser.error("configure exactly two real backends and two profiles")
    names = list(backends)
    if backends[names[0]]["api_url"] == backends[names[1]]["api_url"]:
        parser.error("the two API endpoints must differ")
    if {profile["backend_id"] for profile in profiles.values()} != set(names):
        parser.error("each backend must have its own profile")
    args.workdir.mkdir(parents=True, exist_ok=False, mode=0o700)
    work = args.workdir
    for name in ("backends", "profiles"):
        private_json(work / (name + ".json"), {name: settings[name]})
    admin, runtime, limited = (uuid.uuid4().hex + uuid.uuid4().hex for _ in range(3))
    profile_names = list(profiles)
    private_json(
        work / "principals.json",
        [
            {
                "token": token,
                "tenant_id": "acceptance",
                "subject": subject,
                "roles": ["runtime"],
                "allowed_profiles": allowed,
                "allowed_actions": ["*"],
            }
            for token, subject, allowed in (
                (runtime, "clients", profile_names),
                (limited, "restricted", profile_names[:1]),
            )
        ],
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CUBE_")}
    environment.update(
        PYTHONPATH=str(REPO),
        CUBE_ADAPTER_TOKEN=admin,
        CUBE_ADAPTER_HMAC_KEY=uuid.uuid4().hex + uuid.uuid4().hex,
        CUBE_ADAPTER_PORT=str(args.port),
        CUBE_ADAPTER_BIND="127.0.0.1",
        CUBE_ADAPTER_BACKENDS_FILE=str(work / "backends.json"),
        CUBE_ADAPTER_PROFILES_FILE=str(work / "profiles.json"),
        CUBE_ADAPTER_TOKENS_FILE=str(work / "principals.json"),
        CUBE_ADAPTER_AUDIT_LOG=str(work / "audit.jsonl"),
        CUBE_ADAPTER_AUDIT_UI="1",
        CUBE_ADAPTER_KEEPALIVE_INTERVAL="5",
    )
    private_json(
        work / "session-private.json",
        {"port": args.port, "admin": admin, "runtime": runtime, "limited": limited},
    )
    base = f"http://127.0.0.1:{args.port}"

    def post(path: str, body: dict | None = None, token: str = admin, expected: int = 200):
        response = requests.post(
            base + path,
            headers={"Authorization": "Bearer " + token},
            json=body or {},
            timeout=100,
        )
        if response.status_code != expected:
            # Details and endpoints remain in the private server log.
            raise RuntimeError("unexpected HTTP status " + str(response.status_code))
        return response.json()

    checks: dict[str, str] = {}
    report: dict = {
        "suite": "two-real-cluster-routing",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": "FAIL",
        "checks": checks,
        "clusters": 2,
        "production_configuration_changed": False,
    }
    process = None
    stage = "distinct-control-planes"
    known_ids: dict[str, set[str]] = {name: set() for name in backends}
    try:
        signatures = []
        for backend in backends.values():
            ids = sorted(item["templateID"] for item in inventory(backend, "templates"))
            if not ids:
                raise AssertionError("no existing templates")
            signatures.append(hashlib.sha256(json.dumps(ids).encode()).hexdigest())
        assert signatures[0] != signatures[1], "control-plane template inventories must differ"
        checks[stage] = "PASS"
        with (work / "adapter.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "adapter.cube_adapter"],
                cwd=REPO,
                env=environment,
                stdout=log,
                stderr=log,
            )
        stage = "candidate-readiness"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("candidate exited; inspect private log")
            try:
                if requests.get(base + "/readyz", timeout=5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            raise TimeoutError("candidate readiness")
        checks[stage] = "PASS"
        leases = []
        for index, (profile, policy) in enumerate(profiles.items()):
            stage = f"cluster-{index + 1}-create-execute"
            backend_id = policy["backend_id"]
            baseline = sandbox_ids(backends[backend_id])
            lease = post(
                "/v1/leases/acquire",
                {
                    "runtime": "mcp",
                    "session_key": "cross-cluster-acceptance",
                    "profile": profile,
                },
                runtime,
            )
            leases.append(lease)
            assert lease["backend_id"] == backend_id
            live_ids = sandbox_ids(backends[backend_id])
            added = {
                value for value in live_ids - baseline if value.startswith(lease["sandbox_ref"])
            }
            assert len(added) == 1
            known_ids[backend_id].update(added)
            other_id = next(name for name in backends if name != backend_id)
            assert not added.intersection(sandbox_ids(backends[other_id]))
            marker = "CLUSTER_" + str(index + 1) + "_OK"
            result = post(
                f"/v1/leases/{lease['lease_ref']}/exec",
                {
                    "command": "test ! -e /tmp/adapter-route-marker && printf "
                    + marker
                    + " > /tmp/adapter-route-marker && cat /tmp/adapter-route-marker",
                },
                runtime,
            )
            assert result["exit_code"] == 0 and result["stdout"] == marker
            reused = post(
                "/v1/leases/acquire",
                {
                    "runtime": "mcp",
                    "session_key": "cross-cluster-acceptance",
                    "profile": profile,
                },
                runtime,
            )
            assert reused["lease_ref"] == lease["lease_ref"] and reused["reused"]
            checks[stage] = "PASS"
        assert leases[0]["lease_ref"] != leases[1]["lease_ref"]
        checks["same-session-profile-isolation"] = "PASS"
        stage = "restricted-profile-denied"
        post(
            "/v1/leases/acquire",
            {
                "runtime": "mcp",
                "session_key": "denied",
                "profile": profile_names[1],
            },
            limited,
            403,
        )
        checks[stage] = "PASS"
        stage = "durable-audit-cluster-filter"
        for lease in leases:
            page = post("/v1/audit/query", {"backend_id": lease["backend_id"], "limit": 100})
            assert page["durable"]
            events = [event for event in page["events"] if event.get("action") == "exec"]
            assert events and all(event["backend_id"] == lease["backend_id"] for event in events)
        checks[stage] = "PASS"
        stage = "cleanup"
        for lease in leases:
            post(f"/v1/leases/{lease['lease_ref']}/release", {"action": "kill"}, runtime)
        assert all(
            not owned.intersection(sandbox_ids(backends[name])) for name, owned in known_ids.items()
        )
        checks[stage] = "PASS"
        report["result"] = "PASS"
        print("Two real cluster create/execute/isolation/audit/cleanup checks PASS", flush=True)
        if args.hold:
            private_json(work / "ready-private.json", report)
            print("Holding isolated candidate for client acceptance", flush=True)
            while not (work / "finish").exists():
                time.sleep(1)
    except Exception as error:
        checks[stage] = "FAIL"
        report["failure"] = {"stage": stage, "type": type(error).__name__}
        print("Cross-cluster check failed: " + stage + "/" + type(error).__name__, flush=True)
    finally:
        errors = 0
        if process is not None and process.poll() is None:
            try:
                # The isolated process contains test leases only, including
                # leases created later by the explicitly configured clients.
                response = requests.get(
                    base + "/v1/leases",
                    headers={"Authorization": "Bearer " + admin},
                    timeout=30,
                )
                response.raise_for_status()
                for lease in response.json()["leases"]:
                    if lease["state"] != "released":
                        post(f"/v1/leases/{lease['lease_ref']}/release", {"action": "kill"})
            except Exception:
                errors += 1
            process.terminate()
            process.wait(timeout=20)
        # A crashed candidate must never be reported as successfully cleaned.
        for name, owned in known_ids.items():
            try:
                if owned.intersection(sandbox_ids(backends[name])):
                    errors += 1
            except requests.RequestException:
                errors += 1
        report["cleanup"] = {"errors": errors, "complete": errors == 0}
        if errors:
            report["result"] = "FAIL"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
