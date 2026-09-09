"""Offline journal inspection/reconciliation. Stop the Adapter first.

python -m adapter.audit_admin --database /path/audit.jsonl.sqlite3 pending
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json

from .audit_journal import AuditJournal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending")
    commands.add_parser("export")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument(
        "--note", required=True, help="External incident/evidence reference; stored as SHA256"
    )
    args = parser.parse_args()
    from pathlib import Path

    if not Path(args.database).is_file():
        parser.error("journal does not exist; refusing to create an empty audit history")
    journal = AuditJournal(args.database, ())
    try:
        if args.command == "pending":
            print(json.dumps(journal.pending_operations(), indent=2))
        elif args.command == "export":
            for row in journal.db.execute("SELECT payload FROM events ORDER BY seq"):
                print(row[0])
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


if __name__ == "__main__":
    main()
