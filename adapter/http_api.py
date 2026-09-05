"""HTTP/JSON and SSE transport for the Cube Adapter core."""

from __future__ import annotations

import json
import re
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .config import AdapterConfig, AuthContext
from .core import MAX_BODY_BYTES, VERSION, AdapterError, CubeAdapter

ALLOWED_FIELDS: Dict[str, frozenset[str]] = {
    "acquire": frozenset({"runtime", "session_key", "profile", "request_id"}),
    "exec": frozenset({"command", "cwd", "timeout_ms", "request_id"}),
    "read": frozenset({"path", "request_id"}),
    "write": frozenset({"path", "content", "request_id"}),
    "list": frozenset({"path", "request_id"}),
    "stat": frozenset({"path", "request_id"}),
    "mkdir": frozenset({"path", "request_id"}),
    "remove": frozenset({"path", "request_id"}),
    "move": frozenset({"path", "destination", "request_id"}),
    "artifact_upload": frozenset({"path", "content_base64", "request_id"}),
    "artifact_download": frozenset({"path", "request_id"}),
    "release": frozenset({"action", "request_id"}),
    "status": frozenset({"refresh", "request_id"}),
    "job_start": frozenset({"command", "cwd", "request_id"}),
    "job_output": frozenset({"offset", "max_bytes", "request_id"}),
    "job_cancel": frozenset({"request_id"}),
    "pty_create": frozenset({"rows", "cols", "cwd", "request_id"}),
    "pty_input": frozenset({"data", "request_id"}),
    "pty_resize": frozenset({"rows", "cols", "request_id"}),
    "pty_kill": frozenset({"request_id"}),
    "checkpoint_create": frozenset({"name", "request_id"}),
    "checkpoint_action": frozenset({"branch", "request_id"}),
    "task_plan": frozenset({"template", "parameters", "request_id"}),
    "task_approve": frozenset({"decision", "reason", "request_id"}),
    "empty": frozenset({"request_id"}),
}


class AdapterHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def make_handler(adapter: CubeAdapter) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CubeAdapter/" + VERSION
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/healthz":
                    self._json(200, adapter.health())
                    return
                if path == "/readyz":
                    status, value = adapter.readiness()
                    self._json(status, value)
                    return
                if path == "/metrics":
                    self._bytes(
                        200,
                        adapter.metrics_payload(),
                        "text/plain; version=0.0.4; charset=utf-8",
                    )
                    return
                if path == "/audit":
                    auth = self._auth(adapter)
                    if not auth.is_admin:
                        raise AdapterError(403, "admin_required", "admin role required")
                    self._bytes(
                        200,
                        adapter.audit_html().encode("utf-8"),
                        "text/html; charset=utf-8",
                    )
                    return

                auth = self._auth(adapter)
                if path == "/v1/leases":
                    adapter.authorize(auth, "lease:list")
                    self._json(200, adapter.list_leases(auth))
                    return
                if path == "/v1/task-templates":
                    self._json(200, adapter.list_task_templates(auth))
                    return
                match = re.fullmatch(r"/v1/task-plans/([^/]+)", path)
                if match:
                    self._json(200, adapter.task_plan_status(match.group(1), auth))
                    return
                match = re.fullmatch(r"/v1/leases/([^/]+)/status", path)
                if match:
                    adapter.authorize(auth, "lease:status")
                    self._json(200, adapter.lease_status(match.group(1), {}, auth))
                    return
                match = re.fullmatch(r"/v1/leases/([^/]+)/checkpoints", path)
                if match:
                    adapter.authorize(auth, "checkpoint:list")
                    self._json(200, adapter.checkpoint_list(match.group(1), auth))
                    return
                match = re.fullmatch(r"/v1/jobs/([^/]+)/status", path)
                if match:
                    adapter.authorize(auth, "job:status")
                    self._json(200, adapter.job_status(match.group(1), auth))
                    return
                match = re.fullmatch(r"/v1/jobs/([^/]+)/events", path)
                if match:
                    adapter.authorize(auth, "job:output")
                    self._sse(
                        adapter.iter_job_events(match.group(1), auth, timeout=self._sse_timeout(parsed.query))
                    )
                    return
                match = re.fullmatch(r"/v1/ptys/([^/]+)/status", path)
                if match:
                    adapter.authorize(auth, "pty:status")
                    self._json(200, adapter.pty_status(match.group(1), auth))
                    return
                match = re.fullmatch(r"/v1/ptys/([^/]+)/events", path)
                if match:
                    adapter.authorize(auth, "pty:events")
                    self._sse(
                        adapter.iter_pty_events(match.group(1), auth, timeout=self._sse_timeout(parsed.query))
                    )
                    return
                match = re.fullmatch(r"/v1/tasks/([^/]+)/status", path)
                if match:
                    self._json(200, adapter.task_status(match.group(1), auth))
                    return
                match = re.fullmatch(r"/v1/tasks/([^/]+)/receipt", path)
                if match:
                    self._json(200, adapter.task_receipt(match.group(1), auth))
                    return
                raise AdapterError(404, "not_found", "route not found")
            except AdapterError as error:
                self._error(error)
            except TimeoutError:
                self._error(AdapterError(504, "timeout", "operation timed out"))
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as error:
                self._upstream_error(error)

        def do_POST(self) -> None:  # noqa: N802
            try:
                auth = self._auth(adapter)
                path = urlparse(self.path).path
                if path == "/v1/leases/acquire":
                    body = self._body("acquire")
                    adapter.authorize(auth, "lease:acquire")
                    self._json(200, adapter.acquire(body, auth))
                    return
                if path == "/v1/admin/gc":
                    body = self._body("empty")
                    del body
                    adapter.authorize(auth, "admin:gc")
                    self._json(200, adapter.force_gc(auth))
                    return
                if path == "/v1/tasks/plan":
                    self._json(201, adapter.task_plan(self._body("task_plan"), auth))
                    return
                match = re.fullmatch(r"/v1/task-plans/([^/]+)/(approve|submit)", path)
                if match:
                    plan_ref, action = match.groups()
                    body = self._body("task_approve" if action == "approve" else "empty")
                    value = (
                        adapter.task_approve(plan_ref, body, auth)
                        if action == "approve"
                        else adapter.task_submit(plan_ref, body, auth)
                    )
                    self._json(202 if action == "submit" else 200, value)
                    return
                match = re.fullmatch(
                    r"/v1/tasks/([^/]+)/(status|result|cancel|receipt)", path
                )
                if match:
                    task_ref, action = match.groups()
                    body = self._body("empty")
                    if action == "status":
                        value = adapter.task_status(task_ref, auth)
                    elif action == "result":
                        value = adapter.task_result(task_ref, auth)
                    elif action == "cancel":
                        value = adapter.task_cancel(task_ref, body, auth)
                    else:
                        value = adapter.task_receipt(task_ref, auth)
                    self._json(200, value)
                    return

                match = re.fullmatch(
                    r"/v1/leases/([^/]+)/(exec|read|write|release|status|list|stat|mkdir|remove|move)",
                    path,
                )
                if match:
                    lease_ref, action = match.groups()
                    body = self._body(action)
                    adapter.authorize(
                        auth,
                        {
                            "exec": "exec:run",
                            "read": "file:read",
                            "write": "file:write",
                            "release": "lease:release",
                            "status": "lease:status",
                            "list": "file:list",
                            "stat": "file:stat",
                            "mkdir": "file:mkdir",
                            "remove": "file:remove",
                            "move": "file:move",
                        }[action],
                    )
                    handlers: Dict[
                        str, Callable[[str, Dict[str, Any], AuthContext], Dict[str, Any]]
                    ] = {
                        "exec": adapter.exec,
                        "read": adapter.read,
                        "write": adapter.write,
                        "release": adapter.release,
                        "status": adapter.lease_status,
                        "list": adapter.list_files,
                        "stat": adapter.stat_file,
                        "mkdir": adapter.make_dir,
                        "remove": adapter.remove_file,
                        "move": adapter.move_file,
                    }
                    self._json(200, handlers[action](lease_ref, body, auth))
                    return

                match = re.fullmatch(
                    r"/v1/leases/([^/]+)/artifacts/(upload|download)", path
                )
                if match:
                    lease_ref, action = match.groups()
                    adapter.authorize(auth, "artifact:" + action)
                    key = "artifact_" + action
                    body = self._body(key)
                    handler = (
                        adapter.artifact_upload
                        if action == "upload"
                        else adapter.artifact_download
                    )
                    self._json(200, handler(lease_ref, body, auth))
                    return

                match = re.fullmatch(r"/v1/leases/([^/]+)/jobs", path)
                if match:
                    body = self._body("job_start")
                    adapter.authorize(auth, "job:start")
                    self._json(202, adapter.job_start(match.group(1), body, auth))
                    return
                match = re.fullmatch(r"/v1/jobs/([^/]+)/(status|output|cancel)", path)
                if match:
                    job_ref, action = match.groups()
                    adapter.authorize(auth, "job:" + action)
                    body = self._body(
                        "job_output" if action == "output" else "job_cancel" if action == "cancel" else "empty"
                    )
                    if action == "status":
                        value = adapter.job_status(job_ref, auth)
                    elif action == "output":
                        value = adapter.job_output(job_ref, body, auth)
                    else:
                        value = adapter.job_cancel(job_ref, body, auth)
                    self._json(200, value)
                    return

                match = re.fullmatch(r"/v1/leases/([^/]+)/ptys", path)
                if match:
                    body = self._body("pty_create")
                    adapter.authorize(auth, "pty:create")
                    self._json(201, adapter.pty_create(match.group(1), body, auth))
                    return
                match = re.fullmatch(r"/v1/ptys/([^/]+)/(input|resize|kill)", path)
                if match:
                    pty_ref, action = match.groups()
                    adapter.authorize(auth, "pty:" + action)
                    body = self._body("pty_" + action)
                    pty_handlers: Dict[
                        str, Callable[[str, Dict[str, Any], AuthContext], Dict[str, Any]]
                    ] = {
                        "input": adapter.pty_input,
                        "resize": adapter.pty_resize,
                        "kill": adapter.pty_kill,
                    }
                    self._json(200, pty_handlers[action](pty_ref, body, auth))
                    return

                match = re.fullmatch(r"/v1/leases/([^/]+)/checkpoints", path)
                if match:
                    body = self._body("checkpoint_create")
                    adapter.authorize(auth, "checkpoint:create")
                    self._json(201, adapter.checkpoint_create(match.group(1), body, auth))
                    return
                match = re.fullmatch(
                    r"/v1/leases/([^/]+)/checkpoints/([^/]+)/(rollback|fork|delete)",
                    path,
                )
                if match:
                    lease_ref, checkpoint_ref, action = match.groups()
                    adapter.authorize(auth, "checkpoint:" + action)
                    body = self._body("checkpoint_action")
                    checkpoint_handlers: Dict[
                        str,
                        Callable[
                            [str, str, Dict[str, Any], AuthContext], Dict[str, Any]
                        ],
                    ] = {
                        "rollback": adapter.checkpoint_rollback,
                        "fork": adapter.checkpoint_fork,
                        "delete": adapter.checkpoint_delete,
                    }
                    self._json(
                        201 if action == "fork" else 200,
                        checkpoint_handlers[action](lease_ref, checkpoint_ref, body, auth),
                    )
                    return
                raise AdapterError(404, "not_found", "route not found")
            except AdapterError as error:
                self._error(error)
            except TimeoutError:
                self._error(AdapterError(504, "timeout", "operation timed out"))
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as error:
                self._upstream_error(error)

        def _auth(self, target: CubeAdapter) -> AuthContext:
            return target.authenticate(
                self.headers.get("Authorization"), peer_subject=self._peer_subject()
            )

        def _peer_subject(self) -> Optional[str]:
            getpeercert = getattr(self.connection, "getpeercert", None)
            if getpeercert is None:
                return None
            try:
                cert = getpeercert()
            except Exception:
                return None
            subject = cert.get("subject", ()) if isinstance(cert, dict) else ()
            parts = []
            for group in subject:
                for key, value in group:
                    parts.append(f"{key}={value}")
            return ",".join(parts) or None

        def _body(self, schema: str) -> Dict[str, Any]:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise AdapterError(400, "invalid_body", "invalid Content-Length") from error
            if size <= 0 or size > MAX_BODY_BYTES:
                raise AdapterError(413, "invalid_body", "request body is empty or too large")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                raise AdapterError(415, "unsupported_media_type", "Content-Type must be application/json")
            try:
                value = json.loads(self.rfile.read(size))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AdapterError(400, "invalid_json", "request body must be JSON") from error
            if not isinstance(value, dict):
                raise AdapterError(400, "invalid_json", "request body must be a JSON object")
            unknown = set(value) - ALLOWED_FIELDS[schema]
            if unknown:
                raise AdapterError(
                    400,
                    "unknown_fields",
                    "request contains unsupported fields: " + ", ".join(sorted(unknown)),
                )
            return value

        @staticmethod
        def _sse_timeout(query: str) -> float:
            values = parse_qs(query)
            try:
                return min(900.0, max(1.0, float(values.get("timeout", ["300"])[0])))
            except ValueError as error:
                raise AdapterError(400, "invalid_timeout", "timeout query is invalid") from error

        def _sse(self, events: Any) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            for item in events:
                payload = json.dumps(item["data"], ensure_ascii=False)
                self.wfile.write(
                    f"event: {item['event']}\ndata: {payload}\n\n".encode("utf-8")
                )
                self.wfile.flush()
            self.close_connection = True

        def _upstream_error(self, error: Exception) -> None:
            self._error(
                AdapterError(
                    502,
                    "execution_failed",
                    f"CubeSandbox operation failed ({type(error).__name__})",
                )
            )

        def _error(self, error: AdapterError) -> None:
            self._json(
                error.status,
                {"error": {"code": error.code, "message": error.message}},
            )

        def _json(self, status: int, value: Dict[str, Any]) -> None:
            self._bytes(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _bytes(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def build_server(config: AdapterConfig, adapter: CubeAdapter) -> AdapterHttpServer:
    server = AdapterHttpServer((config.bind, config.port), make_handler(adapter))
    if config.tls_cert_file and config.tls_key_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config.tls_cert_file, config.tls_key_file)
        if config.tls_client_ca_file:
            context.load_verify_locations(config.tls_client_ca_file)
            context.verify_mode = ssl.CERT_REQUIRED
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
