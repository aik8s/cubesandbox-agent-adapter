from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adapter.audit_admin import export_archive, verify_chain
from adapter.audit_journal import AuditJournal, AuditUnavailable
from adapter.postgres_audit import PostgresAuditJournal, import_sqlite


@unittest.skipUnless(
    os.getenv("CUBE_ADAPTER_TEST_POSTGRES_URL"),
    "isolated PostgreSQL integration URL not configured",
)
class PostgresAuditTest(unittest.TestCase):
    def setUp(self):
        import psycopg

        self.url = os.environ["CUBE_ADAPTER_TEST_POSTGRES_URL"]
        self.schema = "audit_test_" + uuid.uuid4().hex
        self.control = psycopg.connect(self.url, autocommit=True)
        self.journals = []

    def tearDown(self):
        from psycopg import sql

        for journal in self.journals:
            journal.close()
        # Only the unique, test-owned schema is deleted.
        self.control.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        self.control.close()

    def journal(self, sinks=("http",)):
        journal = PostgresAuditJournal(self.url, sinks, schema=self.schema)
        self.journals.append(journal)
        return journal

    def test_parallel_writers_hash_chain_and_outbox_claims(self):
        one, two = self.journal(), self.journal()

        def write(item):
            journal = one if item % 2 else two
            operation = "parallel-" + str(item)
            journal.append({"action": "parallel", "phase": "intent"}, begin=operation)
            journal.append({"action": "parallel", "phase": "result"}, finish=operation)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))
        self.assertEqual(one.counts(), (80, 0))
        self.assertEqual(verify_chain(one)["hashed_events"], 80)
        first, second = one.next_delivery("http"), two.next_delivery("http")
        self.assertNotEqual(first[0], second[0])
        one.acknowledge(second[0], "http")
        self.assertEqual(one.counts()[0], 80)
        one.acknowledge(first[0], "http")
        self.assertEqual(one.counts()[0], 79)

    def test_quarantined_result_does_not_fence_other_writers_or_restart(self):
        one, two = self.journal(), self.journal()
        one.append({"action": "exec", "lease_ref": "lease_test"}, begin="uncertain-exec")
        one.append(
            {"action": "exec", "outcome": "uncertain"},
            finish="uncertain-exec",
            quarantine_lease="lease_test",
        )
        self.assertTrue(two.lease_quarantined("lease_test"))
        self.assertEqual(one.pending_operations(), [])
        two.ensure_ready()
        one.close()
        two.ensure_ready()
        three = self.journal()
        self.assertTrue(three.lease_quarantined("lease_test"))
        three.append(
            {"action": "release", "quarantine_resolution": "lease_destroyed"},
            clear_lease="lease_test",
        )
        self.assertFalse(two.lease_quarantined("lease_test"))
        self.assertEqual(three.quarantine_operations(), [])
        verify_chain(three)

    def test_live_intent_is_allowed_but_dead_owner_blocks_all_replicas(self):
        one = self.journal()
        one.append({"action": "external", "phase": "intent"}, begin="incomplete")
        two = self.journal()
        two.ensure_ready()
        one.close()
        with self.assertRaises(AuditUnavailable):
            two.ensure_ready()
        third = self.journal()
        with self.assertRaises(AuditUnavailable):
            two.reconcile("incomplete", "operator", "evidence")
        third.close()
        two.close()
        admin = self.journal()
        self.assertEqual(len(admin.pending_operations()), 1)
        admin.reconcile("incomplete", "operator", "evidence")
        admin.close()
        resumed = self.journal()
        resumed.ensure_ready()
        self.assertEqual(resumed.counts()[1], 0)
        self.assertIn("manually_reconciled", json.dumps(resumed.recent()))

    def test_expired_owner_cannot_rejoin_after_clock_fencing(self):
        one, two = self.journal(), self.journal()
        from psycopg import sql

        self.control.execute(
            sql.SQL(
                "UPDATE {}.owners SET expires_at=clock_timestamp()-interval '1 second' WHERE owner_id=%s"
            ).format(sql.Identifier(self.schema)),
            (one.owner_id,),
        )
        with self.assertRaises(AuditUnavailable):
            one.append({"action": "must-not-run"})
        with self.assertRaises(AuditUnavailable):
            two.append({"action": "must-not-run"})
        self.assertEqual(len(one.recent()), 0)

    def test_abrupt_process_exit_keeps_intent_and_blocks_restart(self):
        one = self.journal()
        code = """import os
from adapter.postgres_audit import PostgresAuditJournal
j=PostgresAuditJournal(os.environ['CUBE_ADAPTER_TEST_POSTGRES_URL'],('http',),schema=os.environ['CUBE_TEST_SCHEMA'])
j.append({'action':'crash-test'},begin='crashed')
os._exit(17)
"""
        result = subprocess.run(  # noqa: S603 -- fixed isolated test process
            [sys.executable, "-c", code],
            env={**os.environ, "CUBE_TEST_SCHEMA": self.schema},
            check=False,
        )  # noqa: S603 -- fixed isolated test process
        self.assertEqual(result.returncode, 17)
        from psycopg import sql

        self.control.execute(
            sql.SQL(
                "UPDATE {}.owners SET expires_at=clock_timestamp()-interval '1 second' WHERE owner_id<>%s"
            ).format(sql.Identifier(self.schema)),
            (one.owner_id,),
        )
        self.assertTrue(one.failed)
        self.assertEqual(one.counts(), (1, 1))
        self.assertEqual(one.pending_operations()[0]["action"], "crash-test")

    def test_offline_migration_export_and_restore_into_empty_schema(self):
        target = self.journal(())
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "source.sqlite3")
            source = AuditJournal(path, ("http",))
            source.append({"action": "source", "phase": "intent"}, begin="source")
            source.append({"action": "source", "phase": "result"}, finish="source")
            ids = [event["event_id"] for event in source.recent()]
            source.close()
            before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            result = import_sqlite(target, path)
            self.assertEqual(result["imported_events"], 2)
            self.assertEqual(before, hashlib.sha256(Path(path).read_bytes()).hexdigest())
            self.assertEqual([event["event_id"] for event in target.recent()], ids)
            self.assertEqual(target.counts(), (2, 0))
            self.assertEqual(verify_chain(target)["hashed_events"], 2)
            export_archive(target, str(Path(directory) / "archive.jsonl"))
            with self.assertRaises(RuntimeError):
                import_sqlite(target, path)


if __name__ == "__main__":
    unittest.main()
