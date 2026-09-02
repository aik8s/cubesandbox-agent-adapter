"""Policy-controlled CubeSandbox execution broker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import shlex
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from cubesandbox import PtySize, Sandbox, Template, Volume, VolumeMount

from .audit import AuditManager
from .auth import Authenticator, AuthFailure
from .config import AdapterConfig, AuthContext, ProfileConfig
from .metrics import AdapterMetrics
from .state import (
    CheckpointRecord,
    JobRecord,
    LeaseRecord,
    StateStore,
    build_state_store,
)

VERSION = "0.3.0"
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_COMMAND_BYTES = 16 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_JOB_OUTPUT_BYTES = 1024 * 1024
ALLOWED_FILE_ROOTS = ("/tmp", "/workspace")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
LEASE_REF_RE = re.compile(r"^lease_[a-f0-9]{20}$")
JOB_REF_RE = re.compile(r"^job_[a-f0-9]{20}$")
PTY_REF_RE = re.compile(r"^pty_[a-f0-9]{20}$")
CHECKPOINT_REF_RE = re.compile(r"^checkpoint_[a-f0-9]{20}$")


class AdapterError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class ReadinessCache:
    checked_at: float = 0.0
    result: Optional[Dict[str, Any]] = None


def _digest(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _request_id(value: Any) -> str:
    if isinstance(value, str) and REQUEST_ID_RE.fullmatch(value):
        return value
    return uuid.uuid4().hex[:16]


def _required_string(body: Dict[str, Any], key: str, maximum: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(400, "invalid_request", f"{key} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum:
        raise AdapterError(413, "value_too_large", f"{key} exceeds the size limit")
    return value


def _bounded_string(body: Dict[str, Any], key: str, maximum: int) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise AdapterError(400, "invalid_request", f"{key} must be a string")
    if len(value.encode("utf-8")) > maximum:
        raise AdapterError(413, "value_too_large", f"{key} exceeds the size limit")
    return value


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise AdapterError(400, "invalid_path", "path must be absolute")
    candidate = PurePosixPath(value)
    if ".." in candidate.parts:
        raise AdapterError(403, "path_denied", "path traversal is not allowed")
    normalized = str(candidate)
    if not any(
        normalized == root or normalized.startswith(root + "/")
        for root in ALLOWED_FILE_ROOTS
    ):
        raise AdapterError(
            403, "path_denied", "path must remain under /workspace or /tmp"
        )
    return normalized


def _truncate(value: str, maximum: int = MAX_OUTPUT_BYTES) -> Tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value, False
    return raw[:maximum].decode("utf-8", errors="replace"), True


def _default_auth() -> AuthContext:
    return AuthContext(
        tenant_id="default",
        subject="direct-call",
        roles=frozenset({"runtime", "admin"}),
    )


class CubeAdapter:
    def __init__(
        self,
        config: AdapterConfig,
        sandbox_factory: Callable[..., Any] = Sandbox.create,
        *,
        sandbox_connector: Callable[..., Any] = Sandbox.connect,
        volume_factory: Callable[..., Any] = Volume.create,
        volume_connector: Callable[..., Any] = Volume.connect,
        volume_destroyer: Callable[..., Any] = Volume.destroy,
        template_checker: Callable[..., Any] = Template.get,
        snapshot_deleter: Callable[..., Any] = Sandbox.delete_snapshot,
        state_store: Optional[StateStore] = None,
        metrics: Optional[AdapterMetrics] = None,
        audit: Optional[AuditManager] = None,
        start_gc: bool = True,
    ) -> None:
        self.config = config
        self.profiles = config.profiles()
        self.authenticator = Authenticator(config)
        self.metrics = metrics or AdapterMetrics()
        self.state = state_store or build_state_store(
            config.state_backend_url,
            prefix=config.state_prefix,
            encryption_key=config.state_encryption_key,
        )
        self.audit = audit or AuditManager.from_config(config, self.metrics)
        self._sandbox_factory = sandbox_factory
        self._sandbox_connector = sandbox_connector
        self._volume_factory = volume_factory
        self._volume_connector = volume_connector
        self._volume_destroyer = volume_destroyer
        self._template_checker = template_checker
        self._snapshot_deleter = snapshot_deleter
        self._handles: Dict[str, Any] = {}
        self._handles_lock = threading.RLock()
        self._readiness = ReadinessCache()
        self._readiness_lock = threading.Lock()
        self._stop = threading.Event()
        self._gc_thread: Optional[threading.Thread] = None
        if start_gc:
            self._gc_thread = threading.Thread(target=self._gc_loop, daemon=True)
            self._gc_thread.start()

    # Authentication -------------------------------------------------

    def authenticate(
        self, authorization: Optional[str], *, peer_subject: Optional[str] = None
    ) -> AuthContext:
        try:
            return self.authenticator.authenticate(
                authorization, peer_subject=peer_subject
            )
        except AuthFailure as error:
            raise AdapterError(error.status, error.code, error.message) from error

    # Lease lifecycle ------------------------------------------------

    def acquire(
        self, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        started = time.perf_counter()
        runtime = _required_string(body, "runtime", 32).lower()
        if runtime not in {"openclaw", "dsh", "hermes", "mcp"}:
            raise AdapterError(400, "invalid_runtime", "unsupported runtime")
        if not auth.permits_runtime(runtime):
            raise AdapterError(403, "runtime_denied", "runtime is not allowed")
        session_key = _required_string(body, "session_key", 512)
        profile_name = body.get("profile", "offline-code")
        if not isinstance(profile_name, str) or profile_name not in self.profiles:
            raise AdapterError(403, "profile_denied", "profile is not available")
        if not auth.permits_profile(profile_name):
            raise AdapterError(403, "profile_denied", "profile is not allowed")
        profile = self.profiles[profile_name]
        if runtime not in profile.allowed_runtimes:
            raise AdapterError(403, "profile_denied", "runtime is not allowed by profile")

        session_hash = hmac.new(
            self.config.session_hmac_key.encode("utf-8"),
            f"{auth.tenant_id}\0{session_key}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
        request_id = _request_id(body.get("request_id"))
        session_lock = f"session:{auth.tenant_id}:{runtime}:{session_hash}"

        with self.state.lock(session_lock, self.config.lock_ttl_seconds):
            existing_ref = self.state.get_session(
                auth.tenant_id, runtime, session_hash
            )
            existing = self.state.get_lease(existing_ref) if existing_ref else None
            if existing and existing.state != "released":
                existing.last_used_at = time.time()
                existing.version += 1
                self.state.put_lease(existing)
                result = self._lease_result(existing, reused=True)
                self._audit(existing, "acquire", request_id, "ok", started, reused=True)
                return result

            active = [
                record
                for record in self.state.list_leases(auth.tenant_id)
                if record.state != "released"
            ]
            if len(active) >= profile.max_active_leases_per_tenant:
                raise AdapterError(
                    429,
                    "lease_quota_exceeded",
                    "tenant active lease quota has been reached",
                )

            volume, volume_id, volume_owned, volume_mounts = self._workspace(
                profile, auth, session_hash
            )
            del volume
            create_args: Dict[str, Any] = {
                "template": profile.template,
                "timeout": profile.sandbox_timeout_seconds,
                "lifecycle": profile.lifecycle,
                "allow_internet_access": profile.allow_internet_access,
                "network": profile.network,
                "metadata": {
                    "runtime": runtime,
                    "purpose": "agent-adapter",
                    "tenant": _digest(auth.tenant_id, 16),
                    "session": session_hash,
                    "policy": profile.name,
                },
            }
            if profile.distribution_scope:
                create_args["distribution_scope"] = list(profile.distribution_scope)
            if volume_mounts:
                create_args["volume_mounts"] = volume_mounts

            try:
                sandbox = self._sandbox_factory(**create_args)
            except Exception:
                if volume_owned and volume_id and not profile.workspace.retain_on_kill:
                    try:
                        self._volume_destroyer(volume_id)
                    except Exception:
                        pass
                raise
            lease = LeaseRecord(
                lease_ref=f"lease_{uuid.uuid4().hex[:20]}",
                tenant_id=auth.tenant_id,
                runtime=runtime,
                session_hash=session_hash,
                profile=profile.name,
                sandbox_id=str(sandbox.sandbox_id),
                sandbox_ref=str(sandbox.sandbox_id)[:8],
                traffic_access_token=getattr(sandbox, "traffic_access_token", None),
                volume_id=volume_id,
                volume_owned=volume_owned,
            )
            self.state.put_lease(lease)
            with self._handles_lock:
                self._handles[lease.lease_ref] = sandbox
            result = self._lease_result(lease, reused=False)
            self._audit(lease, "acquire", request_id, "ok", started, reused=False)
            self._refresh_gauges()
            return result

    def lease_status(
        self,
        lease_ref: str,
        body: Optional[Dict[str, Any]] = None,
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        body = body or {}
        record = self._lease(lease_ref, auth)
        live_state = record.state
        if bool(body.get("refresh", True)) and record.state != "released":
            try:
                sandbox = self._sandbox(record)
                info = sandbox.get_info()
                live_state = str(info.get("state") or record.state)
                record.state = live_state
                record.last_used_at = time.time()
                self.state.put_lease(record)
            except Exception as error:
                record.last_error = type(error).__name__
                self.state.put_lease(record)
        profile = self.profiles[record.profile]
        return {
            "executor": "cubesandbox-microvm",
            "lease_ref": record.lease_ref,
            "sandbox_ref": record.sandbox_ref,
            "runtime": record.runtime,
            "profile": record.profile,
            "state": live_state,
            "created_at": _timestamp(record.created_at),
            "last_used_at": _timestamp(record.last_used_at),
            "expires_in_seconds": max(
                0, int(profile.lease_idle_ttl_seconds - (time.time() - record.last_used_at))
            ),
            "volume_attached": bool(record.volume_id),
            "jobs": len(self.state.list_jobs(lease_ref=record.lease_ref)),
            "checkpoints": len(self.state.list_checkpoints(record.lease_ref)),
            "recoverable": self.state.durable,
        }

    def list_leases(self, auth: Optional[AuthContext] = None) -> Dict[str, Any]:
        auth = auth or _default_auth()
        tenant = None if auth.is_admin else auth.tenant_id
        records = sorted(
            self.state.list_leases(tenant), key=lambda item: item.created_at, reverse=True
        )
        return {
            "leases": [
                {
                    "lease_ref": item.lease_ref,
                    "sandbox_ref": item.sandbox_ref,
                    "tenant": item.tenant_id if auth.is_admin else None,
                    "runtime": item.runtime,
                    "profile": item.profile,
                    "state": item.state,
                    "created_at": _timestamp(item.created_at),
                    "last_used_at": _timestamp(item.last_used_at),
                }
                for item in records
            ]
        }

    def release(
        self,
        lease_ref: str,
        body: Dict[str, Any],
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth, allow_released=True)
        action = body.get("action", "pause")
        if action not in {"pause", "kill"}:
            raise AdapterError(400, "invalid_action", "action must be pause or kill")
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            if action == "pause":
                sandbox = self._sandbox(record)
                sandbox.pause(wait=True)
                record.state = "paused"
                record.last_used_at = time.time()
                record.version += 1
                self.state.put_lease(record)
            else:
                if record.state != "released":
                    self._sandbox(record).kill()
                    record.state = "released"
                    record.last_used_at = time.time()
                    record.version += 1
                    self.state.put_lease(record)
                    with self._handles_lock:
                        self._handles.pop(record.lease_ref, None)
                try:
                    for checkpoint in self.state.list_checkpoints(record.lease_ref):
                        self._snapshot_deleter(checkpoint.snapshot_id)
                        self.state.delete_checkpoint(checkpoint)
                except Exception as error:
                    self._audit(
                        record,
                        "release",
                        request_id,
                        "error",
                        started,
                        release_action=action,
                        cleanup_pending=True,
                    )
                    self._refresh_gauges()
                    raise AdapterError(
                        502,
                        "checkpoint_cleanup_failed",
                        "sandbox stopped but checkpoint cleanup must be retried",
                    ) from error
                self._destroy_owned_volume(record)
                self.state.delete_lease(record)
        self._audit(record, "release", request_id, "ok", started, release_action=action)
        self._refresh_gauges()
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            "action": action,
        }

    # Synchronous execution -----------------------------------------

    def exec(
        self,
        lease_ref: str,
        body: Dict[str, Any],
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        command = _required_string(body, "command", MAX_COMMAND_BYTES)
        cwd = body.get("cwd")
        if cwd is not None:
            cwd = _safe_path(cwd)
        profile = self.profiles[record.profile]
        timeout_ms = body.get("timeout_ms", 60_000)
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            raise AdapterError(400, "invalid_timeout", "timeout_ms must be a positive integer")
        timeout_ms = min(timeout_ms, profile.max_command_seconds * 1000)
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        try:
            with self._lease_lock(record, profile.max_command_seconds + 60):
                sandbox = self._sandbox(record)
                result = sandbox.commands.run(
                    command, cwd=cwd, timeout=timeout_ms / 1000
                )
                self._touch(record, "running")
            stdout, stdout_truncated = _truncate(result.stdout or "")
            stderr, stderr_truncated = _truncate(result.stderr or "")
            response = {
                "executor": "cubesandbox-microvm",
                "sandbox_ref": record.sandbox_ref,
                "request_id": request_id,
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
            self._audit(
                record,
                "exec",
                request_id,
                "ok",
                started,
                command_sha256=_digest(command, 16),
                exit_code=result.exit_code,
            )
            return response
        except Exception as error:
            self._audit(
                record,
                "exec",
                request_id,
                "error",
                started,
                command_sha256=_digest(command, 16),
                error_type=type(error).__name__,
            )
            raise

    # Safe filesystem and artifacts ---------------------------------

    def read(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        return self._file_operation("read", lease_ref, body, auth)

    def write(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        return self._file_operation("write", lease_ref, body, auth)

    def list_files(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        return self._file_operation("list", lease_ref, body, auth)

    def stat_file(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        return self._file_operation("stat", lease_ref, body, auth)

    def make_dir(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        return self._file_operation("mkdir", lease_ref, body, auth)

    def remove_file(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        return self._file_operation("remove", lease_ref, body, auth)

    def move_file(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        return self._file_operation("move", lease_ref, body, auth)

    def _file_operation(
        self,
        action: str,
        lease_ref: str,
        body: Dict[str, Any],
        auth: Optional[AuthContext],
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        path = _safe_path(body.get("path"))
        destination = _safe_path(body.get("destination")) if action == "move" else None
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            files = self._sandbox(record).files
            if action == "read":
                content = files.read(path)
                content, truncated = _truncate(content)
                result: Dict[str, Any] = {"path": path, "content": content, "truncated": truncated}
            elif action == "write":
                content = _bounded_string(body, "content", MAX_FILE_BYTES)
                files.write(path, content)
                result = {"path": path, "bytes": len(content.encode("utf-8"))}
            elif action == "list":
                entries = files.list(path)
                result = {"path": path, "entries": _redact_entries(entries)}
            elif action == "stat":
                result = {"path": path, "entry": _redact_entry(files.stat(path))}
            elif action == "mkdir":
                result = {"path": path, "entry": _redact_entry(files.make_dir(path))}
            elif action == "remove":
                files.remove(path)
                result = {"path": path, "removed": True}
            elif action == "move":
                assert destination is not None
                result = {
                    "path": path,
                    "destination": destination,
                    "entry": _redact_entry(files.rename(path, destination)),
                }
            else:  # pragma: no cover - internal invariant
                raise AssertionError(action)
            self._touch(record, "running")
        self._audit(
            record,
            action,
            request_id,
            "ok",
            started,
            path_sha256=_digest(path, 16),
            **(
                {"destination_sha256": _digest(destination, 16)}
                if destination is not None
                else {}
            ),
        )
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            **result,
        }

    def artifact_upload(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        path = _safe_path(body.get("path"))
        encoded = _required_string(
            body, "content_base64", (MAX_ARTIFACT_BYTES * 4 // 3) + 16
        )
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise AdapterError(400, "invalid_base64", "content_base64 is invalid") from error
        if len(content) > MAX_ARTIFACT_BYTES:
            raise AdapterError(413, "artifact_too_large", "artifact exceeds the size limit")
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            self._sandbox(record).files.write(path, content)
            self._touch(record, "running")
        digest = hashlib.sha256(content).hexdigest()
        self._audit(
            record,
            "artifact_upload",
            request_id,
            "ok",
            started,
            path_sha256=_digest(path, 16),
            content_sha256=digest,
            bytes=len(content),
        )
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            "path": path,
            "bytes": len(content),
            "sha256": digest,
        }

    def artifact_download(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        path = _safe_path(body.get("path"))
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            sandbox = self._sandbox(record)
            entry = sandbox.files.stat(path)
            size = _entry_size(entry)
            if size is not None and size > MAX_ARTIFACT_BYTES:
                raise AdapterError(413, "artifact_too_large", "artifact exceeds the size limit")
            command = f"base64 -w0 -- {shlex.quote(path)}"
            result = sandbox.commands.run(command, timeout=60)
            if result.exit_code != 0:
                raise AdapterError(404, "artifact_not_found", "artifact could not be read")
            try:
                content = base64.b64decode(result.stdout, validate=True)
            except ValueError as error:
                raise AdapterError(502, "invalid_artifact", "sandbox returned invalid data") from error
            if len(content) > MAX_ARTIFACT_BYTES:
                raise AdapterError(413, "artifact_too_large", "artifact exceeds the size limit")
            self._touch(record, "running")
        digest = hashlib.sha256(content).hexdigest()
        self._audit(
            record,
            "artifact_download",
            request_id,
            "ok",
            started,
            path_sha256=_digest(path, 16),
            content_sha256=digest,
            bytes=len(content),
        )
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            "path": path,
            "bytes": len(content),
            "sha256": digest,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    # Durable asynchronous jobs -------------------------------------

    def job_start(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        command = _required_string(body, "command", MAX_COMMAND_BYTES)
        cwd = _safe_path(body.get("cwd", "/workspace"))
        profile = self.profiles[record.profile]
        job_ref = f"job_{uuid.uuid4().hex[:20]}"
        root = f"/tmp/cube-adapter-jobs/{job_ref}"
        script_path = f"{root}/command.sh"
        stdout_path = f"{root}/stdout"
        stderr_path = f"{root}/stderr"
        exit_path = f"{root}/exit-code"
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            if len(self._active_jobs_locked(record)) >= profile.max_jobs_per_lease:
                raise AdapterError(
                    429, "job_quota_exceeded", "active job quota has been reached"
                )
            sandbox = self._sandbox(record)
            prepared = sandbox.commands.run(
                f"mkdir -p -- {shlex.quote(root)}", timeout=30
            )
            if prepared.exit_code != 0:
                raise RuntimeError("failed to prepare asynchronous job directory")
            sandbox.files.write(
                script_path,
                "#!/bin/bash\nset +e\ncd -- "
                + shlex.quote(cwd)
                + "\n"
                + command
                + "\n",
            )
            wrapper = (
                f"chmod 700 -- {shlex.quote(script_path)} && "
                "setsid /bin/sh -c "
                + shlex.quote(
                    "timeout --signal=TERM --kill-after=5s "
                    f"{profile.max_command_seconds} /bin/bash {shlex.quote(script_path)} "
                    f">{shlex.quote(stdout_path)} 2>{shlex.quote(stderr_path)}; "
                    f"printf '%s' $? >{shlex.quote(exit_path)}"
                )
                + " </dev/null >/dev/null 2>&1 & echo $!"
            )
            launch = sandbox.commands.run(wrapper, timeout=30)
            if launch.exit_code != 0:
                raise RuntimeError("failed to launch asynchronous job")
            try:
                pid = int((launch.stdout or "").strip().splitlines()[-1])
            except (ValueError, IndexError) as error:
                raise RuntimeError("sandbox did not return a job PID") from error
            job = JobRecord(
                job_ref=job_ref,
                lease_ref=lease_ref,
                tenant_id=record.tenant_id,
                command_sha256=_digest(command, 16),
                pid=pid,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                exit_path=exit_path,
            )
            self.state.put_job(job)
            self._touch(record, "running")
        self._audit(
            record,
            "job_start",
            request_id,
            "ok",
            started,
            job_ref=job_ref,
            command_sha256=job.command_sha256,
        )
        self._refresh_gauges()
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            "job_ref": job_ref,
            "state": "running",
        }

    def job_status(
        self, job_ref: str, auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        job, record = self._job(job_ref, auth)
        self._refresh_job(job, record)
        return self._job_result(job, record)

    def job_output(
        self,
        job_ref: str,
        body: Optional[Dict[str, Any]] = None,
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        body = body or {}
        job, record = self._job(job_ref, auth)
        self._refresh_job(job, record)
        offset = body.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise AdapterError(400, "invalid_offset", "offset must be a non-negative integer")
        maximum = body.get("max_bytes", MAX_OUTPUT_BYTES)
        if not isinstance(maximum, int) or maximum < 1:
            raise AdapterError(400, "invalid_limit", "max_bytes must be positive")
        maximum = min(maximum, MAX_JOB_OUTPUT_BYTES)
        stdout = self._read_job_file(record, job.stdout_path)
        stderr = self._read_job_file(record, job.stderr_path)
        combined = json.dumps({"stdout": stdout, "stderr": stderr}, ensure_ascii=False).encode(
            "utf-8"
        )
        chunk = combined[offset : offset + maximum]
        return {
            **self._job_result(job, record),
            "offset": offset,
            "next_offset": offset + len(chunk),
            "eof": offset + len(chunk) >= len(combined) and job.state not in {"running", "starting"},
            "data": chunk.decode("utf-8", errors="replace"),
            "encoding": "json-fragment",
        }

    def iter_job_events(
        self,
        job_ref: str,
        auth: AuthContext,
        *,
        interval: float = 0.5,
        timeout: float = 300,
    ) -> Iterator[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        last_stdout = ""
        last_stderr = ""
        while time.monotonic() < deadline:
            job, record = self._job(job_ref, auth)
            self._refresh_job(job, record)
            stdout = self._read_job_file(record, job.stdout_path)
            stderr = self._read_job_file(record, job.stderr_path)
            if stdout != last_stdout or stderr != last_stderr:
                yield {
                    "event": "output",
                    "data": {
                        "stdout": stdout[len(last_stdout) :],
                        "stderr": stderr[len(last_stderr) :],
                    },
                }
                last_stdout = stdout
                last_stderr = stderr
            yield {"event": "status", "data": self._job_result(job, record)}
            if job.state not in {"running", "starting"}:
                return
            time.sleep(interval)
        yield {"event": "timeout", "data": {"job_ref": job_ref}}

    def job_cancel(
        self,
        job_ref: str,
        body: Optional[Dict[str, Any]] = None,
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        body = body or {}
        job, record = self._job(job_ref, auth)
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            self._refresh_job_locked(job, record)
            if job.state not in {"running", "starting"}:
                self._audit(
                    record,
                    "job_cancel",
                    request_id,
                    "ok",
                    started,
                    job_ref=job.job_ref,
                    already_finished=True,
                )
                return self._job_result(job, record)
            sandbox = self._sandbox(record)
            command = (
                f"kill -TERM -- -{job.pid} 2>/dev/null || true; "
                f"sleep 1; kill -KILL -- -{job.pid} 2>/dev/null || true"
            )
            sandbox.commands.run(command, timeout=10)
            job.cancelled = True
            job.state = "cancelled"
            job.updated_at = time.time()
            self.state.put_job(job)
            self._touch(record, "running")
        self._audit(
            record,
            "job_cancel",
            request_id,
            "ok",
            started,
            job_ref=job.job_ref,
        )
        self._refresh_gauges()
        return self._job_result(job, record)

    # Interactive PTYs ----------------------------------------------

    def pty_create(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        rows = self._pty_dimension(body.get("rows", 24), "rows")
        cols = self._pty_dimension(body.get("cols", 80), "cols")
        cwd = _safe_path(body.get("cwd", "/workspace"))
        request_id = _request_id(body.get("request_id"))
        profile = self.profiles[record.profile]
        pty_ref = f"pty_{uuid.uuid4().hex[:20]}"
        started = time.perf_counter()
        with self._lease_lock(record):
            if len(self._active_jobs_locked(record)) >= profile.max_jobs_per_lease:
                raise AdapterError(
                    429, "job_quota_exceeded", "active job quota has been reached"
                )
            handle = self._sandbox(record).pty.create(
                PtySize(rows=rows, cols=cols), cwd=cwd, timeout=None
            )
            pid = int(handle.pid)
            handle.disconnect()
            pty = JobRecord(
                job_ref=pty_ref,
                lease_ref=lease_ref,
                tenant_id=record.tenant_id,
                command_sha256="pty",
                pid=pid,
                stdout_path="",
                stderr_path="",
                exit_path="",
            )
            self.state.put_job(pty)
            self._touch(record, "running")
        self._audit(record, "pty_create", request_id, "ok", started, pty_ref=pty_ref)
        self._refresh_gauges()
        return {**self._pty_result(pty, record), "request_id": request_id}

    def pty_status(
        self, pty_ref: str, auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        pty, record = self._pty(pty_ref, auth)
        self._refresh_pty(pty, record)
        return self._pty_result(pty, record)

    def pty_input(
        self, pty_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        pty, record = self._pty(pty_ref, auth)
        data = _bounded_string(body, "data", MAX_FILE_BYTES)
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            self._sandbox(record).pty.send_stdin(pty.pid, data, request_timeout=30)
            self._touch(record, "running")
        self._audit(
            record,
            "pty_input",
            request_id,
            "ok",
            started,
            pty_ref=pty_ref,
            bytes=len(data.encode("utf-8")),
        )
        return {**self._pty_result(pty, record), "request_id": request_id}

    def pty_resize(
        self, pty_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        pty, record = self._pty(pty_ref, auth)
        rows = self._pty_dimension(body.get("rows"), "rows")
        cols = self._pty_dimension(body.get("cols"), "cols")
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            self._sandbox(record).pty.resize(
                pty.pid, PtySize(rows=rows, cols=cols), request_timeout=30
            )
            self._touch(record, "running")
        self._audit(record, "pty_resize", request_id, "ok", started, pty_ref=pty_ref)
        return {
            **self._pty_result(pty, record),
            "request_id": request_id,
            "rows": rows,
            "cols": cols,
        }

    def pty_kill(
        self,
        pty_ref: str,
        body: Optional[Dict[str, Any]] = None,
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        body = body or {}
        pty, record = self._pty(pty_ref, auth)
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            self._refresh_pty_locked(pty, record)
            if pty.state not in {"running", "starting"}:
                self._audit(
                    record,
                    "pty_kill",
                    request_id,
                    "ok",
                    started,
                    pty_ref=pty_ref,
                    already_finished=True,
                )
                return {**self._pty_result(pty, record), "request_id": request_id}
            killed = self._sandbox(record).pty.kill(pty.pid, request_timeout=30)
            pty.cancelled = bool(killed)
            pty.state = "cancelled" if killed else "exited"
            pty.updated_at = time.time()
            self.state.put_job(pty)
            self._touch(record, "running")
        self._audit(record, "pty_kill", request_id, "ok", started, pty_ref=pty_ref)
        self._refresh_gauges()
        return {**self._pty_result(pty, record), "request_id": request_id}

    def iter_pty_events(
        self,
        pty_ref: str,
        auth: AuthContext,
        *,
        timeout: float = 300,
    ) -> Iterator[Dict[str, Any]]:
        pty, record = self._pty(pty_ref, auth)
        with self.state.lock(f"pty-stream:{pty_ref}", max(60, int(timeout) + 30)):
            handle = self._sandbox(record).pty.connect(pty.pid, timeout=timeout)
            try:
                yield {"event": "status", "data": self._pty_result(pty, record)}
                for chunk in handle:
                    yield {
                        "event": "output",
                        "data": {
                            "content_base64": base64.b64encode(chunk).decode("ascii"),
                            "encoding": "base64",
                        },
                    }
                pty.exit_code = handle.exit_code
                pty.state = "succeeded" if handle.exit_code == 0 else "failed"
                pty.updated_at = time.time()
                self.state.put_job(pty)
                self._refresh_gauges()
                yield {"event": "status", "data": self._pty_result(pty, record)}
            finally:
                handle.disconnect()

    # Checkpoints, rollback, and fork --------------------------------

    def checkpoint_create(
        self, lease_ref: str, body: Dict[str, Any], auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        profile = self.profiles[record.profile]
        self._require_checkpoints(profile, record)
        name = body.get("name")
        if name is not None:
            name = _required_string(body, "name", 128)
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record, max(180, self.config.lock_ttl_seconds)):
            snapshot = self._sandbox(record).create_snapshot(name=name)
            checkpoint = CheckpointRecord(
                checkpoint_ref=f"checkpoint_{uuid.uuid4().hex[:20]}",
                lease_ref=record.lease_ref,
                tenant_id=record.tenant_id,
                snapshot_id=str(snapshot.snapshot_id),
                name=name,
            )
            self.state.put_checkpoint(checkpoint)
            self._touch(record, "running")
        self._audit(
            record,
            "checkpoint_create",
            request_id,
            "ok",
            started,
            checkpoint_ref=checkpoint.checkpoint_ref,
        )
        return self._checkpoint_result(checkpoint, record, request_id)

    def checkpoint_list(
        self, lease_ref: str, auth: Optional[AuthContext] = None
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "checkpoints": [
                self._checkpoint_result(item, record, None)
                for item in sorted(
                    self.state.list_checkpoints(lease_ref),
                    key=lambda value: value.created_at,
                    reverse=True,
                )
            ],
        }

    def checkpoint_rollback(
        self,
        lease_ref: str,
        checkpoint_ref: str,
        body: Dict[str, Any],
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        profile = self.profiles[record.profile]
        self._require_checkpoints(profile, record)
        checkpoint = self._checkpoint(checkpoint_ref, record, auth)
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record, max(180, self.config.lock_ttl_seconds)):
            self._sandbox(record).rollback(checkpoint.snapshot_id)
            self._touch(record, "running")
        self._audit(
            record,
            "checkpoint_rollback",
            request_id,
            "ok",
            started,
            checkpoint_ref=checkpoint_ref,
        )
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            "checkpoint_ref": checkpoint_ref,
            "state": "running",
        }

    def checkpoint_delete(
        self,
        lease_ref: str,
        checkpoint_ref: str,
        body: Dict[str, Any],
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        record = self._lease(lease_ref, auth)
        checkpoint = self._checkpoint(checkpoint_ref, record, auth)
        request_id = _request_id(body.get("request_id"))
        started = time.perf_counter()
        with self._lease_lock(record):
            self._snapshot_deleter(checkpoint.snapshot_id)
            self.state.delete_checkpoint(checkpoint)
        self._audit(
            record,
            "checkpoint_delete",
            request_id,
            "ok",
            started,
            checkpoint_ref=checkpoint_ref,
        )
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            "checkpoint_ref": checkpoint_ref,
            "deleted": True,
        }

    def checkpoint_fork(
        self,
        lease_ref: str,
        checkpoint_ref: str,
        body: Dict[str, Any],
        auth: Optional[AuthContext] = None,
    ) -> Dict[str, Any]:
        auth = auth or _default_auth()
        source = self._lease(lease_ref, auth)
        profile = self.profiles[source.profile]
        self._require_checkpoints(profile, source)
        checkpoint = self._checkpoint(checkpoint_ref, source, auth)
        request_id = _request_id(body.get("request_id"))
        branch = body.get("branch", uuid.uuid4().hex)
        if not isinstance(branch, str) or not branch or len(branch.encode("utf-8")) > 128:
            raise AdapterError(400, "invalid_branch", "branch must be a short string")
        session_hash = hmac.new(
            self.config.session_hmac_key.encode("utf-8"),
            f"{source.session_hash}\0fork\0{branch}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
        started = time.perf_counter()
        with self.state.lock(
            f"session:{source.tenant_id}:{source.runtime}:{session_hash}",
            self.config.lock_ttl_seconds,
        ):
            existing_ref = self.state.get_session(
                source.tenant_id, source.runtime, session_hash
            )
            existing = self.state.get_lease(existing_ref) if existing_ref else None
            if existing and existing.state != "released":
                self._audit(
                    source,
                    "checkpoint_fork",
                    request_id,
                    "ok",
                    started,
                    checkpoint_ref=checkpoint_ref,
                    fork_lease_ref=existing.lease_ref,
                    reused=True,
                )
                return {
                    **self._lease_result(existing, reused=True),
                    "request_id": request_id,
                    "checkpoint_ref": checkpoint_ref,
                }
            active = [
                item
                for item in self.state.list_leases(source.tenant_id)
                if item.state != "released"
            ]
            if len(active) >= profile.max_active_leases_per_tenant:
                raise AdapterError(
                    429,
                    "lease_quota_exceeded",
                    "tenant active lease quota has been reached",
                )
            sandbox = self._sandbox_factory(
                template=checkpoint.snapshot_id,
                timeout=profile.sandbox_timeout_seconds,
                lifecycle=profile.lifecycle,
                allow_internet_access=profile.allow_internet_access,
                network=profile.network,
                metadata={
                    "runtime": source.runtime,
                    "purpose": "agent-adapter-fork",
                    "tenant": _digest(source.tenant_id, 16),
                    "session": session_hash,
                    "policy": profile.name,
                    "checkpoint": checkpoint.checkpoint_ref,
                },
                **(
                    {"distribution_scope": list(profile.distribution_scope)}
                    if profile.distribution_scope
                    else {}
                ),
            )
            forked = LeaseRecord(
                lease_ref=f"lease_{uuid.uuid4().hex[:20]}",
                tenant_id=source.tenant_id,
                runtime=source.runtime,
                session_hash=session_hash,
                profile=source.profile,
                sandbox_id=str(sandbox.sandbox_id),
                sandbox_ref=str(sandbox.sandbox_id)[:8],
                traffic_access_token=getattr(sandbox, "traffic_access_token", None),
            )
            self.state.put_lease(forked)
            with self._handles_lock:
                self._handles[forked.lease_ref] = sandbox
        self._audit(
            source,
            "checkpoint_fork",
            request_id,
            "ok",
            started,
            checkpoint_ref=checkpoint_ref,
            fork_lease_ref=forked.lease_ref,
        )
        self._refresh_gauges()
        return {
            **self._lease_result(forked, reused=False),
            "request_id": request_id,
            "checkpoint_ref": checkpoint_ref,
        }

    # Health, metrics, and administration ----------------------------

    def health(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": VERSION,
            "state_backend": "durable" if self.state.durable else "memory",
        }

    def readiness(self, *, force: bool = False) -> Tuple[int, Dict[str, Any]]:
        with self._readiness_lock:
            now = time.monotonic()
            if (
                not force
                and self._readiness.result is not None
                and now - self._readiness.checked_at < self.config.readiness_cache_seconds
            ):
                result = dict(self._readiness.result)
                return (200 if result["status"] == "ready" else 503, result)
            checks: Dict[str, Any] = {}
            checks["state"] = "ok" if self.state.health() else "error"
            try:
                for template in sorted({profile.template for profile in self.profiles.values()}):
                    info = self._template_checker(template)
                    status_value = getattr(info, "status", None)
                    if status_value is None and isinstance(info, dict):
                        status_value = info.get("status")
                    status = str(status_value or "unknown")
                    checks[f"template:{template}"] = status.lower()
            except Exception as error:
                checks["cube_api"] = f"error:{type(error).__name__}"
            proxy_host = os.environ.get("CUBE_PROXY_NODE_IP")
            proxy_port = int(os.environ.get("CUBE_PROXY_PORT_HTTP", "80"))
            if proxy_host:
                try:
                    with socket.create_connection((proxy_host, proxy_port), timeout=2):
                        checks["cube_proxy"] = "ok"
                except OSError:
                    checks["cube_proxy"] = "error"
            template_checks = [
                value for key, value in checks.items() if key.startswith("template:")
            ]
            good = (
                checks.get("state") == "ok"
                and bool(template_checks)
                and all(value == "ready" for value in template_checks)
                and not any(str(value).startswith("error") for value in checks.values())
            )
            result = {
                "status": "ready" if good else "not_ready",
                "version": VERSION,
                "checks": checks,
            }
            self._readiness = ReadinessCache(checked_at=now, result=result)
            self.metrics.state_ready.set(1 if good else 0)
            return (200 if good else 503, result)

    def metrics_payload(self) -> bytes:
        self._refresh_gauges()
        return self.metrics.render()

    def audit_html(self) -> str:
        if not self.config.audit_ui:
            raise AdapterError(404, "not_found", "audit UI is disabled")
        rows = list(reversed(self.audit.recent()))
        body = "".join(
            "<tr>"
            f"<td>{html.escape(str(row.get('ts', '')))}</td>"
            f"<td>{html.escape(str(row.get('runtime', '')))}</td>"
            f"<td>{html.escape(str(row.get('action', '')))}</td>"
            f"<td><code>{html.escape(str(row.get('sandbox_ref', '')))}</code></td>"
            f"<td><code>{html.escape(str(row.get('request_id', '')))}</code></td>"
            f"<td>{html.escape(str(row.get('outcome', '')))}</td>"
            f"<td>{html.escape(str(row.get('duration_ms', '')))} ms</td>"
            "</tr>"
            for row in rows
        )
        return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Cube Adapter Audit</title><style>
body{{font:14px system-ui;background:#f6f7fb;color:#172033;margin:0;padding:28px}}
table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:12px;border-bottom:1px solid #eee;text-align:left}}
</style></head><body><h1>Cube Adapter Audit</h1><p>Redacted events; no commands, output, tokens, or full Sandbox IDs.</p>
<table><thead><tr><th>Time</th><th>Runtime</th><th>Action</th><th>Sandbox</th><th>Request</th><th>Outcome</th><th>Duration</th></tr></thead><tbody>{body}</tbody></table></body></html>"""

    def force_gc(self, auth: Optional[AuthContext] = None) -> Dict[str, Any]:
        auth = auth or _default_auth()
        if not self.config.admin_enabled:
            raise AdapterError(404, "not_found", "route not found")
        if not auth.is_admin:
            raise AdapterError(403, "admin_required", "admin role required")
        return self._run_gc()

    def close(self) -> None:
        self._stop.set()
        if self._gc_thread is not None:
            self._gc_thread.join(timeout=2)
        with self._handles_lock:
            handles = list(self._handles.items())
            self._handles.clear()
        if self.state.durable:
            for _lease_ref, sandbox in handles:
                try:
                    sandbox.close()
                except Exception:
                    pass
        else:
            for lease_ref, sandbox in handles:
                record = self.state.get_lease(lease_ref)
                try:
                    sandbox.kill()
                    if record:
                        self._audit(
                            record,
                            "shutdown_cleanup",
                            uuid.uuid4().hex[:16],
                            "ok",
                            time.perf_counter(),
                        )
                        self.state.delete_lease(record)
                except Exception as error:
                    if record:
                        self._audit(
                            record,
                            "shutdown_cleanup",
                            uuid.uuid4().hex[:16],
                            "error",
                            time.perf_counter(),
                            error_type=type(error).__name__,
                        )
        self.audit.close()
        self.state.close()

    # Internal helpers -----------------------------------------------

    def _workspace(
        self, profile: ProfileConfig, auth: AuthContext, session_hash: str
    ) -> Tuple[Any, Optional[str], bool, Dict[str, Any]]:
        workspace = profile.workspace
        if workspace.mode == "ephemeral":
            return None, None, False, {}
        if workspace.mode == "existing-volume":
            assert workspace.volume_id is not None
            volume = self._volume_connector(workspace.volume_id)
            mounted: Any = (
                VolumeMount(volume, read_only=True) if workspace.read_only else volume
            )
            return volume, workspace.volume_id, False, {workspace.mount_path: mounted}
        name = workspace.volume_name or (
            "cwa-" + _digest(auth.tenant_id, 12) + "-" + session_hash[:20]
        )
        volume = self._volume_factory(name, driver=workspace.driver)
        volume_id = str(getattr(volume, "volume_id", name))
        mounted = VolumeMount(volume, read_only=True) if workspace.read_only else volume
        return volume, volume_id, True, {workspace.mount_path: mounted}

    def _destroy_owned_volume(self, record: LeaseRecord) -> None:
        if not record.volume_owned or not record.volume_id:
            return
        profile = self.profiles[record.profile]
        if profile.workspace.retain_on_kill:
            return
        try:
            self._volume_destroyer(record.volume_id)
        except Exception:
            pass

    def _lease(
        self, lease_ref: str, auth: AuthContext, *, allow_released: bool = False
    ) -> LeaseRecord:
        if not isinstance(lease_ref, str) or not LEASE_REF_RE.fullmatch(lease_ref):
            raise AdapterError(404, "lease_not_found", "lease was not found or has expired")
        record = self.state.get_lease(lease_ref)
        if record is None or (record.state == "released" and not allow_released):
            raise AdapterError(404, "lease_not_found", "lease was not found or has expired")
        if record.tenant_id != auth.tenant_id and not auth.is_admin:
            raise AdapterError(404, "lease_not_found", "lease was not found or has expired")
        return record

    def _sandbox(self, record: LeaseRecord) -> Any:
        with self._handles_lock:
            sandbox = self._handles.get(record.lease_ref)
        if sandbox is not None:
            return sandbox
        sandbox = self._sandbox_connector(record.sandbox_id)
        if record.traffic_access_token and hasattr(sandbox, "_data"):
            sandbox._data["trafficAccessToken"] = record.traffic_access_token
        with self._handles_lock:
            raced = self._handles.setdefault(record.lease_ref, sandbox)
        if raced is not sandbox:
            try:
                sandbox.close()
            except Exception:
                pass
        return raced

    def _lease_lock(self, record: LeaseRecord, ttl: Optional[int] = None):
        return self.state.lock(
            f"lease:{record.lease_ref}", ttl or self.config.lock_ttl_seconds
        )

    def _touch(self, record: LeaseRecord, state: Optional[str] = None) -> None:
        record.last_used_at = time.time()
        if state:
            record.state = state
        record.version += 1
        record.last_error = None
        self.state.put_lease(record)

    def _job(self, job_ref: str, auth: AuthContext) -> Tuple[JobRecord, LeaseRecord]:
        if not isinstance(job_ref, str) or not JOB_REF_RE.fullmatch(job_ref):
            raise AdapterError(404, "job_not_found", "job was not found")
        job = self.state.get_job(job_ref)
        if job is None:
            raise AdapterError(404, "job_not_found", "job was not found")
        record = self._lease(job.lease_ref, auth)
        if job.tenant_id != record.tenant_id:
            raise AdapterError(404, "job_not_found", "job was not found")
        return job, record

    def _refresh_job(self, job: JobRecord, record: LeaseRecord) -> None:
        if job.state not in {"running", "starting"}:
            return
        with self._lease_lock(record):
            self._refresh_job_locked(job, record)

    def _refresh_job_locked(self, job: JobRecord, record: LeaseRecord) -> None:
        if job.state not in {"running", "starting"}:
            return
        sandbox = self._sandbox(record)
        try:
            if sandbox.files.exists(job.exit_path):
                raw = sandbox.files.read(job.exit_path).strip()
                job.exit_code = int(raw)
                job.state = "succeeded" if job.exit_code == 0 else "failed"
            else:
                live = sandbox.commands.run(
                    f"kill -0 {job.pid} 2>/dev/null", timeout=10
                )
                if live.exit_code == 0:
                    return
                job.state = "failed"
            job.updated_at = time.time()
            self.state.put_job(job)
            self._refresh_gauges()
        except Exception:
            return

    def _active_jobs_locked(self, record: LeaseRecord) -> list[JobRecord]:
        jobs = self.state.list_jobs(lease_ref=record.lease_ref)
        for job in jobs:
            if job.command_sha256 == "pty":
                self._refresh_pty_locked(job, record)
            else:
                self._refresh_job_locked(job, record)
        return [job for job in jobs if job.state in {"starting", "running"}]

    def _read_job_file(self, record: LeaseRecord, path: str) -> str:
        if not path:
            return ""
        try:
            content = self._sandbox(record).files.read(path)
        except Exception:
            return ""
        return _truncate(content, MAX_JOB_OUTPUT_BYTES)[0]

    def _job_result(self, job: JobRecord, record: LeaseRecord) -> Dict[str, Any]:
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "job_ref": job.job_ref,
            "state": job.state,
            "exit_code": job.exit_code,
            "cancelled": job.cancelled,
            "created_at": _timestamp(job.created_at),
            "updated_at": _timestamp(job.updated_at),
        }

    def _pty(self, pty_ref: str, auth: AuthContext) -> Tuple[JobRecord, LeaseRecord]:
        if not isinstance(pty_ref, str) or not PTY_REF_RE.fullmatch(pty_ref):
            raise AdapterError(404, "pty_not_found", "PTY was not found")
        pty = self.state.get_job(pty_ref)
        if pty is None or pty.command_sha256 != "pty":
            raise AdapterError(404, "pty_not_found", "PTY was not found")
        record = self._lease(pty.lease_ref, auth)
        if pty.tenant_id != record.tenant_id:
            raise AdapterError(404, "pty_not_found", "PTY was not found")
        return pty, record

    def _refresh_pty(self, pty: JobRecord, record: LeaseRecord) -> None:
        if pty.state not in {"running", "starting"}:
            return
        with self._lease_lock(record):
            self._refresh_pty_locked(pty, record)

    def _refresh_pty_locked(self, pty: JobRecord, record: LeaseRecord) -> None:
        if pty.state not in {"running", "starting"}:
            return
        try:
            result = self._sandbox(record).commands.run(
                f"kill -0 {pty.pid} 2>/dev/null", timeout=10
            )
        except Exception:
            return
        if result.exit_code != 0:
            pty.state = "exited"
            pty.updated_at = time.time()
            self.state.put_job(pty)
            self._refresh_gauges()

    @staticmethod
    def _pty_dimension(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
            raise AdapterError(
                400, "invalid_pty_size", f"{name} must be an integer from 1 to 1000"
            )
        return value

    @staticmethod
    def _pty_result(pty: JobRecord, record: LeaseRecord) -> Dict[str, Any]:
        return {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "pty_ref": pty.job_ref,
            "state": pty.state,
            "exit_code": pty.exit_code,
            "created_at": _timestamp(pty.created_at),
            "updated_at": _timestamp(pty.updated_at),
        }

    def _checkpoint(
        self, checkpoint_ref: str, record: LeaseRecord, auth: AuthContext
    ) -> CheckpointRecord:
        del auth
        if not isinstance(checkpoint_ref, str) or not CHECKPOINT_REF_RE.fullmatch(
            checkpoint_ref
        ):
            raise AdapterError(404, "checkpoint_not_found", "checkpoint was not found")
        checkpoint = self.state.get_checkpoint(checkpoint_ref)
        if checkpoint is None or checkpoint.lease_ref != record.lease_ref:
            raise AdapterError(404, "checkpoint_not_found", "checkpoint was not found")
        return checkpoint

    def _require_checkpoints(self, profile: ProfileConfig, record: LeaseRecord) -> None:
        if not profile.checkpoints_enabled:
            raise AdapterError(403, "checkpoint_denied", "checkpoints are disabled by profile")
        if record.volume_id and not profile.allow_checkpoint_with_mounts:
            raise AdapterError(
                409,
                "checkpoint_with_mount_denied",
                "upstream snapshot support for mounted volumes is not enabled",
            )

    @staticmethod
    def _checkpoint_result(
        checkpoint: CheckpointRecord,
        record: LeaseRecord,
        request_id: Optional[str],
    ) -> Dict[str, Any]:
        result = {
            "executor": "cubesandbox-microvm",
            "sandbox_ref": record.sandbox_ref,
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "name": checkpoint.name,
            "created_at": _timestamp(checkpoint.created_at),
        }
        if request_id is not None:
            result["request_id"] = request_id
        return result

    @staticmethod
    def _lease_result(record: LeaseRecord, reused: bool) -> Dict[str, Any]:
        return {
            "executor": "cubesandbox-microvm",
            "lease_ref": record.lease_ref,
            "sandbox_ref": record.sandbox_ref,
            "profile": record.profile,
            "reused": reused,
            "state": record.state,
            "persistent_workspace": bool(record.volume_id),
        }

    def _audit(
        self,
        record: LeaseRecord,
        action: str,
        request_id: str,
        outcome: str,
        started: float,
        **extra: Any,
    ) -> None:
        elapsed = max(0.0, time.perf_counter() - started)
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tenant_hash": _digest(record.tenant_id, 16),
            "runtime": record.runtime,
            "session_hash": record.session_hash,
            "action": action,
            "profile": record.profile,
            "lease_ref": record.lease_ref,
            "sandbox_ref": record.sandbox_ref,
            "request_id": request_id,
            "outcome": outcome,
            "duration_ms": round(elapsed * 1000),
            **extra,
        }
        self.audit.emit(event)
        self.metrics.observe(
            action,
            outcome,
            elapsed,
            runtime=record.runtime,
            profile=record.profile,
        )

    def _refresh_gauges(self) -> None:
        leases = self.state.list_leases()
        jobs = self.state.list_jobs()
        self.metrics.active_leases.set(
            len([item for item in leases if item.state != "released"])
        )
        self.metrics.active_jobs.set(
            len([item for item in jobs if item.state in {"starting", "running"}])
        )

    def _gc_loop(self) -> None:
        while not self._stop.wait(self.config.gc_interval_seconds):
            try:
                self._run_gc()
            except Exception:
                continue

    def _run_gc(self) -> Dict[str, Any]:
        now = time.time()
        collected = 0
        failed = 0
        for record in self.state.list_leases():
            profile = self.profiles.get(record.profile)
            if profile is None or record.state == "released":
                continue
            if now - record.last_used_at < profile.lease_idle_ttl_seconds:
                continue
            started = time.perf_counter()
            try:
                with self._lease_lock(record):
                    latest = self.state.get_lease(record.lease_ref)
                    if latest is None or now - latest.last_used_at < profile.lease_idle_ttl_seconds:
                        continue
                    sandbox = self._sandbox(latest)
                    sandbox.kill()
                    latest.state = "released"
                    self._destroy_owned_volume(latest)
                    self.state.delete_lease(latest)
                    with self._handles_lock:
                        self._handles.pop(latest.lease_ref, None)
                    self._audit(
                        latest,
                        "gc_release",
                        uuid.uuid4().hex[:16],
                        "ok",
                        started,
                    )
                    collected += 1
                    self.metrics.gc_actions.labels("ok").inc()
            except Exception as error:
                record.last_error = type(error).__name__
                self.state.put_lease(record)
                failed += 1
                self.metrics.gc_actions.labels("error").inc()
        self._refresh_gauges()
        return {"collected": collected, "failed": failed}


def _timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _redact_entry(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "name",
        "path",
        "type",
        "size",
        "mode",
        "permissions",
        "modified_time",
        "modifiedTime",
        "is_dir",
        "isDir",
    }
    return {key: item for key, item in value.items() if key in allowed}


def _redact_entries(values: Any) -> list[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [_redact_entry(item) for item in values[:1000]]


def _entry_size(value: Any) -> Optional[int]:
    if not isinstance(value, dict):
        return None
    for key in ("size", "sizeBytes", "size_bytes"):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return None
    return None
