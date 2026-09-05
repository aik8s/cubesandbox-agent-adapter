"""Durable lease/job state with optional Redis fencing and encryption."""

from __future__ import annotations

import contextlib
import copy
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, Optional, Protocol
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken


@dataclass
class LeaseRecord:
    lease_ref: str
    tenant_id: str
    runtime: str
    session_hash: str
    profile: str
    sandbox_id: str
    sandbox_ref: str
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    state: str = "running"
    traffic_access_token: Optional[str] = field(default=None, repr=False)
    volume_id: Optional[str] = None
    volume_owned: bool = False
    version: int = 1
    last_error: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "LeaseRecord":
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JobRecord:
    job_ref: str
    lease_ref: str
    tenant_id: str
    command_sha256: str
    pid: int
    stdout_path: str
    stderr_path: str
    exit_path: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    state: str = "running"
    exit_code: Optional[int] = None
    cancelled: bool = False

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "JobRecord":
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CheckpointRecord:
    checkpoint_ref: str
    lease_ref: str
    tenant_id: str
    snapshot_id: str
    name: Optional[str]
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CheckpointRecord":
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskPlanRecord:
    plan_ref: str
    tenant_id: str
    requester_hash: str
    template_name: str
    template_digest: str
    parameters: Dict[str, Any]
    parameters_sha256: str
    command_sha256: str
    state: str
    approval_required: bool
    created_at: float
    expires_at: float
    approved_at: Optional[float] = None
    approved_by_hash: Optional[str] = None
    submitted_task_ref: Optional[str] = None
    denial_reason_sha256: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TaskPlanRecord":
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskRecord:
    task_ref: str
    plan_ref: str
    tenant_id: str
    requester_hash: str
    template_name: str
    template_digest: str
    profile: str
    lease_ref: Optional[str] = None
    job_ref: Optional[str] = None
    state: str = "starting"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: Optional[Dict[str, Any]] = None
    receipt: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    cleanup_status: str = "not_started"

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TaskRecord":
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StateStore(Protocol):
    durable: bool

    def health(self) -> bool: ...

    @contextlib.contextmanager
    def lock(self, key: str, ttl_seconds: int = 180) -> Iterator[None]: ...

    def get_session(self, tenant_id: str, runtime: str, session_hash: str) -> Optional[str]: ...

    def get_lease(self, lease_ref: str) -> Optional[LeaseRecord]: ...

    def put_lease(self, record: LeaseRecord) -> None: ...

    def delete_lease(self, record: LeaseRecord) -> None: ...

    def list_leases(self, tenant_id: Optional[str] = None) -> list[LeaseRecord]: ...

    def put_job(self, record: JobRecord) -> None: ...

    def get_job(self, job_ref: str) -> Optional[JobRecord]: ...

    def delete_job(self, record: JobRecord) -> None: ...

    def list_jobs(
        self, *, lease_ref: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> list[JobRecord]: ...

    def put_checkpoint(self, record: CheckpointRecord) -> None: ...

    def get_checkpoint(self, checkpoint_ref: str) -> Optional[CheckpointRecord]: ...

    def delete_checkpoint(self, record: CheckpointRecord) -> None: ...

    def list_checkpoints(self, lease_ref: str) -> list[CheckpointRecord]: ...

    def put_task_plan(self, record: TaskPlanRecord) -> None: ...

    def get_task_plan(self, plan_ref: str) -> Optional[TaskPlanRecord]: ...

    def put_task(self, record: TaskRecord) -> None: ...

    def get_task(self, task_ref: str) -> Optional[TaskRecord]: ...

    def close(self) -> None: ...


class MemoryStateStore:
    durable = False

    def __init__(self) -> None:
        self._leases: Dict[str, LeaseRecord] = {}
        self._sessions: Dict[tuple[str, str, str], str] = {}
        self._jobs: Dict[str, JobRecord] = {}
        self._checkpoints: Dict[str, CheckpointRecord] = {}
        self._task_plans: Dict[str, TaskPlanRecord] = {}
        self._tasks: Dict[str, TaskRecord] = {}
        self._global_lock = threading.RLock()
        self._key_locks: Dict[str, threading.RLock] = {}

    def health(self) -> bool:
        return True

    @contextlib.contextmanager
    def lock(self, key: str, ttl_seconds: int = 180) -> Iterator[None]:
        del ttl_seconds
        with self._global_lock:
            lock = self._key_locks.setdefault(key, threading.RLock())
        with lock:
            yield

    def get_session(self, tenant_id: str, runtime: str, session_hash: str) -> Optional[str]:
        with self._global_lock:
            return self._sessions.get((tenant_id, runtime, session_hash))

    def get_lease(self, lease_ref: str) -> Optional[LeaseRecord]:
        with self._global_lock:
            value = self._leases.get(lease_ref)
            return copy.deepcopy(value) if value else None

    def put_lease(self, record: LeaseRecord) -> None:
        with self._global_lock:
            old = self._leases.get(record.lease_ref)
            if old and (
                old.tenant_id,
                old.runtime,
                old.session_hash,
            ) != (record.tenant_id, record.runtime, record.session_hash):
                self._sessions.pop(
                    (old.tenant_id, old.runtime, old.session_hash), None
                )
            self._leases[record.lease_ref] = copy.deepcopy(record)
            self._sessions[(record.tenant_id, record.runtime, record.session_hash)] = (
                record.lease_ref
            )

    def delete_lease(self, record: LeaseRecord) -> None:
        with self._global_lock:
            self._leases.pop(record.lease_ref, None)
            self._sessions.pop(
                (record.tenant_id, record.runtime, record.session_hash), None
            )
            for job in list(self._jobs.values()):
                if job.lease_ref == record.lease_ref:
                    self._jobs.pop(job.job_ref, None)
            for checkpoint in list(self._checkpoints.values()):
                if checkpoint.lease_ref == record.lease_ref:
                    self._checkpoints.pop(checkpoint.checkpoint_ref, None)

    def list_leases(self, tenant_id: Optional[str] = None) -> list[LeaseRecord]:
        with self._global_lock:
            values = list(self._leases.values())
            if tenant_id is not None:
                values = [item for item in values if item.tenant_id == tenant_id]
            return [copy.deepcopy(item) for item in values]

    def put_job(self, record: JobRecord) -> None:
        with self._global_lock:
            self._jobs[record.job_ref] = copy.deepcopy(record)

    def get_job(self, job_ref: str) -> Optional[JobRecord]:
        with self._global_lock:
            value = self._jobs.get(job_ref)
            return copy.deepcopy(value) if value else None

    def delete_job(self, record: JobRecord) -> None:
        with self._global_lock:
            self._jobs.pop(record.job_ref, None)

    def list_jobs(
        self, *, lease_ref: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> list[JobRecord]:
        with self._global_lock:
            values = list(self._jobs.values())
            if lease_ref is not None:
                values = [item for item in values if item.lease_ref == lease_ref]
            if tenant_id is not None:
                values = [item for item in values if item.tenant_id == tenant_id]
            return [copy.deepcopy(item) for item in values]

    def put_checkpoint(self, record: CheckpointRecord) -> None:
        with self._global_lock:
            self._checkpoints[record.checkpoint_ref] = copy.deepcopy(record)

    def get_checkpoint(self, checkpoint_ref: str) -> Optional[CheckpointRecord]:
        with self._global_lock:
            value = self._checkpoints.get(checkpoint_ref)
            return copy.deepcopy(value) if value else None

    def delete_checkpoint(self, record: CheckpointRecord) -> None:
        with self._global_lock:
            self._checkpoints.pop(record.checkpoint_ref, None)

    def list_checkpoints(self, lease_ref: str) -> list[CheckpointRecord]:
        with self._global_lock:
            return [
                copy.deepcopy(item)
                for item in self._checkpoints.values()
                if item.lease_ref == lease_ref
            ]

    def put_task_plan(self, record: TaskPlanRecord) -> None:
        with self._global_lock:
            self._task_plans[record.plan_ref] = copy.deepcopy(record)

    def get_task_plan(self, plan_ref: str) -> Optional[TaskPlanRecord]:
        with self._global_lock:
            value = self._task_plans.get(plan_ref)
            return copy.deepcopy(value) if value else None

    def put_task(self, record: TaskRecord) -> None:
        with self._global_lock:
            self._tasks[record.task_ref] = copy.deepcopy(record)

    def get_task(self, task_ref: str) -> Optional[TaskRecord]:
        with self._global_lock:
            value = self._tasks.get(task_ref)
            return copy.deepcopy(value) if value else None

    def close(self) -> None:
        return


class RecordCipher:
    """Encrypt every Redis record, including data-plane traffic tokens."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def dumps(self, value: Dict[str, Any]) -> bytes:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return self._fernet.encrypt(raw)

    def loads(self, value: bytes) -> Dict[str, Any]:
        try:
            raw = self._fernet.decrypt(value)
        except InvalidToken as error:
            raise RuntimeError("cannot decrypt persisted Adapter state") from error
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError("persisted Adapter state is not an object")
        return parsed


class RedisStateStore:
    durable = True

    _RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
    _EXTEND_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

    def __init__(self, url: str, *, prefix: str, encryption_key: str) -> None:
        try:
            import redis
        except ImportError as error:  # pragma: no cover - deployment dependency
            raise RuntimeError("Redis state requires the redis Python package") from error
        parsed = urlparse(url)
        if parsed.scheme not in {"redis", "rediss"}:
            raise RuntimeError("state backend URL must use redis:// or rediss://")
        # redis-py exposes a sync/async union in its public stubs even though
        # Redis.from_url here is the synchronous client.
        self._redis: Any = redis.Redis.from_url(url, decode_responses=False)
        self._prefix = prefix.rstrip(":")
        self._cipher = RecordCipher(encryption_key)

    def _key(self, *parts: str) -> str:
        encoded = [part.replace(":", "%3A") for part in parts]
        return ":".join([self._prefix, *encoded])

    def health(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    @contextlib.contextmanager
    def lock(self, key: str, ttl_seconds: int = 180) -> Iterator[None]:
        lock_key = self._key("lock", key)
        token = uuid.uuid4().hex.encode("ascii")
        deadline = time.monotonic() + min(30.0, max(5.0, ttl_seconds / 3))
        ttl_ms = max(1000, int(ttl_seconds * 1000))
        while not self._redis.set(lock_key, token, nx=True, px=ttl_ms):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring state lock {key!r}")
            time.sleep(0.05)

        stop = threading.Event()

        def heartbeat() -> None:
            interval = max(0.5, ttl_seconds / 3)
            while not stop.wait(interval):
                try:
                    result = self._redis.eval(
                        self._EXTEND_LOCK_SCRIPT, 1, lock_key, token, ttl_ms
                    )
                    if not result:
                        return
                except Exception:
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)
            try:
                self._redis.eval(self._RELEASE_LOCK_SCRIPT, 1, lock_key, token)
            except Exception:
                pass

    def _load(self, key: str, record_type: Any) -> Any:
        raw = self._redis.get(key)
        if raw is None:
            return None
        return record_type.from_dict(self._cipher.loads(raw))

    def _save(self, key: str, record: Any) -> None:
        self._redis.set(key, self._cipher.dumps(record.to_dict()))

    def get_session(self, tenant_id: str, runtime: str, session_hash: str) -> Optional[str]:
        raw = self._redis.get(self._key("session", tenant_id, runtime, session_hash))
        return raw.decode("utf-8") if raw else None

    def get_lease(self, lease_ref: str) -> Optional[LeaseRecord]:
        return self._load(self._key("lease", lease_ref), LeaseRecord)

    def put_lease(self, record: LeaseRecord) -> None:
        lease_key = self._key("lease", record.lease_ref)
        session_key = self._key(
            "session", record.tenant_id, record.runtime, record.session_hash
        )
        tenant_key = self._key("tenant-leases", record.tenant_id)
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(lease_key, self._cipher.dumps(record.to_dict()))
        pipe.set(session_key, record.lease_ref.encode("utf-8"))
        pipe.sadd(self._key("leases"), record.lease_ref.encode("utf-8"))
        pipe.sadd(tenant_key, record.lease_ref.encode("utf-8"))
        pipe.execute()

    def delete_lease(self, record: LeaseRecord) -> None:
        job_refs = [item.job_ref for item in self.list_jobs(lease_ref=record.lease_ref)]
        checkpoints = self.list_checkpoints(record.lease_ref)
        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(self._key("lease", record.lease_ref))
        pipe.delete(
            self._key("session", record.tenant_id, record.runtime, record.session_hash)
        )
        pipe.srem(self._key("leases"), record.lease_ref.encode("utf-8"))
        pipe.srem(
            self._key("tenant-leases", record.tenant_id),
            record.lease_ref.encode("utf-8"),
        )
        for job_ref in job_refs:
            pipe.delete(self._key("job", job_ref))
            pipe.srem(self._key("jobs"), job_ref.encode("utf-8"))
            pipe.srem(self._key("lease-jobs", record.lease_ref), job_ref.encode("utf-8"))
        for checkpoint in checkpoints:
            pipe.delete(self._key("checkpoint", checkpoint.checkpoint_ref))
            pipe.srem(
                self._key("lease-checkpoints", record.lease_ref),
                checkpoint.checkpoint_ref.encode("utf-8"),
            )
        pipe.execute()

    def _members(self, key: str) -> list[str]:
        return [item.decode("utf-8") for item in self._redis.smembers(key)]

    def list_leases(self, tenant_id: Optional[str] = None) -> list[LeaseRecord]:
        key = (
            self._key("tenant-leases", tenant_id)
            if tenant_id is not None
            else self._key("leases")
        )
        records = [self.get_lease(item) for item in self._members(key)]
        return [item for item in records if item is not None]

    def put_job(self, record: JobRecord) -> None:
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(self._key("job", record.job_ref), self._cipher.dumps(record.to_dict()))
        pipe.sadd(self._key("jobs"), record.job_ref.encode("utf-8"))
        pipe.sadd(
            self._key("lease-jobs", record.lease_ref), record.job_ref.encode("utf-8")
        )
        pipe.execute()

    def get_job(self, job_ref: str) -> Optional[JobRecord]:
        return self._load(self._key("job", job_ref), JobRecord)

    def delete_job(self, record: JobRecord) -> None:
        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(self._key("job", record.job_ref))
        pipe.srem(self._key("jobs"), record.job_ref.encode("utf-8"))
        pipe.srem(
            self._key("lease-jobs", record.lease_ref), record.job_ref.encode("utf-8")
        )
        pipe.execute()

    def list_jobs(
        self, *, lease_ref: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> list[JobRecord]:
        key = self._key("lease-jobs", lease_ref) if lease_ref else self._key("jobs")
        records = [self.get_job(item) for item in self._members(key)]
        values = [item for item in records if item is not None]
        if tenant_id is not None:
            values = [item for item in values if item.tenant_id == tenant_id]
        return values

    def put_checkpoint(self, record: CheckpointRecord) -> None:
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(
            self._key("checkpoint", record.checkpoint_ref),
            self._cipher.dumps(record.to_dict()),
        )
        pipe.sadd(
            self._key("lease-checkpoints", record.lease_ref),
            record.checkpoint_ref.encode("utf-8"),
        )
        pipe.execute()

    def get_checkpoint(self, checkpoint_ref: str) -> Optional[CheckpointRecord]:
        return self._load(
            self._key("checkpoint", checkpoint_ref), CheckpointRecord
        )

    def delete_checkpoint(self, record: CheckpointRecord) -> None:
        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(self._key("checkpoint", record.checkpoint_ref))
        pipe.srem(
            self._key("lease-checkpoints", record.lease_ref),
            record.checkpoint_ref.encode("utf-8"),
        )
        pipe.execute()

    def list_checkpoints(self, lease_ref: str) -> list[CheckpointRecord]:
        records = [
            self.get_checkpoint(item)
            for item in self._members(self._key("lease-checkpoints", lease_ref))
        ]
        return [item for item in records if item is not None]

    def put_task_plan(self, record: TaskPlanRecord) -> None:
        self._save(self._key("task-plan", record.plan_ref), record)

    def get_task_plan(self, plan_ref: str) -> Optional[TaskPlanRecord]:
        return self._load(self._key("task-plan", plan_ref), TaskPlanRecord)

    def put_task(self, record: TaskRecord) -> None:
        self._save(self._key("task", record.task_ref), record)

    def get_task(self, task_ref: str) -> Optional[TaskRecord]:
        return self._load(self._key("task", task_ref), TaskRecord)

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:
            pass


def build_state_store(
    url: Optional[str], *, prefix: str, encryption_key: Optional[str]
) -> StateStore:
    if not url or url in {"memory://", "memory"}:
        return MemoryStateStore()
    if url.startswith(("redis://", "rediss://")):
        if not encryption_key:
            raise RuntimeError("Redis state requires an encryption key")
        return RedisStateStore(url, prefix=prefix, encryption_key=encryption_key)
    raise RuntimeError("unsupported state backend; use memory://, redis://, or rediss://")
