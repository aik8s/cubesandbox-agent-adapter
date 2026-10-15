#!/usr/bin/env python3
"""Isolated current-source acceptance against a real CubeSandbox backend.

Starts two local Adapter processes sharing test-only PostgreSQL/Redis. Never
changes an existing Adapter, cluster labels, network or production audit data.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests
import yaml
from cubesandbox import Config, Sandbox, SandboxNotFoundError, Template, TemplateNotFoundError

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--registry-auth-file", type=Path, required=True)
    parser.add_argument("--mcp-image", required=True)
    parser.add_argument("--template", default="agent-code")
    parser.add_argument("--api-url", default="http://127.0.0.1:33001")
    parser.add_argument("--proxy-port", type=int, default=33081)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--hold",
        action="store_true",
        help="Keep local canary servers alive for UI screenshot verification",
    )
    args = parser.parse_args()
    work = args.workdir
    work.mkdir(parents=True, exist_ok=True)
    run = uuid.uuid4().hex[:12]
    schema, prefix = "live_" + run, "cube-adapter-live-" + run
    pg_url = os.environ["CUBE_ADAPTER_TEST_POSTGRES_URL"]
    redis_url = os.environ["CUBE_TEST_REDIS_URL"]
    cfg = Config(api_url=args.api_url, proxy_node_ip="127.0.0.1", proxy_port=args.proxy_port)
    checks = {}
    report = {
        "suite": "production-features",
        "backend": "v0.7.3",
        "sdk": "0.7.3",
        "checks": checks,
        "routing": "Two logical bindings to one sr1 backend; no production-cluster mutation",
        "result": "FAIL",
    }
    processes, streams, mcp_template, stage = [], [], None, "mcp-template"
    auth = {
        name: uuid.uuid4().hex + uuid.uuid4().hex
        for name in ("admin", "runtime", "approver", "hmac", "receipt")
    }
    environment = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "CUBE_ADAPTER_TOKEN": auth["admin"],
        "CUBE_ADAPTER_HMAC_KEY": auth["hmac"],
        "CUBE_ADAPTER_RECEIPT_HMAC_KEY": auth["receipt"],
        "CUBE_ADAPTER_AUDIT_DATABASE_URL": pg_url,
        "CUBE_ADAPTER_AUDIT_DATABASE_SCHEMA": schema,
        "CUBE_ADAPTER_STATE_BACKEND_URL": redis_url,
        "CUBE_ADAPTER_STATE_PREFIX": prefix,
        "CUBE_ADAPTER_STATE_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "CUBE_ADAPTER_AUDIT_UI": "1",
        "CUBE_ADAPTER_REPLICAS": "2",
        "CUBE_ADAPTER_KEEPALIVE_INTERVAL": "5",
        "CUBE_ADAPTER_GC_INTERVAL": "30",
    }
    leases = []

    def document(name, value):
        path = work / (name + ".yaml")
        path.write_text(yaml.safe_dump(value))
        return str(path)

    def start(index):
        port = 34880 + index
        stream = (work / (f"adapter-{index}.log")).open("ab")
        streams.append(stream)
        process = subprocess.Popen(
            [sys.executable, "-m", "adapter.cube_adapter"],
            cwd=REPO,
            env={
                **environment,
                "CUBE_ADAPTER_PORT": str(port),
                "CUBE_ADAPTER_AUDIT_LOG": str(work / f"audit-{index}.jsonl"),
            },
            stdout=stream,
            stderr=stream,
        )  # noqa: S603 -- fixed candidate process
        processes.append(process)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("candidate Adapter exited; inspect private log")
            try:
                if requests.get(f"http://127.0.0.1:{port}/readyz", timeout=8).status_code == 200:
                    return port
            except requests.RequestException:
                pass
            time.sleep(0.5)
        raise TimeoutError("candidate readiness deadline")

    def post(port, path, body=None, identity="admin", expected=200):
        response = requests.post(
            f"http://127.0.0.1:{port}" + path,
            json=body or {},
            headers={"Authorization": "Bearer " + auth[identity]},
            timeout=70,
        )
        if response.status_code != expected:
            raise RuntimeError("unexpected acceptance HTTP status: " + str(response.status_code))
        return response.json()

    try:
        raw = json.loads(args.registry_auth_file.read_text())
        username, password = base64.b64decode(raw["auth"]).decode().split(":", 1)
        template = Template.build(
            image=args.mcp_image,
            name="adapter-mcp-" + run,
            exposed_ports=[49983, 49999, 50005],
            probe_port=49999,
            probe_path="/",
            writable_layer_size="1G",
            cpu_count=1000,
            memory_mb=1024,
            allow_internet_access=False,
            registry_username=username,
            registry_password=password,
            config=cfg,
        )
        mcp_template = template.template_id
        save(
            work / "resources-private.json",
            {"template_id": mcp_template, "schema": schema, "prefix": prefix},
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            try:
                info = Template.get(mcp_template, config=cfg)
            except TemplateNotFoundError:
                time.sleep(2)
                continue
            if info.status == "READY":
                break
            if info.status == "FAILED":
                raise RuntimeError("MCP template failed; inspect private backend logs")
            time.sleep(2)
        else:
            raise TimeoutError("MCP template build deadline")
        checks[stage] = "PASS"
        print("MCP template READY; configuring candidate servers", flush=True)
        environment["CUBE_ADAPTER_BACKENDS_FILE"] = document(
            "backends",
            {
                "backends": {
                    name: {
                        "api_url": args.api_url,
                        "proxy_host": "127.0.0.1",
                        "proxy_port": args.proxy_port,
                    }
                    for name in ("lab-a", "lab-b")
                }
            },
        )
        environment["CUBE_ADAPTER_PROFILES_FILE"] = document(
            "profiles",
            {
                "profiles": {
                    "lab-code": {
                        "backend_id": "lab-a",
                        "template": args.template,
                        "keepalive_enabled": True,
                        "sandbox_timeout_seconds": 15,
                        "max_keepalive_seconds": 180,
                        "max_command_seconds": 120,
                        "lifecycle": {"on_timeout": "kill", "auto_resume": False},
                    },
                    "lab-internal": {"backend_id": "lab-b", "template_preset": "internal-v1"},
                    "lab-mcp": {
                        "backend_id": "lab-a",
                        "template": mcp_template,
                        "mcp_policy": "echo",
                    },
                }
            },
        )
        # Synthetic credential; not a real model gateway or model-response test.
        save(work / "model-key.json", {"test_key": "SYNTHETIC_CREDENTIAL"})
        model_config = {
            "models": {
                "providers": {
                    "internal": {
                        "apiKey": "synthetic-credential-not-real",
                        "models": [{"id": "demo-primary"}, {"id": "demo-fallback"}],
                    }
                }
            },
            "agents": {
                "defaults": {
                    "model": {
                        "primary": "internal/demo-primary",
                        "fallbacks": ["internal/demo-fallback"],
                    }
                }
            },
        }
        environment["CUBE_ADAPTER_TEMPLATE_PRESETS_FILE"] = document(
            "presets",
            {
                "templates": {
                    "internal-v1": {
                        "backend_id": "lab-b",
                        "template": args.template,
                        "version": "internal-test.1",
                        "files": {"/root/.openclaw/openclaw.json": model_config},
                        "preflight_argv": [
                            "python3",
                            "-c",
                            "import json; json.load(open('/root/.openclaw/openclaw.json'))",
                        ],
                    }
                }
            },
        )
        environment["CUBE_ADAPTER_MCP_POLICIES_FILE"] = document(
            "mcp",
            {
                "policies": {
                    "echo": {
                        "servers": {
                            "alpha": {
                                "run_cmd": "python3 /opt/mcp_echo_server.py",
                                "env_vars": {"ECHO_NAME": "alpha"},
                            }
                        },
                        "tools": {
                            "echo": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"text": {"type": "string", "maxLength": 256}},
                                "required": ["text"],
                            }
                        },
                        "timeout_seconds": 30,
                    }
                }
            },
        )
        environment["CUBE_ADAPTER_TASK_TEMPLATES_FILE"] = document(
            "tasks",
            {
                "tasks": {
                    "approved-demo": {
                        "description": "Write one approved synthetic result",
                        "profile": "lab-code",
                        "approval_required": True,
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["message"],
                            "properties": {"message": {"type": "string", "enum": ["PIPELINE_OK"]}},
                        },
                        "command": {
                            "argv": [
                                "python3",
                                "-c",
                                "import json,sys; json.dump({'message':sys.argv[1]},open('/workspace/approved.json','w'))",
                                "${message}",
                            ]
                        },
                        "outputs": [
                            {
                                "name": "result",
                                "path": "/workspace/approved.json",
                                "format": "json",
                                "expose": "content",
                            }
                        ],
                    }
                }
            },
        )
        principals = [
            {
                "token": auth[name],
                "tenant_id": "acceptance",
                "subject": name,
                "roles": ["runtime"] if name == "runtime" else ["approver"],
                "allowed_actions": ["*"]
                if name == "runtime"
                else ["task:approve", "task:status", "audit:read", "lease:discover"],
                "allowed_profiles": ["lab-code", "lab-internal", "lab-mcp"],
                "allowed_task_templates": ["approved-demo"],
            }
            for name in ("runtime", "approver")
        ]
        save(work / "principals.json", principals)
        environment["CUBE_ADAPTER_TOKENS_FILE"] = str(work / "principals.json")
        save(
            work / "session-private.json",
            {
                "environment": environment,
                "auth": auth,
                "schema": schema,
                "prefix": prefix,
                "mcp_template": mcp_template,
            },
        )
        stage = "two-process-required-audit"
        first, second = start(0), start(1)
        checks[stage] = "PASS"
        stage = "backend-and-template-binding"
        for profile in ("lab-code", "lab-internal"):
            lease = post(
                first,
                "/v1/leases/acquire",
                {"runtime": "mcp", "session_key": "same", "profile": profile},
            )
            leases.append(lease["lease_ref"])
        assert leases[0] != leases[1]
        assert lease["backend_id"] == "lab-b" and lease["template_version"] == "internal-test.1"
        # Normal sandbox user cannot access the root Secret config. Verify via
        # operator-owned preflight, not raw client root execution.
        report["template_config"] = {
            "version": lease["template_version"],
            "structural_preflight": "PASS",
            "model_api_response": "NOT_TESTED",
        }
        checks[stage] = "PASS"
        stage = "governed-mcp"
        lease = post(
            first,
            "/v1/leases/acquire",
            {"runtime": "mcp", "session_key": "mcp", "profile": "lab-mcp"},
        )
        leases.append(lease["lease_ref"])
        result = post(
            second,
            f"/v1/leases/{lease['lease_ref']}/mcp-call",
            {"name": "echo", "arguments": {"text": "ADAPTER_MCP_OK"}},
        )
        assert result["result"]["content"][0]["text"] == "alpha:ADAPTER_MCP_OK"
        post(
            first,
            f"/v1/leases/{lease['lease_ref']}/mcp-call",
            {"name": "not-allowed", "arguments": {}},
            expected=400,
        )
        checks[stage] = "PASS"
        stage = "job-renew-restart-resume"
        long_lease = post(
            first,
            "/v1/leases/acquire",
            {"runtime": "mcp", "session_key": "long-job", "profile": "lab-code"},
        )
        leases.append(long_lease["lease_ref"])
        job = post(
            first,
            f"/v1/leases/{long_lease['lease_ref']}/jobs",
            {"command": "printf BEFORE_RESTART; sleep 35; printf AFTER_RESTART"},
            expected=202,
        )
        time.sleep(5)
        processes[0].terminate()
        processes[0].wait(timeout=20)
        inventory = post(second, "/v1/recover")
        assert inventory["commands_restarted"] == 0
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            state = post(second, f"/v1/jobs/{job['job_ref']}/status")
            if state["state"] == "succeeded":
                break
            time.sleep(1)
        else:
            raise TimeoutError("live job deadline")
        logs = post(
            second, f"/v1/jobs/{job['job_ref']}/resume", {"stdout_offset": 0, "stderr_offset": 0}
        )
        assert base64.b64decode(logs["stdout"]["data_base64"]) == b"BEFORE_RESTARTAFTER_RESTART"
        assert not logs["restarted"]
        checks[stage] = "PASS"
        stage = "independent-approval-receipt"
        plan = post(
            second,
            "/v1/tasks/plan",
            {"template": "approved-demo", "parameters": {"message": "PIPELINE_OK"}},
            "runtime",
            201,
        )
        post(second, f"/v1/task-plans/{plan['plan_ref']}/approve", {}, "runtime", 403)
        post(second, f"/v1/task-plans/{plan['plan_ref']}/approve", {}, "approver")
        task = post(second, f"/v1/task-plans/{plan['plan_ref']}/submit", {}, "runtime", 202)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            state = post(second, f"/v1/tasks/{task['task_ref']}/status", identity="runtime")
            if state["state"] == "succeeded":
                break
            time.sleep(1)
        result = post(second, f"/v1/tasks/{task['task_ref']}/result", identity="runtime")
        assert (
            result["result"]["cleanup"] == "verified"
            and result["receipt"]["signature"]["alg"] == "HS256"
        )
        checks[stage] = "PASS"
        stage = "persistent-audit-query"
        renewals = post(
            second,
            "/v1/audit/query",
            {"action": "lease_renew", "lease_ref": long_lease["lease_ref"], "limit": 100},
        )
        renewal_count = sum(event.get("phase") is None for event in renewals["events"])
        assert renewal_count >= 3
        report["long_job"] = {
            "initial_timeout_seconds": 15,
            "job_sleep_seconds": 35,
            "timeout_action": "kill",
            "renewal_events": renewal_count,
            "automatic_resume": False,
            "command_restarted": False,
        }
        page = post(second, "/v1/audit/query", {"action": "mcp_call", "limit": 2})
        assert page["events"] and page["durable"]
        post(second, "/v1/audit/query", {}, "runtime", 403)
        scoped = post(second, "/v1/audit/query", {"limit": 20}, "approver")
        assert all(
            event["tenant_hash"] == hashlib.sha256(b"acceptance").hexdigest()[:16]
            for event in scoped["events"]
        )
        checks[stage] = "PASS"
        report["result"] = "PASS"
        print("All live feature checks PASS", flush=True)
        if args.hold:
            print("Holding private canary on localhost:34881 for console verification", flush=True)
            save(work / "report-private.json", report)
            while not (work / "finish").exists():
                time.sleep(1)
    except Exception as error:
        checks[stage] = "FAIL"
        report["failure"] = {
            "stage": stage,
            "type": type(error).__name__,
            "detail": str(error)[:160],
        }
        print("Live stage failed: " + stage + " / " + type(error).__name__, flush=True)
    finally:
        cleanup_errors = 0
        cleanup_details = []
        # Exact test-owned leases only. Administrative fallback is necessary if
        # an intentionally failed audit leaves the online entry point blocked.
        try:
            from adapter.state import RedisStateStore

            state = RedisStateStore(
                redis_url,
                prefix=prefix,
                encryption_key=environment["CUBE_ADAPTER_STATE_ENCRYPTION_KEY"],
            )
            for record in state.list_leases():
                try:
                    sandbox = Sandbox({"sandboxID": record.sandbox_id}, config=cfg)
                    sandbox.kill()
                except SandboxNotFoundError:
                    pass
                except Exception as error:
                    cleanup_errors += 1
                    cleanup_details.append({"kind": "sandbox", "type": type(error).__name__})
            state.close()
        except Exception as error:
            cleanup_errors += 1
            cleanup_details.append({"kind": "state", "type": type(error).__name__})
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in streams:
            stream.close()
        if mcp_template:
            try:
                Template.delete(mcp_template, config=cfg)
            except Exception as error:
                cleanup_errors += 1
                cleanup_details.append({"kind": "template", "type": type(error).__name__})
        report["cleanup"] = {
            "sandbox_and_template_errors": cleanup_errors,
            "details": cleanup_details,
            "isolated_db_retained_for_restore_drill": True,
        }
        if cleanup_errors:
            report["result"] = "FAIL"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
