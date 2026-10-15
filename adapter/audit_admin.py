"""Offline journal inspection/reconciliation. Stop the Adapter first.

python -m adapter.audit_admin --database /path/audit.jsonl.sqlite3 pending
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path

from .audit_journal import AuditJournal
from .postgres_audit import PostgresAuditJournal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database")
    source.add_argument(
        "--postgres-url-env",
        help="Environment variable holding the DSN; never put passwords in argv",
    )
    parser.add_argument("--schema", default="cube_adapter_audit")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending")
    export = commands.add_parser("export")
    export.add_argument(
        "--output", help="Exclusive private JSONL archive; creates a SHA256 manifest too"
    )
    commands.add_parser("verify")
    clear = commands.add_parser("acknowledge-block")
    clear.add_argument("--note", required=True)
    migration = commands.add_parser("import-sqlite")
    migration.add_argument(
        "--source",
        required=True,
        help="Stopped, reconciled source journal; target PostgreSQL must be empty",
    )
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument(
        "--note", required=True, help="External incident/evidence reference; stored as SHA256"
    )
    args = parser.parse_args()
    journal: AuditJournal | PostgresAuditJournal
    if args.database:
        if not Path(args.database).is_file():
            parser.error("journal does not exist; refusing to create an empty audit history")
        journal = AuditJournal(args.database, ())
    else:
        url = os.environ.get(args.postgres_url_env)
        if not url:
            parser.error("PostgreSQL DSN environment variable is missing")
        journal = PostgresAuditJournal(url, (), schema=args.schema)
    try:
        if args.command == "pending":
            print(json.dumps(journal.pending_operations(), indent=2))
        elif args.command == "export":
            if args.output:
                export_archive(journal, args.output)
            else:
                for row in journal.db.execute("SELECT payload FROM events ORDER BY seq"):
                    print(row[0] if isinstance(row[0], str) else json.dumps(row[0], sort_keys=True))
        elif args.command == "verify":
            print(json.dumps(verify_chain(journal), sort_keys=True))
        elif args.command == "import-sqlite":
            if not isinstance(journal, PostgresAuditJournal):
                parser.error("import-sqlite requires a PostgreSQL target")
            from .postgres_audit import import_sqlite

            print(json.dumps(import_sqlite(journal, args.source), sort_keys=True))
        elif args.command == "acknowledge-block":
            if not isinstance(journal, PostgresAuditJournal):
                parser.error("shared audit blocks are PostgreSQL-only")
            if not args.note.strip():
                parser.error("an evidence reference is required")
            journal.acknowledge_block(
                hashlib.sha256(getpass.getuser().encode()).hexdigest(),
                hashlib.sha256(args.note.encode()).hexdigest(),
            )
            print(
                "Shared block acknowledged. Restart execution only after checking external outcomes."
            )
        else:
            if not args.note.strip():
                parser.error("a reconciliation evidence reference is required")
            journal.reconcile(
                args.operation_id,
                hashlib.sha256(getpass.getuser().encode()).hexdigest(),
                hashlib.sha256(args.note.encode()).hexdigest(),
            )
            print(
                "Reconciliation recorded. It does not assert execution success. Restart the Adapter after resolving every pending operation."
            )
    finally:
        journal.close()


def verify_chain(journal) -> dict:
    previous, count, legacy = "", 0, 0
    for row in journal.db.execute("SELECT payload FROM events ORDER BY seq"):
        event = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
        digest = event.pop("event_sha256", None)
        if digest is None:
            legacy += 1
            previous = ""
            continue
        actual = hashlib.sha256(
            json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        if digest != actual or event.get("previous_sha256") != previous:
            raise RuntimeError("audit event hash chain verification failed")
        previous = digest
        count += 1
    return {
        "hashed_events": count,
        "legacy_unhashed_events": legacy,
        "chain_tip": previous,
        "note": "Local hashes detect changes only against a separately retained trusted anchor.",
    }


def export_archive(journal, path: str) -> None:
    target, manifest = Path(path), Path(path + ".manifest.json")
    if target.exists() or manifest.exists():
        raise RuntimeError("archive or manifest already exists; refusing overwrite")
    count, digest = 0, hashlib.sha256()
    transaction = (
        journal.db.transaction() if isinstance(journal, PostgresAuditJournal) else journal.db
    )
    with journal._lock, transaction:
        if isinstance(journal, PostgresAuditJournal):
            journal.db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        verify = verify_chain(journal)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            for row in journal.db.execute("SELECT payload FROM events ORDER BY seq"):
                value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                raw = (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()
                stream.write(raw)
                digest.update(raw)
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
    data = {
        "events": count,
        "sha256": digest.hexdigest(),
        "chain": verify,
        "format": "cube-audit-jsonl-v1",
    }
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(target.absolute().parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    print(json.dumps(data, sort_keys=True))


if __name__ == "__main__":
    main()
