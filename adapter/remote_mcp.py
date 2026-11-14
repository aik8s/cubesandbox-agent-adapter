"""Bearer-authenticated MCP Streamable HTTP facade over the Adapter API.

Each request is authenticated by the Adapter and its bearer token is forwarded
only for that request. The facade never owns a privileged shared Adapter token.
"""

from __future__ import annotations

import argparse
import contextvars
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from .mcp_server import AdapterClientError, AdapterHttpClient, build_mcp_server

_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("mcp_bearer", default=None)


class RequestAdapterClient:
    """Create a bounded Adapter client from the current authenticated request."""

    def __init__(self, base_url: str, ca_file: Optional[str] = None) -> None:
        # Validate the target once without allowing a shared credential or client cert.
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("Adapter URL must not contain credentials, query, or fragment")
        AdapterHttpClient(base_url, "validation-only", ca_file=ca_file)
        self.base_url = base_url
        self.ca_file = ca_file

    @staticmethod
    def segment(value: str) -> str:
        return AdapterHttpClient.segment(value)

    def _client(self, token: str) -> AdapterHttpClient:
        return AdapterHttpClient(self.base_url, token, ca_file=self.ca_file, timeout=310)

    def verify(self, token: str) -> int:
        client = self._client(token)
        request = urllib.request.Request(
            client.base_url + "/v1/identity", headers={"Authorization": "Bearer " + token}
        )
        try:
            with urllib.request.urlopen(request, timeout=5, context=client.ssl_context) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return 401 if error.code in {401, 403} else 503
        except (urllib.error.URLError, TimeoutError, OSError):
            return 503

    def post(self, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        token = _token.get()
        if not token:
            raise RuntimeError("authenticated MCP request required")
        try:
            return self._client(token).post(path, body)
        except AdapterClientError as error:
            code = str(error).partition(":")[0]
            if not code.replace("_", "").isalnum() or len(code) > 64:
                code = "adapter_unavailable"
            raise ToolError(code) from None


class AuthenticatedMcp:
    def __init__(self, app: Any, client: RequestAdapterClient) -> None:
        self.app = app
        self.client = client

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
        if len(headers) != 1 or len(headers[0]) > 8192:
            await self._reject(scope, receive, send, 401)
            return
        try:
            value = headers[0].decode("ascii")
        except UnicodeDecodeError:
            await self._reject(scope, receive, send, 401)
            return
        if not value.startswith("Bearer ") or not value[7:] or any(c.isspace() for c in value[7:]):
            await self._reject(scope, receive, send, 401)
            return
        bearer = value[7:]
        status = await anyio.to_thread.run_sync(self.client.verify, bearer)
        if status != 200:
            await self._reject(scope, receive, send, status)
            return
        token_context = _token.set(bearer)
        try:
            await self.app(scope, receive, send)
        finally:
            _token.reset(token_context)

    @staticmethod
    async def _reject(scope: Any, receive: Any, send: Any, status: int) -> None:
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        response = JSONResponse(
            {"error": "unauthorized" if status == 401 else "adapter_unavailable"},
            status_code=status,
            headers=headers,
        )
        await response(scope, receive, send)


def make_app(
    base_url: str,
    *,
    ca_file: Optional[str] = None,
    allowed_hosts: Optional[list[str]] = None,
    allowed_origins: Optional[list[str]] = None,
) -> AuthenticatedMcp:
    client = RequestAdapterClient(base_url, ca_file)
    server = build_mcp_server(client=client)  # type: ignore[arg-type]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts or ["localhost:*", "127.0.0.1:*", "[::1]:*"],
        allowed_origins=allowed_origins or [],
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=security, max_request_body_size=1024 * 1024,
    )
    return AuthenticatedMcp(app, client)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remote MCP facade with per-request Adapter identity")
    parser.add_argument("--adapter-url", default="http://127.0.0.1:18080")
    parser.add_argument("--adapter-ca-file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--allowed-host", action="append")
    parser.add_argument("--allowed-origin", action="append")
    parser.add_argument("--tls-cert-file")
    parser.add_argument("--tls-key-file")
    args = parser.parse_args()
    loopback = args.host in {"127.0.0.1", "::1", "localhost"}
    if bool(args.tls_cert_file) != bool(args.tls_key_file):
        parser.error("TLS certificate and key must be provided together")
    if not loopback and (not args.tls_cert_file or not args.allowed_host):
        parser.error("non-loopback binding requires TLS and at least one allowed Host")
    import uvicorn

    uvicorn.run(
        make_app(args.adapter_url, ca_file=args.adapter_ca_file,
                 allowed_hosts=args.allowed_host, allowed_origins=args.allowed_origin),
        host=args.host, port=args.port, ssl_certfile=args.tls_cert_file,
        ssl_keyfile=args.tls_key_file, access_log=False,
    )


if __name__ == "__main__":
    main()
