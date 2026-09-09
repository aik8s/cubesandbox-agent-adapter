"""Single-writer durable audit journal and transactional delivery outbox.

SQLite EXTRA synchronous commits are the authority; external sinks are replicas.
The containing directory must be on durable local/block storage (not NFS).
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class AuditUnavailable(RuntimeError):
    """No operation may proceed without durable audit evidence."""


class AuditJournal:
    def __init__(self, path: str, sinks: tuple[str, ...]) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self.failed = False
        self.sinks = sinks
        directory = Path(path).absolute().parent
        missing = []
        parent = directory
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for new_directory in reversed(missing):
            new_directory.mkdir(exist_ok=True)
            parent_fd = os.open(new_directory.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        self._owner = open(path + ".lock", "a+b")
        try:
            os.chmod(path + ".lock", 0o600)
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
            self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=EXTRA")
            self.db.execute("PRAGMA fullfsync=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    intent TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    seq INTEGER NOT NULL REFERENCES events(seq),
                    sink TEXT NOT NULL,
                    PRIMARY KEY(seq, sink)
                );
                CREATE INDEX IF NOT EXISTS pending_by_sink ON deliveries(sink, seq);
                CREATE INDEX IF NOT EXISTS incomplete_operations ON operations(completed);
            """)
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise AuditUnavailable("audit journal integrity check failed")
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.failed = bool(self.pending_operations())
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self._owner.close()
            raise

    def ensure_ready(self) -> None:
        if self.failed or self._closed:
            raise AuditUnavailable(
                "audit unavailable or unresolved operations require reconciliation"
            )

    def _insert(self, event: dict[str, Any]) -> dict[str, Any]:
        value = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **event,
            "event_id": uuid.uuid4().hex,
        }
        cursor = self.db.execute(
            "INSERT INTO events(event_id,payload) VALUES (?,?)",
            (value["event_id"], json.dumps(value, sort_keys=True)),
        )
        self.db.executemany(
            "INSERT INTO deliveries(seq,sink) VALUES (?,?)",
            [(cursor.lastrowid, sink) for sink in self.sinks],
        )
        return value

    def append(
        self, event: dict[str, Any], *, begin: str | None = None, finish: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self.ensure_ready()
            try:
                with self.db:
                    value = self._insert(event)
                    if begin:
                        self.db.execute(
                            "INSERT INTO operations(operation_id,intent) VALUES (?,?)",
                            (begin, json.dumps(value, sort_keys=True)),
                        )
                    if finish:
                        cursor = self.db.execute(
                            "UPDATE operations SET completed=1 WHERE operation_id=? AND completed=0",
                            (finish,),
                        )
                        if cursor.rowcount != 1:
                            raise AuditUnavailable("operation intent missing or already completed")
                return value
            except Exception as error:
                self.failed = True
                raise AuditUnavailable("audit commit failed; execution is blocked") from error

    def pending_operations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT intent FROM operations WHERE completed=0 ORDER BY rowid"
                )
            ]

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(row[0])
                for row in reversed(
                    self.db.execute(
                        "SELECT payload FROM events ORDER BY seq DESC LIMIT ?", (limit,)
                    ).fetchall()
                )
            ]

    def counts(self) -> tuple[int, int]:
        with self._lock:
            pending = self.db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
            incomplete = self.db.execute(
                "SELECT count(*) FROM operations WHERE completed=0"
            ).fetchone()[0]
            return pending, incomplete

    def next_delivery(self, sink: str) -> tuple[int, dict[str, Any]] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT e.seq,e.payload FROM deliveries d JOIN events e ON e.seq=d.seq WHERE d.sink=? ORDER BY e.seq LIMIT 1",
                (sink,),
            ).fetchone()
            return (row[0], json.loads(row[1])) if row else None

    def acknowledge(self, seq: int, sink: str) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM deliveries WHERE seq=? AND sink=?", (seq, sink))

    def reconcile(self, operation_id: str, operator_hash: str, note_hash: str) -> None:
        """Offline administrative acknowledgement; never claims execution succeeded."""
        with self._lock, self.db:
            cursor = self.db.execute(
                "UPDATE operations SET completed=1 WHERE operation_id=? AND completed=0",
                (operation_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown or already reconciled operation")
            self._insert(
                {
                    "action": "audit_reconcile",
                    "operation_id": operation_id,
                    "outcome": "manually_reconciled",
                    "operator_hash": operator_hash,
                    "note_sha256": note_hash,
                    "runtime": "admin",
                }
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.db.close()
            self._owner.close()
