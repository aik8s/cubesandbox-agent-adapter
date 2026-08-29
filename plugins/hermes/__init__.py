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
    if profile != "offline-code":
        raise ValueError("only the offline-code profile is supported")
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
            if operation == "release":
                body = {"action": body.get("action") or "pause"}
            result = _request(
                settings,
                f"/v1/leases/{lease['lease_ref']}/{operation}",
                body,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as error:
            if isinstance(error, AdapterClientError):
                message = str(error)
            else:
                message = "Cube Adapter request failed"
            return json.dumps({"error": message}, ensure_ascii=False)

    return run


def register(ctx: Any) -> None:
    """Register four CubeSandbox tools with Hermes Agent."""

    settings = _settings(ctx)

    def configured() -> bool:
        try:
            _bearer_token(settings)
            return True
        except AdapterClientError:
            return False

    for name, operation in (
        ("cube_exec", "exec"),
        ("cube_read", "read"),
        ("cube_write", "write"),
        ("cube_release", "release"),
    ):
        ctx.register_tool(
            name=name,
            toolset="cube-adapter",
            schema=TOOL_SCHEMAS[name],
            handler=_handler(settings, operation),
            check_fn=configured,
        )
