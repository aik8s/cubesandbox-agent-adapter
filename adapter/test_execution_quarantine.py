from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from adapter import test_audit_durability as audit_tests
from adapter.audit_admin import verify_chain
from adapter.audit_journal import AuditJournal
from adapter.config import AuthContext
from adapter.core import AdapterError


class ExecutionQuarantineTest(unittest.TestCase):
    setUp = audit_tests.DurableAuditTest.setUp
    tearDown = audit_tests.DurableAuditTest.tearDown
    journal = audit_tests.DurableAuditTest.journal
    adapter = audit_tests.DurableAuditTest.adapter

    def quarantine(self, adapter, lease):
        sandbox = adapter._handles[lease["lease_ref"]]
        with mock.patch.object(
            sandbox.commands, "run", side_effect=RuntimeError("command timed out")
        ):
            with self.assertRaises(AdapterError) as raised:
                adapter.exec(lease["lease_ref"], {"command": "sleep 300", "timeout_ms": 1})
        self.assertEqual(raised.exception.status, 504)
        self.assertEqual(raised.exception.code, "execution_timeout")
        return sandbox

    def test_timeout_isolates_lease_not_service_and_allows_kill(self):
        adapter = self.adapter()
        one = adapter.acquire({"runtime": "dsh", "session_key": "uncertain"})
        sandbox = self.quarantine(adapter, one)
        journal = adapter.audit.journal
        self.assertFalse(journal.failed)
        self.assertEqual(journal.pending_operations(), [])
        self.assertTrue(journal.lease_quarantined(one["lease_ref"]))
        self.assertEqual(adapter.readiness(force=True)[0], 200)
        with mock.patch.object(sandbox.commands, "run") as run:
            with self.assertRaises(AdapterError) as raised:
                adapter.exec(one["lease_ref"], {"command": "must-not-run"})
            self.assertEqual(raised.exception.code, "lease_quarantined")
            run.assert_not_called()
        with self.assertRaises(AdapterError):
            adapter.write(one["lease_ref"], {"path": "/workspace/blocked", "content": "blocked"})
        with self.assertRaises(AdapterError):
            adapter.release(one["lease_ref"], {"action": "pause"})
        reused = adapter.acquire({"runtime": "dsh", "session_key": "uncertain"})
        self.assertEqual(reused["lease_ref"], one["lease_ref"])
        self.assertTrue(adapter.lease_status(one["lease_ref"])["quarantined"])
        two = adapter.acquire({"runtime": "dsh", "session_key": "healthy"})
        self.assertEqual(
            adapter.exec(two["lease_ref"], {"command": "echo healthy"})["exit_code"], 0
        )
        adapter.release(one["lease_ref"], {"action": "kill"})
        self.assertTrue(sandbox.killed)
        self.assertFalse(journal.lease_quarantined(one["lease_ref"]))
        adapter.release(two["lease_ref"], {"action": "kill"})
        self.assertTrue(any(e.get("outcome") == "uncertain" for e in journal.recent()))
        self.assertNotIn("sleep 300", json.dumps(journal.recent()))
        verify_chain(journal)

    def test_quarantine_survives_restart_without_global_block(self):
        adapter = self.adapter()
        lease = adapter.acquire({"runtime": "dsh", "session_key": "restart"})
        self.quarantine(adapter, lease)
        adapter.close()
        journal = AuditJournal(self.path, ())
        self.resources.append(journal)
        self.assertFalse(journal.failed)
        self.assertTrue(journal.lease_quarantined(lease["lease_ref"]))
        self.assertEqual(len(journal.quarantine_operations()), 1)
        journal.reconcile(
            journal.quarantine_operations()[0]["operation_id"], "operator", "evidence"
        )
        self.assertFalse(journal.lease_quarantined(lease["lease_ref"]))
        verify_chain(journal)

    def test_result_commit_failure_still_blocks_globally(self):
        adapter = self.adapter()
        lease = adapter.acquire({"runtime": "dsh", "session_key": "disk-failure"})
        sandbox = adapter._handles[lease["lease_ref"]]
        insert = adapter.audit.journal._insert

        def fail(event):
            if event.get("outcome") == "uncertain":
                raise sqlite3.OperationalError("disk full")
            return insert(event)

        with mock.patch.object(
            sandbox.commands, "run", side_effect=RuntimeError("command timed out")
        ):
            with mock.patch.object(adapter.audit.journal, "_insert", side_effect=fail):
                with self.assertRaises(AdapterError) as raised:
                    adapter.exec(lease["lease_ref"], {"command": "sleep 300"})
        self.assertEqual(raised.exception.code, "audit_unavailable")
        self.assertTrue(adapter.audit.journal.failed)
        self.assertEqual(adapter.readiness(force=True)[0], 503)
        self.assertEqual(len(adapter.audit.journal.pending_operations()), 1)

    def test_unknown_upstream_failure_is_scoped_and_tenant_private(self):
        adapter = self.adapter()
        lease = adapter.acquire({"runtime": "dsh", "session_key": "unknown"})
        sandbox = adapter._handles[lease["lease_ref"]]
        with mock.patch.object(
            sandbox.commands, "run", side_effect=RuntimeError("upstream disconnected")
        ):
            with self.assertRaises(AdapterError) as raised:
                adapter.exec(lease["lease_ref"], {"command": "echo unknown"})
        self.assertEqual(raised.exception.status, 502)
        self.assertFalse(adapter.audit.journal.failed)
        with self.assertRaises(AdapterError) as denied:
            adapter.exec(
                lease["lease_ref"],
                {"command": "echo cross-tenant"},
                AuthContext("other", "subject"),
            )
        self.assertEqual(denied.exception.status, 404)
        self.assertEqual(adapter.lease_status(lease["lease_ref"])["state"], "running")
        self.assertIn(
            "cube_adapter_audit_quarantined_operations 1.0", adapter.metrics_payload().decode()
        )

    def test_quarantine_transaction_failure_rolls_back_completion(self):
        journal = self.journal()
        journal.append({"action": "exec", "lease_ref": "lease_example"}, begin="op")
        journal.db.execute("DROP TABLE lease_quarantines")
        from adapter.audit_journal import AuditUnavailable

        with self.assertRaises(AuditUnavailable):
            journal.append({"outcome": "uncertain"}, finish="op", quarantine_lease="lease_example")
        self.assertTrue(journal.failed)
        self.assertEqual(len(journal.pending_operations()), 1)
        self.assertEqual(len(journal.recent()), 1)

    def test_concurrent_retry_cannot_slip_past_quarantine_commit(self):
        adapter = self.adapter()
        lease = adapter.acquire({"runtime": "dsh", "session_key": "racing"})
        sandbox = adapter._handles[lease["lease_ref"]]
        started, finish = threading.Event(), threading.Event()

        def timeout(*_args, **_kwargs):
            started.set()
            self.assertTrue(finish.wait(5))
            raise RuntimeError("deadline_exceeded: context deadline exceeded")

        def execute():
            try:
                adapter.exec(lease["lease_ref"], {"command": "sleep 300"})
            except AdapterError as error:
                return error.code
            self.fail("uncertain command was retried")

        with mock.patch.object(sandbox.commands, "run", side_effect=timeout) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(execute)
                self.assertTrue(started.wait(5))
                second = pool.submit(execute)
                finish.set()
                self.assertEqual(first.result(timeout=5), "execution_timeout")
                self.assertEqual(second.result(timeout=5), "lease_quarantined")
            self.assertEqual(run.call_count, 1)
        self.assertFalse(adapter.audit.journal.failed)
        self.assertEqual(adapter.audit.journal.pending_operations(), [])
