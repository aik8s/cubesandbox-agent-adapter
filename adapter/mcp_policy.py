"""Governed sandbox MCP tools: fixed servers, local schemas, bounded results."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import requests
import yaml
from jsonschema import Draft202012Validator

from .backend_config import NAME_RE, REFERENCE_RE, BackendConfig, resolve_references
from .task_config import _check_schema


@dataclass(frozen=True)
class McpPolicy:
    name: str
    servers: Mapping[str, Any] = field(repr=False)
    tools: Mapping[str, Any]
    timeout_seconds: int = 15
    max_input_bytes: int = 16384
    max_output_bytes: int = 65536

    def launch_config(self) -> dict[str, Any]:
        result = {}
        for name, value in self.servers.items():
            env = resolve_references(value.get("env_vars", {}))
            if not all(isinstance(key, str) and isinstance(item, str) for key, item in env.items()):
                raise RuntimeError("MCP environment must resolve to strings")
            result[name] = {"runCmd": value["run_cmd"], "envs": env}
        return result

    def validate(self, name: Any, arguments: Any) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self.tools:
            raise ValueError("MCP tool is not allowed")
        if not isinstance(arguments, dict):
            raise ValueError("MCP arguments must be an object")
        encoded = json.dumps(arguments, ensure_ascii=False).encode()
        if len(encoded) > self.max_input_bytes:
            raise ValueError("MCP arguments exceed the policy size limit")
        if next(Draft202012Validator(self.tools[name]).iter_errors(arguments), None):
            raise ValueError("MCP arguments do not match the operator-owned schema")
        return arguments

    def secret_values(self) -> list[str]:
        secrets = []
        for config in self.servers.values():
            for key, raw in config.get("env_vars", {}).items():
                if (isinstance(raw, str) and REFERENCE_RE.fullmatch(raw)) or re.search(
                    r"TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL", key, re.IGNORECASE
                ):
                    value = resolve_references(raw)
                    if isinstance(value, str) and value:
                        secrets.append(value)
        return secrets


def load_mcp_policies(path: str | None) -> dict[str, McpPolicy]:
    if not path:
        return {}
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError("MCP policy catalog cannot be loaded") from error
    if (
        not isinstance(document, Mapping)
        or set(document) != {"policies"}
        or not isinstance(document["policies"], Mapping)
    ):
        raise RuntimeError("MCP policy catalog must contain policies")
    result = {}
    for name, value in document["policies"].items():
        if (
            not isinstance(name, str)
            or not NAME_RE.fullmatch(name)
            or not isinstance(value, Mapping)
        ):
            raise RuntimeError("invalid MCP policy")
        if set(value) - {
            "servers",
            "tools",
            "timeout_seconds",
            "max_input_bytes",
            "max_output_bytes",
        }:
            raise RuntimeError("MCP policy contains unsupported fields")
        servers, tools = value.get("servers"), value.get("tools")
        if (
            not isinstance(servers, Mapping)
            or not servers
            or not isinstance(tools, Mapping)
            or not tools
        ):
            raise RuntimeError("MCP policies require explicit servers and tool schemas")
        for server, config in servers.items():
            if (
                not isinstance(server, str)
                or not NAME_RE.fullmatch(server)
                or not isinstance(config, Mapping)
                or set(config) - {"run_cmd", "env_vars"}
            ):
                raise RuntimeError("invalid operator-owned MCP server")
            if not isinstance(config.get("run_cmd"), str) or not config["run_cmd"].strip():
                raise RuntimeError("MCP server requires a fixed run_cmd")
            env = config.get("env_vars", {})
            if not isinstance(env, Mapping) or any(
                not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                for key in env
            ):
                raise RuntimeError("invalid MCP server environment")
        schemas = {}
        for tool, schema in tools.items():
            if (
                not isinstance(tool, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool)
                or not isinstance(schema, Mapping)
            ):
                raise RuntimeError("invalid MCP tool schema")
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise RuntimeError("MCP input schemas must be closed objects")
            schemas[tool] = _check_schema("MCP tool", schema)
        bounds = {}
        for key, default, maximum in (
            ("timeout_seconds", 15, 60),
            ("max_input_bytes", 16384, 65536),
            ("max_output_bytes", 65536, 1048576),
        ):
            number = value.get(key, default)
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or not 1 <= number <= maximum
            ):
                raise RuntimeError("MCP policy limit is invalid")
            bounds[key] = number
        result[name] = McpPolicy(name, dict(servers), schemas, **bounds)
    return result


class _GatewaySession:
    def __init__(
        self,
        backend: BackendConfig,
        sandbox: Any,
        token: str,
        traffic_token: str | None,
        policy: McpPolicy,
    ) -> None:
        host = backend.proxy_host
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        if not host:
            raise RuntimeError("MCP requires a configured CubeProxy host")
        self.url = f"http://{host}:{backend.proxy_port}/mcp"
        self.policy = policy
        self.session = requests.Session()
        self.session.trust_env = False
        self.headers = {
            "Host": sandbox.get_host(50005),
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if traffic_token:
            self.headers["cube-traffic-access-token"] = traffic_token
        if backend.api_key:
            self.headers["X-API-Key"] = backend.api_key
        self.sequence = 0
        self.deadline = time.monotonic() + policy.timeout_seconds

    def request(
        self, method: str, params: dict | None = None, *, notification: bool = False
    ) -> dict:
        self.sequence += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.sequence
        if params is not None:
            payload["params"] = params
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("MCP deadline exceeded")
        try:
            with self.session.post(
                self.url,
                json=payload,
                headers=self.headers,
                timeout=(min(remaining, 3), remaining),
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code >= 300:
                    raise RuntimeError("MCP gateway rejected the request")
                if response.headers.get("Mcp-Session-Id"):
                    self.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                if notification:
                    return {}
                raw = bytearray()
                is_sse = "text/event-stream" in response.headers.get("Content-Type", "")
                for chunk in response.iter_content(1):
                    raw.extend(chunk)
                    if len(raw) > self.policy.max_output_bytes or time.monotonic() > self.deadline:
                        raise RuntimeError("MCP response exceeded policy limits")
                    if is_sse and raw.endswith((b"\n\n", b"\r\n\r\n")):
                        lines = raw.decode("utf-8").splitlines()
                        data = "\n".join(
                            line[5:].lstrip() for line in lines if line.startswith("data:")
                        )
                        if data:
                            value = json.loads(data)
                            if value.get("id") == self.sequence:
                                raw = bytearray(data.encode())
                                break
                        raw.clear()
                text = raw.decode("utf-8")
                value = json.loads(text)
                if (
                    value.get("id") != self.sequence
                    or "error" in value
                    or not isinstance(value.get("result"), dict)
                ):
                    raise RuntimeError("MCP gateway returned an invalid response")
                return value["result"]
        except (requests.RequestException, ValueError, StopIteration, UnicodeError) as error:
            raise RuntimeError("MCP gateway request failed") from error

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "cube-adapter", "version": "1"},
            },
        )
        self.headers["MCP-Protocol-Version"] = "2025-03-26"
        self.request("notifications/initialized", notification=True)

    def close(self) -> None:
        if "Mcp-Session-Id" in self.headers:
            try:
                self.session.delete(
                    self.url, headers=self.headers, timeout=1, allow_redirects=False
                )
            except requests.RequestException:
                pass
        self.session.close()


class GatewayClient:
    """Killable transport worker bounds the entire call, including slow/drip SSE.

    Credentials travel only through an anonymous pipe, never process argv.
    A timeout leaves an unknown external outcome; callers must not retry it.
    """

    def __init__(
        self,
        backend: BackendConfig,
        sandbox: Any,
        token: str,
        traffic_token: str | None,
        policy: McpPolicy,
    ) -> None:
        session = _GatewaySession(backend, sandbox, token, traffic_token, policy)
        self.descriptor = {
            "url": session.url,
            "headers": session.headers,
            "timeout_seconds": policy.timeout_seconds,
            "max_output_bytes": policy.max_output_bytes,
        }
        session.session.close()
        self.policy = policy

    def initialize(self) -> None:
        # Initialization and tool call occur in one deadline-bounded child.
        return

    def request(self, method: str, params: dict) -> dict:
        if method != "tools/call":
            raise RuntimeError("unsupported governed MCP operation")
        payload = {**self.descriptor, "params": params}
        process = subprocess.Popen(
            [sys.executable, "-m", "adapter.mcp_transport"],  # noqa: S603 -- fixed private transport worker
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "LANG", "LC_ALL", "PYTHONPATH"}
            },
        )
        try:
            output, _ = process.communicate(
                json.dumps(payload).encode(), timeout=self.policy.timeout_seconds
            )
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate()
            raise RuntimeError("MCP total deadline exceeded; outcome unknown") from error
        if process.returncode or len(output) > self.policy.max_output_bytes:
            raise RuntimeError("MCP transport failed or result exceeded limits")
        value = json.loads(output)
        if not isinstance(value, dict):
            raise RuntimeError("MCP transport returned an invalid result")
        return value

    def close(self) -> None:
        return
