"""Multi-writer fail-closed audit with fenced owners and a transactional outbox.

The PostgreSQL primary is the authority. Database HA requires separately managed
synchronous replication, backups and restore drills; this class does not create it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from typing import Any

from .audit_journal import AuditUnavailable


class PostgresAuditJournal:
    def __init__(
        self,
        url: str,
        sinks: tuple[str, ...],
        *,
        schema: str = "cube_adapter_audit",
        owner_ttl: int = 60,
        require_sync_replica: bool = False,
    ) -> None:
        import psycopg
        from psycopg import sql

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) or owner_ttl < 10:
            raise RuntimeError("invalid PostgreSQL audit schema or owner TTL")
        self._sql = sql
        self._schema = schema
        self.sinks = sinks
        self.owner_id = uuid.uuid4().hex
        self.owner_ttl = owner_ttl
        self._lock = threading.RLock()
        self._closed = False
        self._local_failed = False
        self._stop = threading.Event()
        self.db: Any = psycopg.connect(
            url,
            autocommit=True,
            connect_timeout=5,
            options="-c synchronous_commit=on -c statement_timeout=5000 -c lock_timeout=5000",
        )
        try:
            for setting in ("fsync", "full_page_writes", "synchronous_commit"):
                row = self.db.execute(
                    sql.SQL("SHOW {} ").format(sql.Identifier(setting))
                ).fetchone()
                if row is None or row[0] != "on":
                    raise AuditUnavailable("PostgreSQL durable commit settings are required")
            if require_sync_replica:
                row = self.db.execute("SHOW synchronous_standby_names").fetchone()
                if row is None or not row[0]:
                    raise AuditUnavailable("synchronous standby configuration is required")
            with self.db.transaction():
                # Serializes schema initialization across concurrently starting replicas.
                self.db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (schema,))
                self.db.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
                )
                self.db.execute(sql.SQL("SET search_path TO {} ").format(sql.Identifier(schema)))
                self.db.execute("""CREATE TABLE IF NOT EXISTS metadata (
                    singleton BOOLEAN PRIMARY KEY CHECK(singleton), blocked BOOLEAN NOT NULL DEFAULT FALSE,
                    chain_tip TEXT NOT NULL DEFAULT '')""")
                self.db.execute(
                    "INSERT INTO metadata(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING"
                )
                self.db.execute("""CREATE TABLE IF NOT EXISTS owners (
                    owner_id TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS events (
                    seq BIGSERIAL PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, payload JSONB NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY, intent JSONB NOT NULL, owner_id TEXT NOT NULL REFERENCES owners,
                    completed BOOLEAN NOT NULL DEFAULT FALSE)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                    seq BIGINT NOT NULL REFERENCES events, sink TEXT NOT NULL, claimed_by TEXT,
                    claimed_until TIMESTAMPTZ, PRIMARY KEY(seq,sink))""")
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS pending_by_sink ON deliveries(sink,seq)"
                )
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS pending_operations ON operations(owner_id) WHERE NOT completed"
                )
                self.db.execute("""CREATE TABLE IF NOT EXISTS lease_quarantines (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    lease_ref TEXT NOT NULL, resolved BOOLEAN NOT NULL DEFAULT FALSE)""")
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS quarantine_by_lease ON lease_quarantines(lease_ref,resolved)"
                )
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS audit_tenant_seq ON events((payload->>'tenant_hash'),seq)"
                )
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS audit_operation ON events((payload->>'operation_id'),seq)"
                )
                self.db.execute(
                    "INSERT INTO owners VALUES(%s,clock_timestamp()+%s*interval '1 second')",
                    (self.owner_id, owner_ttl),
                )
            # SET LOCAL would revert after the initialization transaction.
            self.db.execute(sql.SQL("SET search_path TO {} ").format(sql.Identifier(schema)))
        except Exception:
            self.db.close()
            raise
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()

    def _check(self) -> str:
        if self._closed or self._local_failed:
            raise AuditUnavailable("audit owner is fenced or unavailable")
        row = self.db.execute(
            "SELECT blocked,chain_tip FROM metadata WHERE singleton FOR UPDATE"
        ).fetchone()
        alive = self.db.execute(
            "SELECT 1 FROM owners WHERE owner_id=%s AND expires_at>clock_timestamp()",
            (self.owner_id,),
        ).fetchone()
        abandoned = self.db.execute("""SELECT 1 FROM operations o JOIN owners w USING(owner_id)
            WHERE NOT o.completed AND w.expires_at<=clock_timestamp() LIMIT 1""").fetchone()
        if row[0] or not alive or abandoned:
            raise AuditUnavailable(
                "unresolved operations or expired audit owner require reconciliation"
            )
        return row[1]

    @property
    def failed(self) -> bool:
        try:
            self.ensure_ready()
            return False
        except Exception:
            return True

    @failed.setter
    def failed(self, value: bool) -> None:
        if not value:
            raise AuditUnavailable("only offline reconciliation can clear the shared audit block")
        self._local_failed = True
        self._stop.set()
        try:
            with self._lock:
                self.db.execute("UPDATE metadata SET blocked=TRUE WHERE singleton")
        except Exception:
            # The incomplete intent remains authoritative if the DB is offline.
            pass

    def ensure_ready(self) -> None:
        try:
            with self._lock, self.db.transaction():
                self._check()
        except AuditUnavailable:
            raise
        except Exception as error:
            self._local_failed = True
            raise AuditUnavailable("PostgreSQL audit is unavailable") from error

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.owner_ttl / 3):
            try:
                with self._lock:
                    row = self.db.execute(
                        """UPDATE owners SET expires_at=clock_timestamp()+%s*interval '1 second'
                        WHERE owner_id=%s AND expires_at>clock_timestamp() RETURNING owner_id""",
                        (self.owner_ttl, self.owner_id),
                    ).fetchone()
                if row is None:
                    self._local_failed = True
                    return
            except Exception:
                self._local_failed = True
                return

    def _insert(self, event: dict[str, Any], tip: str) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        value = {
            **event,
            "ts": event.get("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            "event_id": event.get("event_id", uuid.uuid4().hex),
            "previous_sha256": tip,
        }
        value.pop("event_sha256", None)
        value["event_sha256"] = hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        seq = self.db.execute(
            "INSERT INTO events(event_id,payload) VALUES(%s,%s) RETURNING seq",
            (value["event_id"], Jsonb(value)),
        ).fetchone()[0]
        for sink in self.sinks:
            self.db.execute("INSERT INTO deliveries(seq,sink) VALUES(%s,%s)", (seq, sink))
        self.db.execute(
            "UPDATE metadata SET chain_tip=%s WHERE singleton", (value["event_sha256"],)
        )
        return value

    def append(
        self,
        event: dict[str, Any],
        *,
        begin: str | None = None,
        finish: str | None = None,
        quarantine_lease: str | None = None,
        clear_lease: str | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        try:
            with self._lock, self.db.transaction():
                tip = self._check()
                value = self._insert(event, tip)
                if begin:
                    self.db.execute(
                        "INSERT INTO operations(operation_id,intent,owner_id) VALUES(%s,%s,%s)",
                        (begin, Jsonb(value), self.owner_id),
                    )
                if finish:
                    row = self.db.execute(
                        """UPDATE operations SET completed=TRUE
                        WHERE operation_id=%s AND owner_id=%s AND NOT completed RETURNING operation_id""",
                        (finish, self.owner_id),
                    ).fetchone()
                    if row is None:
                        raise AuditUnavailable("operation owner or intent mismatch")
                if quarantine_lease:
                    if not finish:
                        raise AuditUnavailable("quarantine requires a committed result")
                    self.db.execute(
                        "INSERT INTO lease_quarantines(operation_id,lease_ref) VALUES(%s,%s)",
                        (finish, quarantine_lease),
                    )
                if clear_lease:
                    self.db.execute(
                        "UPDATE lease_quarantines SET resolved=TRUE WHERE lease_ref=%s",
                        (clear_lease,),
                    )
            return value
        except Exception as error:
            self.failed = True
            raise AuditUnavailable(
                "PostgreSQL audit commit failed; execution is blocked"
            ) from error

    def lease_quarantined(self, lease_ref: str) -> bool:
        try:
            with self._lock:
                return (
                    self.db.execute(
                        "SELECT 1 FROM lease_quarantines WHERE lease_ref=%s AND NOT resolved LIMIT 1",
                        (lease_ref,),
                    ).fetchone()
                    is not None
                )
        except Exception as error:
            self.failed = True
            raise AuditUnavailable("lease quarantine authority unavailable") from error

    def quarantine_operations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**row[0], "quarantined": True}
                for row in self.db.execute(
                    "SELECT o.intent FROM lease_quarantines q JOIN operations o USING(operation_id) WHERE NOT q.resolved ORDER BY o.operation_id"
                )
            ]

    def pending_operations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                row[0]
                for row in self.db.execute(
                    "SELECT intent FROM operations WHERE NOT completed ORDER BY operation_id"
                )
            ]

    def counts(self) -> tuple[int, int]:
        with self._lock:
            row = self.db.execute(
                "SELECT (SELECT count(*) FROM deliveries),(SELECT count(*) FROM operations WHERE NOT completed)"
            ).fetchone()
            return row[0], row[1]

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(reversed(self.query(limit=limit)["events"]))

    def query(
        self, *, limit: int = 100, before: int | None = None, filters: dict[str, str] | None = None
    ) -> dict[str, Any]:
        clauses: list[Any] = []
        parameters: list[Any] = []
        if before is not None:
            clauses.append(self._sql.SQL("seq < %s"))
            parameters.append(before)
        for key, value in (filters or {}).items():
            if key not in {
                "action",
                "operation_id",
                "lease_ref",
                "task_ref",
                "backend_id",
                "tenant_hash",
            }:
                raise ValueError("unsupported audit filter")
            clauses.append(self._sql.SQL("payload->>%s = %s"))
            parameters.extend([key, value])
        where = (
            self._sql.SQL(" WHERE ") + self._sql.SQL(" AND ").join(clauses)
            if clauses
            else self._sql.SQL("")
        )
        with self._lock:
            rows = self.db.execute(
                self._sql.SQL("SELECT seq,payload FROM events")
                + where
                + self._sql.SQL(" ORDER BY seq DESC LIMIT %s"),
                [*parameters, limit + 1],
            ).fetchall()
        events = [{"seq": row[0], **row[1]} for row in rows[:limit]]
        return {
            "events": events,
            "next_cursor": events[-1]["seq"] if len(rows) > limit else None,
            "durable": True,
        }

    def next_delivery(self, sink: str) -> tuple[int, dict[str, Any]] | None:
        with self._lock, self.db.transaction():
            row = self.db.execute(
                """SELECT d.seq,e.payload FROM deliveries d JOIN events e USING(seq)
                WHERE sink=%s AND (claimed_until IS NULL OR claimed_until<clock_timestamp())
                ORDER BY seq FOR UPDATE OF d SKIP LOCKED LIMIT 1""",
                (sink,),
            ).fetchone()
            if row:
                self.db.execute(
                    """UPDATE deliveries SET claimed_by=%s,claimed_until=clock_timestamp()+interval '30 seconds'
                    WHERE seq=%s AND sink=%s""",
                    (self.owner_id, row[0], sink),
                )
            return (row[0], row[1]) if row else None

    def acknowledge(self, seq: int, sink: str) -> None:
        with self._lock:
            self.db.execute(
                "DELETE FROM deliveries WHERE seq=%s AND sink=%s AND claimed_by=%s",
                (seq, sink, self.owner_id),
            )

    def reconcile(self, operation_id: str, operator_hash: str, note_hash: str) -> None:
        # Run only with execution stopped. Never clears another unresolved intent.
        with self._lock, self.db.transaction():
            tip = self.db.execute(
                "SELECT chain_tip FROM metadata WHERE singleton FOR UPDATE"
            ).fetchone()[0]
            self._require_offline()
            row = self.db.execute(
                "UPDATE operations SET completed=TRUE WHERE operation_id=%s AND NOT completed RETURNING operation_id",
                (operation_id,),
            ).fetchone()
            quarantine = self.db.execute(
                "UPDATE lease_quarantines SET resolved=TRUE WHERE operation_id=%s AND NOT resolved RETURNING operation_id",
                (operation_id,),
            ).fetchone()
            if row is None and quarantine is None:
                raise ValueError("unknown or already reconciled operation")
            self._insert(
                {
                    "action": "audit_reconcile",
                    "operation_id": operation_id,
                    "outcome": "manually_reconciled",
                    "operator_hash": operator_hash,
                    "note_sha256": note_hash,
                    "runtime": "admin",
                },
                tip,
            )
            if not self.db.execute(
                "SELECT 1 FROM operations WHERE NOT completed LIMIT 1"
            ).fetchone():
                self.db.execute("UPDATE metadata SET blocked=FALSE WHERE singleton")
        # A fenced process may not rejoin. Restart the Adapter after reconciliation.

    def _require_offline(self) -> None:
        if self.db.execute(
            "SELECT 1 FROM owners WHERE owner_id<>%s AND expires_at>clock_timestamp() LIMIT 1",
            (self.owner_id,),
        ).fetchone():
            raise AuditUnavailable("stop all Adapter writers before offline reconciliation")

    def acknowledge_block(self, operator_hash: str, note_hash: str) -> None:
        with self._lock, self.db.transaction():
            tip = self.db.execute(
                "SELECT chain_tip FROM metadata WHERE singleton FOR UPDATE"
            ).fetchone()[0]
            self._require_offline()
            if self.db.execute("SELECT 1 FROM operations WHERE NOT completed LIMIT 1").fetchone():
                raise AuditUnavailable(
                    "reconcile every unresolved intent before acknowledging a block"
                )
            self._insert(
                {
                    "action": "audit_block_acknowledged",
                    "outcome": "manually_reconciled",
                    "operator_hash": operator_hash,
                    "note_sha256": note_hash,
                    "runtime": "admin",
                },
                tip,
            )
            self.db.execute("UPDATE metadata SET blocked=FALSE WHERE singleton")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        with self._lock:
            if self._closed:
                return
            try:
                self.db.execute(
                    "UPDATE owners SET expires_at=clock_timestamp() WHERE owner_id=%s",
                    (self.owner_id,),
                )
            finally:
                self._closed = True
                self.db.close()


def import_sqlite(target: PostgresAuditJournal, source_path: str) -> dict[str, Any]:
    """Explicit atomic migration to an empty schema; source history is retained."""
    from pathlib import Path

    from psycopg.types.json import Jsonb

    from .audit_admin import verify_chain
    from .audit_journal import AuditJournal

    if not Path(source_path).is_file():
        raise RuntimeError("source journal does not exist")
    source = AuditJournal(source_path, ())
    saved_sinks = target.sinks
    try:
        if source.pending_operations() or source.quarantine_operations():
            raise AuditUnavailable("reconcile source operations before migration")
        verification = verify_chain(source)
        with target._lock, target.db.transaction():
            target._check()
            target._require_offline()
            if target.db.execute("SELECT 1 FROM events LIMIT 1").fetchone():
                raise RuntimeError("migration target is not empty")
            target.sinks = ()
            tip, count = "", 0
            for seq, raw in source.db.execute("SELECT seq,payload FROM events ORDER BY seq"):
                value = json.loads(raw)
                old_hash = value.pop("event_sha256", None)
                value["source_seq"] = seq
                value["source_event_sha256"] = old_hash
                event = target._insert(value, tip)
                tip = event["event_sha256"]
                count += 1
            for operation_id, raw in source.db.execute(
                "SELECT operation_id,intent FROM operations"
            ):
                target.db.execute(
                    "INSERT INTO operations(operation_id,intent,owner_id,completed) VALUES(%s,%s,%s,TRUE)",
                    (operation_id, Jsonb(json.loads(raw)), target.owner_id),
                )
            for seq, sink in source.db.execute("SELECT seq,sink FROM deliveries"):
                target.db.execute(
                    """INSERT INTO deliveries(seq,sink)
                    SELECT seq,%s FROM events WHERE payload->>'source_seq'=%s""",
                    (sink, str(seq)),
                )
        return {
            "imported_events": count,
            "source_chain": verification,
            "chain_tip": tip,
            "source_modified": False,
            "historical_replay": False,
        }
    finally:
        target.sinks = saved_sinks
        source.close()
