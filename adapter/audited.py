"""Fail-closed audit envelope around execution and policy entry points."""

from __future__ import annotations

import functools
import hashlib
import hmac
import inspect
import json
import re
import uuid
from contextvars import ContextVar
from typing import Any, Callable

from .audit_journal import AuditUnavailable
from .config import AuthContext

# Explicit coverage, including internal GC and authorization failures in methods.
OPERATIONS = frozenset(
    """authenticate acquire lease_status list_leases release exec read write
list_files stat_file make_dir remove_file move_file artifact_upload artifact_download
job_start job_status job_output job_cancel pty_create pty_status pty_input pty_resize
pty_kill checkpoint_create checkpoint_list checkpoint_rollback checkpoint_delete
checkpoint_fork list_task_templates task_plan task_approve task_plan_status
task_submit task_status task_result task_cancel task_receipt force_gc _run_gc""".split()
)
_parent: ContextVar[str | None] = ContextVar("audit_operation", default=None)


def audited_adapter(cls: Any) -> Any:
    for name in OPERATIONS:
        setattr(cls, name, _wrap(getattr(cls, name)))
    for name in ("iter_job_events", "iter_pty_events"):
        setattr(cls, name, _wrap_stream(getattr(cls, name)))
    return cls


def _wrap_stream(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def stream(self: Any, *args: Any, **kwargs: Any) -> Any:
        journal = self.audit.journal
        if journal is None:
            yield from method(self, *args, **kwargs)
            return
        from .core import AdapterError

        operation_id = uuid.uuid4().hex
        event = {"operation_id": operation_id, "action": method.__name__, "runtime": "adapter"}
        try:
            journal.append({**event, "phase": "intent", "outcome": "pending"}, begin=operation_id)
            try:
                yield from method(self, *args, **kwargs)
            except GeneratorExit:
                journal.append(
                    {**event, "phase": "result", "outcome": "disconnected"}, finish=operation_id
                )
                raise
            except Exception:
                journal.append(
                    {**event, "phase": "result", "outcome": "error"}, finish=operation_id
                )
                raise
            else:
                journal.append({**event, "phase": "result", "outcome": "ok"}, finish=operation_id)
        except AuditUnavailable as error:
            raise AdapterError(503, "audit_unavailable", "durable audit unavailable") from error

    return stream


def _wrap(method: Callable[..., Any]) -> Callable[..., Any]:
    signature = inspect.signature(method)

    @functools.wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        journal = self.audit.journal
        if journal is None:
            return method(self, *args, **kwargs)
        # Import at invocation time to avoid a circular dependency.
        from .core import AdapterError

        bound = signature.bind(self, *args, **kwargs).arguments
        auth = bound.get("auth")
        operation_id = uuid.uuid4().hex
        event: dict[str, Any] = {
            "operation_id": operation_id,
            "action": method.__name__,
            "runtime": "adapter",
            "phase": "intent",
            "outcome": "pending",
        }
        if _parent.get():
            event["parent_operation_id"] = _parent.get()
        if isinstance(auth, AuthContext):
            event["identity_hash"] = self._identity_hash(auth)
        # Only opaque server-generated references, never commands/parameters/tokens.
        if isinstance(bound.get("body"), dict):
            event["request_hmac_sha256"] = hmac.new(
                self.config.session_hmac_key.encode(),
                json.dumps(bound["body"], sort_keys=True, default=str).encode(),
                hashlib.sha256,
            ).hexdigest()
        for key in ("lease_ref", "job_ref", "pty_ref", "checkpoint_ref", "plan_ref", "task_ref"):
            value = bound.get(key)
            if isinstance(value, str) and re.fullmatch(r"[a-z-]+_[a-f0-9]{20}", value):
                event[key] = value
        token = _parent.set(operation_id)
        try:
            journal.append(event, begin=operation_id)
            try:
                result = method(self, *args, **kwargs)
            except AuditUnavailable:
                raise
            except Exception as error:
                # An unexpected failure may follow an external side effect.
                known_rejection = isinstance(error, AdapterError) and error.status < 500
                journal.append(
                    {
                        **event,
                        "phase": "result",
                        "outcome": "rejected" if known_rejection else "uncertain",
                        "error_type": type(error).__name__,
                        "error_code": error.code
                        if isinstance(error, AdapterError)
                        else "upstream_error",
                    },
                    finish=operation_id if known_rejection else None,
                )
                if not known_rejection:
                    journal.failed = True
                raise
            completion = {**event, "phase": "result", "outcome": "ok"}
            if isinstance(result, AuthContext):
                completion["identity_hash"] = self._identity_hash(result)
            if isinstance(result, dict):
                for key in (
                    "lease_ref",
                    "job_ref",
                    "pty_ref",
                    "checkpoint_ref",
                    "plan_ref",
                    "task_ref",
                ):
                    value = result.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[a-z-]+_[a-f0-9]{20}", value):
                        completion[key] = value
            journal.append(completion, finish=operation_id)
            return result
        except AuditUnavailable as error:
            raise AdapterError(
                503,
                "audit_unavailable",
                "durable audit unavailable; operator reconciliation may be required",
            ) from error
        finally:
            _parent.reset(token)

    return wrapped
