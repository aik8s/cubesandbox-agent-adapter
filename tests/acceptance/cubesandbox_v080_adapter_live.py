#!/usr/bin/env python3
"""Exercise a disposable current-source Adapter against a real CubeSandbox.

Set the SDK CUBE_* connection variables and CUBE_TEMPLATE_ID to a v0.8-capable
template. The Adapter binds only to loopback and deletes only its own leases.
The output deliberately excludes endpoints, tokens and resource identifiers.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx2
import requests
import yaml
from cubesandbox import Sandbox, SandboxNotFoundError
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from adapter.config import AdapterConfig
from adapter.core import CubeAdapter
from adapter.http_api import AdapterHttpServer, make_handler


def main() -> int:
    report = {
        "suite": "adapter-v080-live",
        "backend": "v0.8.0",
        "sdk": "0.8.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "result": "FAIL",
        "cleanup": {},
    }
    checks = report["checks"]
    leases = []
    snapshot_names = []
    task_ref = None
    stage = "setup"
    with tempfile.TemporaryDirectory(prefix="cube-adapter-v080-") as directory:
        root = Path(directory)
        profile = root / "profiles.yaml"
        profile.write_text(
            yaml.safe_dump(
                {
                    "profiles": {
                        "v080-test": {
                            "template": os.environ["CUBE_TEMPLATE_ID"],
                            "checkpoints_enabled": True,
                            "max_active_leases_per_tenant": 4,
                            "sandbox_timeout_seconds": 600,
                        }
                    }
                }
            )
        )
        task = yaml.safe_load(Path("tests/acceptance/sr1-task-templates.yaml").read_text())[
            "tasks"
        ]["trusted-auto"]
        task["profile"] = "v080-test"
        tasks = root / "tasks.yaml"
        tasks.write_text(yaml.safe_dump({"tasks": {"trusted-auto": task}}))
        token = uuid.uuid4().hex + uuid.uuid4().hex
        config = AdapterConfig(
            token=token,
            session_hmac_key=uuid.uuid4().hex + uuid.uuid4().hex,
            template=os.environ["CUBE_TEMPLATE_ID"],
            profiles_file=str(profile),
            task_templates_file=str(tasks),
            receipt_hmac_key=uuid.uuid4().hex + uuid.uuid4().hex,
            audit_log=str(root / "audit.jsonl"),
        )
        adapter = CubeAdapter(config, start_gc=False)

        class DiagnosticHandler(make_handler(adapter)):
            def _upstream_error(self, error):
                diagnostic = os.environ.get("CUBE_ACCEPTANCE_PRIVATE_ERROR_LOG")
                if diagnostic:
                    descriptor = os.open(diagnostic, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(descriptor, "a") as stream:
                        stream.write(traceback.format_exc() + "\n")
                super()._upstream_error(error)

        server = AdapterHttpServer(("127.0.0.1", 0), DiagnosticHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        session = requests.Session()
        session.trust_env = False
        session.headers["Authorization"] = "Bearer " + token
        mcp_process = None

        def post(path, body=None, expected=200):
            response = session.post(base + path, json=body or {}, timeout=180)
            if response.status_code != expected:
                raise RuntimeError(f"HTTP {response.status_code}, expected {expected}")
            return response.json()

        def lease_path(lease, action):
            return f"/v1/leases/{lease}/{action}"

        try:
            stage = "authentication"
            assert requests.get(base + "/v1/leases", timeout=10).status_code == 401
            checks[stage] = "PASS"
            stage = "acquire-reuse-exec"
            body = {"runtime": "mcp", "session_key": uuid.uuid4().hex, "profile": "v080-test"}
            first = post("/v1/leases/acquire", body)
            lease = first["lease_ref"]
            leases.append(lease)
            assert post("/v1/leases/acquire", body)["reused"]
            response = post(lease_path(lease, "exec"), {"command": "printf ADAPTER_V080_OK"})
            assert response["exit_code"] == 0 and response["stdout"] == "ADAPTER_V080_OK"
            checks[stage] = "PASS"
            stage = "filesystem-and-artifact"
            post(lease_path(lease, "write"), {"path": "/workspace/v080.txt", "content": "before"})
            assert (
                post(lease_path(lease, "read"), {"path": "/workspace/v080.txt"})["content"]
                == "before"
            )
            post(
                lease_path(lease, "artifacts/upload"),
                {"path": "/workspace/v080.bin", "content_base64": "AAH/"},
            )
            assert (
                post(lease_path(lease, "artifacts/download"), {"path": "/workspace/v080.bin"})[
                    "content_base64"
                ]
                == "AAH/"
            )
            post(lease_path(lease, "mkdir"), {"path": "/workspace/v080-dir"})
            post(lease_path(lease, "write"), {"path": "/workspace/v080-dir/a", "content": "file"})
            listing = post(lease_path(lease, "list"), {"path": "/workspace/v080-dir"})
            assert any(item["name"] == "a" for item in listing["entries"])
            post(lease_path(lease, "stat"), {"path": "/workspace/v080-dir/a"})
            post(
                lease_path(lease, "move"),
                {"path": "/workspace/v080-dir/a", "destination": "/workspace/v080-dir/b"},
            )
            post(lease_path(lease, "remove"), {"path": "/workspace/v080-dir/b"})
            checks[stage] = "PASS"
            stage = "async-job"
            job = post(lease_path(lease, "jobs"), {"command": "printf ASYNC_V080_OK"}, 202)[
                "job_ref"
            ]
            for _ in range(30):
                result = post(f"/v1/jobs/{job}/status")
                if result["state"] not in ("running", "starting"):
                    break
                time.sleep(1)
            assert result["state"] == "succeeded"
            assert "ASYNC_V080_OK" in json.dumps(post(f"/v1/jobs/{job}/output"))
            checks[stage] = "PASS"
            stage = "async-job-cancel"
            job = post(lease_path(lease, "jobs"), {"command": "sleep 60"}, 202)["job_ref"]
            assert post(f"/v1/jobs/{job}/cancel")["state"] == "cancelled"
            checks[stage] = "PASS"
            stage = "pty-create-resize-kill"
            pty = post(lease_path(lease, "ptys"), {"rows": 24, "cols": 80}, 201)["pty_ref"]
            assert post(f"/v1/ptys/{pty}/resize", {"rows": 30, "cols": 90})["cols"] == 90
            post(f"/v1/ptys/{pty}/input", {"data": "printf PTY_V080_OK\n"})
            post(f"/v1/ptys/{pty}/kill")
            checks[stage] = "PASS"
            stage = "full-checkpoint-rollback"
            full_name = "adapter-full-" + uuid.uuid4().hex
            snapshot_names.append(full_name)
            full = post(lease_path(lease, "checkpoints"), {"name": full_name}, 201)
            assert full["mode"] == "full"
            post(lease_path(lease, "write"), {"path": "/workspace/v080.txt", "content": "changed"})
            post(lease_path(lease, f"checkpoints/{full['checkpoint_ref']}/rollback"))
            assert (
                post(lease_path(lease, "read"), {"path": "/workspace/v080.txt"})["content"]
                == "before"
            )
            checks[stage] = "PASS"
            stage = "fs-only-checkpoint-and-fork"
            fresh = post(
                "/v1/leases/acquire",
                {"runtime": "mcp", "session_key": uuid.uuid4().hex, "profile": "v080-test"},
            )
            lease = fresh["lease_ref"]
            leases.append(lease)
            post(lease_path(lease, "write"), {"path": "/workspace/v080.txt", "content": "before"})
            post(
                lease_path(lease, "exec"),
                {
                    "command": "nohup bash -c 'exec -a adapter-v080-live-process sleep 600' </dev/null >/dev/null 2>&1 & echo $! > /workspace/v080-pid"
                },
            )
            fs_name = "adapter-fs-" + uuid.uuid4().hex
            snapshot_names.append(fs_name)
            snapshot = post(
                lease_path(lease, "checkpoints"), {"name": fs_name, "mode": "fs-only"}, 201
            )
            assert snapshot["mode"] == "fs-only", "snapshot mode"
            post(
                lease_path(lease, f"checkpoints/{snapshot['checkpoint_ref']}/rollback"),
                expected=409,
            )
            post(lease_path(lease, "write"), {"path": "/workspace/v080.txt", "content": "after"})
            branch = post(
                lease_path(lease, f"checkpoints/{snapshot['checkpoint_ref']}/fork"),
                {"branch": "fs-test"},
                201,
            )
            child = branch["lease_ref"]
            leases.append(child)
            assert (
                post(lease_path(child, "read"), {"path": "/workspace/v080.txt"})["content"]
                == "before"
            ), "child file"
            assert (
                post(lease_path(lease, "read"), {"path": "/workspace/v080.txt"})["content"]
                == "after"
            ), "source file"
            cmd = "p=$(cat /workspace/v080-pid); if [ -r /proc/$p/cmdline ]; then tr '\\0' ' ' < /proc/$p/cmdline; fi"
            assert post(lease_path(lease, "exec"), {"command": cmd})["stdout"].startswith(
                "adapter-v080-live-process"
            ), "source process"
            assert not post(lease_path(child, "exec"), {"command": cmd})["stdout"].startswith(
                "adapter-v080-live-process"
            ), "child process"
            checks[stage] = "PASS"
            stage = "mcp-stdio-checkpoint"
            mcp_name = "adapter-mcp-" + uuid.uuid4().hex
            snapshot_names.append(mcp_name)

            async def exercise_mcp():
                env = {**os.environ, "CUBE_ADAPTER_URL": base, "CUBE_ADAPTER_TOKEN": token}
                params = StdioServerParameters(
                    command=sys.executable, args=["-m", "adapter.mcp_server"], env=env
                )
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize()
                        names = {tool.name for tool in (await client.list_tools()).tools}
                        assert {
                            "cube_checkpoint",
                            "cube_checkpoint_list",
                            "cube_checkpoint_delete",
                        } <= names
                        result = await client.call_tool(
                            "cube_checkpoint",
                            {"lease_ref": lease, "name": mcp_name, "mode": "fs-only"},
                        )
                        assert not result.is_error
                        data = result.structured_content or json.loads(result.content[0].text)
                        assert data["mode"] == "fs-only"
                        checkpoint_ref = data["checkpoint_ref"]
                        listed = await client.call_tool(
                            "cube_checkpoint_list", {"lease_ref": lease}
                        )
                        assert not listed.is_error
                        items = listed.structured_content or json.loads(listed.content[0].text)
                        assert any(
                            item["checkpoint_ref"] == checkpoint_ref
                            for item in items["checkpoints"]
                        )
                        deleted = await client.call_tool(
                            "cube_checkpoint_delete",
                            {"lease_ref": lease, "checkpoint_ref": checkpoint_ref},
                        )
                        assert not deleted.is_error
                        value = deleted.structured_content or json.loads(deleted.content[0].text)
                        assert value["deleted"]

            asyncio.run(exercise_mcp())
            checks[stage] = "PASS"
            stage = "remote-mcp-checkpoint"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                mcp_port = sock.getsockname()[1]
            mcp_process = subprocess.Popen(  # noqa: S603 - fixed module and interpreter
                [
                    sys.executable,
                    "-m",
                    "adapter.remote_mcp",
                    "--adapter-url",
                    base,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(mcp_port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            remote_url = f"http://127.0.0.1:{mcp_port}/mcp"
            for _ in range(100):
                if mcp_process.poll() is not None:
                    raise RuntimeError("remote MCP process exited")
                try:
                    if requests.post(remote_url, timeout=1).status_code == 401:
                        break
                except requests.RequestException:
                    time.sleep(0.1)
            else:
                raise TimeoutError("remote MCP startup")

            async def exercise_remote_mcp():
                async with httpx2.AsyncClient(
                    headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=180
                ) as http:
                    async with streamable_http_client(remote_url, http_client=http) as streams:
                        async with ClientSession(*streams) as client:
                            await client.initialize()
                            result = await client.call_tool(
                                "cube_checkpoint_list", {"lease_ref": lease}
                            )
                            assert not result.is_error
                            data = result.structured_content or json.loads(result.content[0].text)
                            assert any(item["mode"] == "fs-only" for item in data["checkpoints"])

            asyncio.run(exercise_remote_mcp())
            checks[stage] = "PASS"
            stage = "checkpoint-inventory"
            listing = session.get(base + lease_path(lease, "checkpoints"), timeout=30).json()
            assert {item["mode"] for item in listing["checkpoints"]} == {"fs-only"}
            checks[stage] = "PASS"
            stage = "trusted-task-and-receipt"
            plan = post(
                "/v1/tasks/plan",
                {"template": "trusted-auto", "parameters": {"message": "safe-v080"}},
                201,
            )
            task_ref = post(f"/v1/task-plans/{plan['plan_ref']}/submit", expected=202)["task_ref"]
            for _ in range(90):
                task_status = post(f"/v1/tasks/{task_ref}/status")
                if task_status["state"] not in ("starting", "running"):
                    break
                time.sleep(1)
            assert task_status["state"] == "succeeded", "task execution"
            result = post(f"/v1/tasks/{task_ref}/result")
            assert result["execution_state"] == "succeeded"
            assert result["sandbox_cleanup_status"] == "verified"
            assert post(f"/v1/tasks/{task_ref}/receipt")["receipt"]
            checks[stage] = "PASS"
            stage = "audit"
            rows = json.dumps(adapter.audit.recent())
            assert "checkpoint_create" in rows and "checkpoint_fork" in rows
            assert token not in rows and "ADAPTER_V080_OK" not in rows
            checks[stage] = "PASS"
            report["result"] = "PASS"
        except Exception as error:
            diagnostic = os.environ.get("CUBE_ACCEPTANCE_PRIVATE_ERROR_LOG")
            if diagnostic:
                descriptor = os.open(diagnostic, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "a") as stream:
                    stream.write(traceback.format_exc() + "\n")
            checks[stage] = "FAIL"
            report["failure"] = {
                "stage": stage,
                "type": type(error).__name__,
                "detail": str(error)[:120],
            }
        finally:
            failures = 0
            if task_ref is not None:
                try:
                    post(f"/v1/tasks/{task_ref}/cancel")
                except Exception:
                    failures += 1
            for lease in reversed(leases):
                try:
                    post(lease_path(lease, "release"), {"action": "kill"})
                except Exception:
                    try:
                        record = adapter.state.get_lease(lease)
                        if record is not None:
                            try:
                                Sandbox.connect(record.sandbox_id).kill()
                            except SandboxNotFoundError:
                                pass
                    except Exception:
                        failures += 1
            # A failed upstream snapshot may have created an untracked artifact.
            # Names are unique to this run and never supplied by another caller.
            for name in reversed(snapshot_names):
                try:
                    Sandbox.delete_snapshot(name)
                except Exception:
                    failures += 1
            report["cleanup"] = {"leases": len(leases), "errors": failures}
            if failures:
                report["result"] = "FAIL"
            session.close()
            if mcp_process is not None:
                mcp_process.terminate()
                try:
                    mcp_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    mcp_process.kill()
                    mcp_process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            adapter.close()
    output = os.environ.get("CUBE_ACCEPTANCE_OUTPUT")
    if output:
        Path(output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
