#!/usr/bin/env python3
"""Official MCP SDK stdio facade for the Cube Adapter HTTP API."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


class AdapterClientError(RuntimeError):
    pass


class AdapterHttpClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        ca_file: Optional[str] = None,
        client_cert_file: Optional[str] = None,
        client_key_file: Optional[str] = None,
        timeout: float = 310,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError("CUBE_ADAPTER_URL must be an absolute HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("plain HTTP is allowed only for a loopback Adapter URL")
        if bool(client_cert_file) != bool(client_key_file):
            raise RuntimeError("MCP client certificate and key must be configured together")
        if client_cert_file and parsed.scheme != "https":
            raise RuntimeError("MCP client certificates require an HTTPS Adapter URL")
        if not token and not client_cert_file:
            raise RuntimeError("an Adapter bearer token or mTLS client certificate is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=ca_file)
        if client_cert_file and client_key_file:
            self.ssl_context.load_cert_chain(client_cert_file, client_key_file)

    @classmethod
    def from_env(cls) -> "AdapterHttpClient":
        token = os.getenv("CUBE_ADAPTER_TOKEN", "")
        token_file = os.getenv("CUBE_ADAPTER_TOKEN_FILE")
        if not token and token_file:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        return cls(
            os.getenv("CUBE_ADAPTER_URL", "http://127.0.0.1:8787"),
            token,
            ca_file=os.getenv("CUBE_ADAPTER_CA_FILE"),
            client_cert_file=os.getenv("CUBE_ADAPTER_CLIENT_CERT_FILE"),
            client_key_file=os.getenv("CUBE_ADAPTER_CLIENT_KEY_FILE"),
        )

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body or {}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                value = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                payload = json.load(error)
                detail = payload.get("error", {})
                message = detail.get("message") or f"Adapter returned HTTP {error.code}"
                code = detail.get("code") or "adapter_error"
            except Exception:
                code = "adapter_error"
                message = f"Adapter returned HTTP {error.code}"
            raise AdapterClientError(f"{code}: {message}") from error
        except urllib.error.URLError as error:
            raise AdapterClientError(f"Adapter connection failed: {error.reason}") from error
        if not isinstance(value, dict):
            raise AdapterClientError("Adapter returned a non-object response")
        return value

    @staticmethod
    def segment(value: str) -> str:
        return urllib.parse.quote(value, safe="")


def build_mcp_server(
    client: Optional[AdapterHttpClient] = None, *, profile: Optional[str] = None
):
    """Build the server lazily so the REST-only image can diagnose missing MCP deps."""
    try:
        from mcp.server import MCPServer
    except ImportError as error:  # pragma: no cover - packaging failure
        raise RuntimeError("MCP support requires the 'mcp>=2,<3' package") from error

    api = client or AdapterHttpClient.from_env()
    selected_profile = profile or os.getenv("CUBE_ADAPTER_PROFILE", "offline-code")
    if not selected_profile:
        raise RuntimeError("CUBE_ADAPTER_PROFILE must not be empty")
    mcp = MCPServer(
        "CubeSandbox Agent Adapter",
        instructions=(
            "Use CubeSandbox microVM leases for isolated agent execution. Acquire one "
            "lease per runtime session, reuse its lease_ref, and release it when finished."
        ),
    )

    @mcp.tool()
    def cube_acquire(
        runtime: str,
        session_key: str,
        request_id: Optional[str] = None,
    ) -> dict:
        """Acquire or reconnect an isolated CubeSandbox lease."""
        return api.post(
            "/v1/leases/acquire",
            _compact(
                runtime=runtime,
                session_key=session_key,
                profile=selected_profile,
                request_id=request_id,
            ),
        )

    @mcp.tool()
    def cube_status(lease_ref: str) -> dict:
        """Return lease state, profile, TTL, and workspace information."""
        return api.post(f"/v1/leases/{api.segment(lease_ref)}/status", {})

    @mcp.tool()
    def cube_exec(
        lease_ref: str,
        command: str,
        cwd: str = "/workspace",
        timeout_ms: int = 120000,
        request_id: Optional[str] = None,
    ) -> dict:
        """Run a bounded non-interactive shell command in a lease."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/exec",
            _compact(command=command, cwd=cwd, timeout_ms=timeout_ms, request_id=request_id),
        )

    @mcp.tool()
    def cube_read_text(lease_ref: str, path: str) -> dict:
        """Read a UTF-8 text file under /workspace or /tmp."""
        return api.post(f"/v1/leases/{api.segment(lease_ref)}/read", {"path": path})

    @mcp.tool()
    def cube_write_text(lease_ref: str, path: str, content: str) -> dict:
        """Write a UTF-8 text file under /workspace or /tmp."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/write",
            {"path": path, "content": content},
        )

    @mcp.tool()
    def cube_list_files(lease_ref: str, path: str = "/workspace") -> dict:
        """List a directory under /workspace or /tmp."""
        return api.post(f"/v1/leases/{api.segment(lease_ref)}/list", {"path": path})

    @mcp.tool()
    def cube_stat_file(lease_ref: str, path: str) -> dict:
        """Inspect file metadata under /workspace or /tmp."""
        return api.post(f"/v1/leases/{api.segment(lease_ref)}/stat", {"path": path})

    @mcp.tool()
    def cube_upload_artifact(lease_ref: str, path: str, content_base64: str) -> dict:
        """Upload a binary artifact encoded as Base64."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/artifacts/upload",
            {"path": path, "content_base64": content_base64},
        )

    @mcp.tool()
    def cube_download_artifact(lease_ref: str, path: str) -> dict:
        """Download a binary artifact as Base64 with its SHA-256 digest."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/artifacts/download", {"path": path}
        )

    @mcp.tool()
    def cube_job_start(lease_ref: str, command: str, cwd: str = "/workspace") -> dict:
        """Start a durable asynchronous command and return a job_ref."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/jobs",
            {"command": command, "cwd": cwd},
        )

    @mcp.tool()
    def cube_job_status(job_ref: str) -> dict:
        """Return asynchronous job state and exit status."""
        return api.post(f"/v1/jobs/{api.segment(job_ref)}/status", {})

    @mcp.tool()
    def cube_job_output(job_ref: str, offset: int = 0, max_bytes: int = 65536) -> dict:
        """Read a page of asynchronous job stdout/stderr."""
        return api.post(
            f"/v1/jobs/{api.segment(job_ref)}/output",
            {"offset": offset, "max_bytes": max_bytes},
        )

    @mcp.tool()
    def cube_job_cancel(job_ref: str) -> dict:
        """Cancel an asynchronous job and its process group."""
        return api.post(f"/v1/jobs/{api.segment(job_ref)}/cancel", {})

    @mcp.tool()
    def cube_task_plan(template: str, parameters: dict) -> dict:
        """Plan a schema-validated trusted task without executing it."""
        return api.post(
            "/v1/tasks/plan", {"template": template, "parameters": parameters}
        )

    @mcp.tool()
    def cube_task_submit(plan_ref: str) -> dict:
        """Submit a ready or independently approved trusted task plan."""
        return api.post(f"/v1/task-plans/{api.segment(plan_ref)}/submit", {})

    @mcp.tool()
    def cube_task_status(task_ref: str) -> dict:
        """Read trusted task state without exposing raw process output."""
        return api.post(f"/v1/tasks/{api.segment(task_ref)}/status", {})

    @mcp.tool()
    def cube_task_result(task_ref: str) -> dict:
        """Finalize a completed task, release its microVM, and return allowlisted outputs."""
        return api.post(f"/v1/tasks/{api.segment(task_ref)}/result", {})

    @mcp.tool()
    def cube_task_cancel(task_ref: str) -> dict:
        """Cancel and finalize a trusted task."""
        return api.post(f"/v1/tasks/{api.segment(task_ref)}/cancel", {})

    @mcp.tool()
    def cube_task_receipt(task_ref: str) -> dict:
        """Return the signed execution receipt for a finalized trusted task."""
        return api.post(f"/v1/tasks/{api.segment(task_ref)}/receipt", {})

    @mcp.tool()
    def cube_checkpoint(lease_ref: str, name: Optional[str] = None) -> dict:
        """Create a gated microVM checkpoint for a lease."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/checkpoints", _compact(name=name)
        )

    @mcp.tool()
    def cube_rollback(lease_ref: str, checkpoint_ref: str) -> dict:
        """Roll a lease back to one of its checkpoints."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/checkpoints/"
            f"{api.segment(checkpoint_ref)}/rollback",
            {},
        )

    @mcp.tool()
    def cube_fork(
        lease_ref: str, checkpoint_ref: str, branch: Optional[str] = None
    ) -> dict:
        """Fork a new lease from a checkpoint when its profile permits it."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/checkpoints/"
            f"{api.segment(checkpoint_ref)}/fork",
            _compact(branch=branch),
        )

    @mcp.tool()
    def cube_release(lease_ref: str, action: str = "kill") -> dict:
        """Release a lease using kill or pause according to policy."""
        return api.post(
            f"/v1/leases/{api.segment(lease_ref)}/release", {"action": action}
        )

    return mcp


def _compact(**values: Any) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def main() -> None:
    build_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
