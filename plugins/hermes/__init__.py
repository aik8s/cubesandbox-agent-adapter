"""Hermes Agent native Tool Plugin for the CubeSandbox Adapter."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:18080"
MAX_RESPONSE_BYTES = 1024 * 1024
LEASE_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
JOB_REF_RE = re.compile(r"^job_[a-f0-9]{20}$")
CHECKPOINT_REF_RE = re.compile(r"^checkpoint_[a-f0-9]{20}$")


class AdapterClientError(RuntimeError):
    """A redacted Adapter client error safe to return to the model."""


def _object(properties: Dict[str, Any], required: list[str] | None = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


TOOL_SCHEMAS = {
    "cube_exec": {
        "name": "cube_exec",
        "description": (
            "Execute a shell command inside this Hermes session's isolated "
            "CubeSandbox MicroVM. Use this instead of the host terminal for "
            "untrusted code and shell work."
        ),
        "parameters": _object(
            {
                "command": {
                    "type": "string",
                    "description": "Shell command to run inside the MicroVM.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Absolute /workspace or /tmp working directory.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120000,
                },
            },
            ["command"],
        ),
    },
    "cube_read": {
        "name": "cube_read",
        "description": "Read a file from this Hermes session's CubeSandbox /workspace or /tmp.",
        "parameters": _object(
            {"path": {"type": "string", "description": "Absolute file path."}},
            ["path"],
        ),
    },
    "cube_write": {
        "name": "cube_write",
        "description": "Write a UTF-8 file inside this Hermes session's CubeSandbox /workspace or /tmp.",
        "parameters": _object(
            {
                "path": {"type": "string", "description": "Absolute file path."},
                "content": {"type": "string", "description": "UTF-8 file content."},
            },
            ["path", "content"],
        ),
    },
    "cube_release": {
        "name": "cube_release",
        "description": (
            "Pause or destroy this Hermes session's CubeSandbox lease. Prefer "
            "pause when work may continue and kill when the task is complete."
        ),
        "parameters": _object(
            {"action": {"type": "string", "enum": ["pause", "kill"]}}
        ),
    },
}

TOOL_SCHEMAS.update(
    {
        "cube_status": {
            "name": "cube_status",
            "description": "Inspect this Hermes session's lease state and TTL.",
            "parameters": _object({}),
        },
        "cube_list": {
            "name": "cube_list",
            "description": "List a directory inside this Hermes session's CubeSandbox.",
            "parameters": _object({"path": {"type": "string"}}),
        },
        "cube_job_start": {
            "name": "cube_job_start",
            "description": "Start a durable asynchronous command.",
            "parameters": _object(
                {"command": {"type": "string"}, "cwd": {"type": "string"}},
                ["command"],
            ),
        },
        "cube_job_status": {
            "name": "cube_job_status",
            "description": "Inspect an asynchronous CubeSandbox job.",
            "parameters": _object({"job_ref": {"type": "string"}}, ["job_ref"]),
        },
        "cube_job_output": {
            "name": "cube_job_output",
            "description": "Read a page of asynchronous job output.",
            "parameters": _object(
                {
                    "job_ref": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1048576,
                    },
                },
                ["job_ref"],
            ),
        },
        "cube_job_cancel": {
            "name": "cube_job_cancel",
            "description": "Cancel an asynchronous CubeSandbox job.",
            "parameters": _object({"job_ref": {"type": "string"}}, ["job_ref"]),
        },
        "cube_checkpoint": {
            "name": "cube_checkpoint",
            "description": "Create a policy-gated CubeSandbox checkpoint.",
            "parameters": _object({"name": {"type": "string"}}),
        },
        "cube_rollback": {
            "name": "cube_rollback",
            "description": "Roll this Hermes lease back to a checkpoint.",
            "parameters": _object(
                {"checkpoint_ref": {"type": "string"}}, ["checkpoint_ref"]
            ),
        },
        "cube_fork": {
            "name": "cube_fork",
            "description": "Fork a new lease from a checkpoint.",
            "parameters": _object(
                {
                    "checkpoint_ref": {"type": "string"},
                    "branch": {"type": "string"},
                },
                ["checkpoint_ref"],
            ),
        },
    }
)


def _settings(ctx: Any) -> Dict[str, str]:
    adapter_url = str(
        ctx.get_config(
            "adapter_url",
            default=os.environ.get("CUBE_ADAPTER_URL", DEFAULT_URL),
        )
    ).rstrip("/")
    parsed = urlparse(adapter_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Cube Adapter URL must use HTTP or HTTPS")
    profile = str(ctx.get_config("profile", default="offline-code"))
    return {
        "adapter_url": adapter_url,
        "token_file": str(ctx.get_config("token_file", default="")),
        "token_env": str(ctx.get_config("token_env", default="CUBE_ADAPTER_TOKEN")),
        "profile": profile,
    }


def _bearer_token(settings: Dict[str, str]) -> str:
    token = os.environ.get(settings["token_env"], "").strip()
    token_file = settings["token_file"].strip()
    if not token and token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as error:
            raise AdapterClientError("configured Adapter token file is not readable") from error
    if not token:
        raise AdapterClientError(
            f"{settings['token_env']} or token_file is not configured"
        )
    return token


def _error_message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
        message = payload.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message
    except (UnicodeDecodeError, ValueError, AttributeError):
        pass
    return "request failed"


def _request(settings: Dict[str, str], path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    request = Request(
        settings["adapter_url"] + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_bearer_token(settings)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=125) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raw = error.read(MAX_RESPONSE_BYTES)
        raise AdapterClientError(
            f"Cube Adapter {error.code}: {_error_message(raw)}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise AdapterClientError("Cube Adapter is unreachable") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AdapterClientError("Cube Adapter response exceeded the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AdapterClientError("Cube Adapter returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise AdapterClientError("Cube Adapter returned an invalid response")
    return payload


def _session_key(kwargs: Dict[str, Any]) -> str:
    return str(
        kwargs.get("task_id")
        or kwargs.get("session_id")
        or kwargs.get("conversation_id")
        or "hermes-default"
    )


def _acquire(settings: Dict[str, str], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    lease = _request(
        settings,
        "/v1/leases/acquire",
        {
            "runtime": "hermes",
            "session_key": _session_key(kwargs),
            "profile": settings["profile"],
        },
    )
    lease_ref = lease.get("lease_ref")
    if not isinstance(lease_ref, str) or not LEASE_REF_RE.fullmatch(lease_ref):
        raise AdapterClientError("Cube Adapter returned an invalid lease reference")
    return lease


def _handler(
    settings: Dict[str, str],
    operation: str,
) -> Callable[[Dict[str, Any]], str]:
    def run(args: Dict[str, Any], **kwargs: Any) -> str:
        try:
            lease = _acquire(settings, kwargs)
            body = dict(args or {})
            if operation in {"exec", "read", "write", "list", "status", "release"}:
                path = f"/v1/leases/{lease['lease_ref']}/{operation}"
            elif operation == "job_start":
                path = f"/v1/leases/{lease['lease_ref']}/jobs"
            elif operation in {"job_status", "job_output", "job_cancel"}:
                job_ref = body.pop("job_ref", "")
                if not isinstance(job_ref, str) or not JOB_REF_RE.fullmatch(job_ref):
                    raise AdapterClientError("invalid Cube Adapter job reference")
                suffix = operation.removeprefix("job_")
                path = f"/v1/jobs/{job_ref}/{suffix}"
            elif operation == "checkpoint":
                path = f"/v1/leases/{lease['lease_ref']}/checkpoints"
            elif operation in {"rollback", "fork"}:
                checkpoint_ref = body.pop("checkpoint_ref", "")
                if not isinstance(checkpoint_ref, str) or not CHECKPOINT_REF_RE.fullmatch(
                    checkpoint_ref
                ):
                    raise AdapterClientError("invalid Cube Adapter checkpoint reference")
                path = (
                    f"/v1/leases/{lease['lease_ref']}/checkpoints/"
                    f"{checkpoint_ref}/{operation}"
                )
            else:  # pragma: no cover - registration invariant
                raise AdapterClientError("unsupported Cube Adapter operation")
            if operation == "release":
                body = {"action": body.get("action") or "pause"}
            elif operation in {"status", "job_status", "job_cancel", "rollback"}:
                body = {}
            elif operation == "list" and not body.get("path"):
                body = {"path": "/workspace"}
            result = _request(settings, path, body)
            return json.dumps(result, ensure_ascii=False)
        except Exception as error:
            if isinstance(error, AdapterClientError):
                message = str(error)
            else:
                message = "Cube Adapter request failed"
            return json.dumps({"error": message}, ensure_ascii=False)

    return run


def register(ctx: Any) -> None:
    """Register CubeSandbox execution, file, job, and checkpoint tools."""

    settings = _settings(ctx)

    def configured() -> bool:
        try:
            _bearer_token(settings)
            return True
        except AdapterClientError:
            return False

    for name, operation in (
        ("cube_exec", "exec"),
        ("cube_status", "status"),
        ("cube_read", "read"),
        ("cube_write", "write"),
        ("cube_list", "list"),
        ("cube_job_start", "job_start"),
        ("cube_job_status", "job_status"),
        ("cube_job_output", "job_output"),
        ("cube_job_cancel", "job_cancel"),
        ("cube_checkpoint", "checkpoint"),
        ("cube_rollback", "rollback"),
        ("cube_fork", "fork"),
        ("cube_release", "release"),
    ):
        ctx.register_tool(
            name=name,
            toolset="cube-adapter",
            schema=TOOL_SCHEMAS[name],
            handler=_handler(settings, operation),
            check_fn=configured,
        )
