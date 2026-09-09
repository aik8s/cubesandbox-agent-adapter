from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from adapter.audit import AuditManager, HttpAuditSink
from adapter.audit_journal import AuditJournal, AuditUnavailable
from adapter.config import AdapterConfig, AuthContext
from adapter.core import AdapterError, CubeAdapter
from adapter.metrics import AdapterMetrics
from adapter.test_support import FakeSandbox, fake_template


class DurableAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "audit.jsonl.sqlite3")
        self.config = AdapterConfig(
            token="test-token-with-at-least-24-chars",
            session_hmac_key="test-hmac-key-with-at-least-32-chars",
            template="agent-code",
            audit_log=str(Path(self.tmp.name) / "audit.jsonl"),
        )
        self.resources = []
        FakeSandbox.reset()

    def tearDown(self):
        for item in reversed(self.resources):
            item.close()
        self.tmp.cleanup()

    def journal(self):
        journal = AuditJournal(self.path, ("http",))
        self.resources.append(journal)
        return journal

    def adapter(self):
        adapter = CubeAdapter(
            self.config,
            sandbox_factory=FakeSandbox.create,
            sandbox_connector=FakeSandbox.connect,
            template_checker=fake_template,
            start_gc=False,
        )
        self.resources.append(adapter)
        return adapter

    def test_committed_event_survives_abrupt_process_exit(self):
        code = """from adapter.audit_journal import AuditJournal
import os,sys
j = AuditJournal(sys.argv[1], ('http',))
j.append({'action':'durability-test'}, begin='unfinished')
os._exit(17)
"""
        result = subprocess.run([sys.executable, "-c", code, self.path], check=False)  # noqa: S603 - fixed test program
        self.assertEqual(result.returncode, 17)
        journal = self.journal()
        self.assertEqual(len(journal.recent()), 1)
        self.assertEqual(len(journal.pending_operations()), 1)
        self.assertIsNotNone(journal.next_delivery("http"))
        with self.assertRaises(AuditUnavailable):
            journal.append({"action": "must-not-run"})

    def test_storage_error_before_intent_blocks_sandbox_creation(self):
        adapter = self.adapter()
        journal = adapter.audit.journal
        with mock.patch.object(
            journal, "_insert", side_effect=sqlite3.OperationalError("disk full")
        ):
            with self.assertRaises(AdapterError) as raised:
                adapter.acquire({"runtime": "mcp", "session_key": "test"})
        self.assertEqual(raised.exception.code, "audit_unavailable")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(FakeSandbox.created, [])
        self.assertEqual(adapter.readiness(force=True)[0], 503)

    def test_result_commit_failure_preserves_pending_intent(self):
        adapter = self.adapter()
        journal = adapter.audit.journal
        insert = journal._insert

        def fail_result(event):
            if event.get("phase") == "result":
                raise sqlite3.OperationalError("disk full after external effect")
            return insert(event)

        with mock.patch.object(journal, "_insert", side_effect=fail_result):
            with self.assertRaises(AdapterError):
                adapter.acquire({"runtime": "mcp", "session_key": "test"})
        self.assertEqual(len(FakeSandbox.created), 1)
        pending = journal.pending_operations()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "acquire")
        with self.assertRaises(AdapterError):
            adapter.acquire({"runtime": "mcp", "session_key": "test"})
        self.assertEqual(len(FakeSandbox.created), 1)

    def test_independent_sink_ack_and_replay_after_restart(self):
        journal = self.journal()
        journal.append({"action": "retry-me"})
        first = journal.next_delivery("http")
        self.assertIsNotNone(first)
        journal.close()
        reopened = self.journal()
        self.assertEqual(reopened.next_delivery("http"), first)
        reopened.acknowledge(first[0], "http")
        self.assertIsNone(reopened.next_delivery("http"))
        self.assertEqual(len(reopened.recent()), 1)  # Delivery never deletes authority.

    def test_real_worker_retries_same_event_id_after_sink_failure(self):
        from dataclasses import replace

        config = replace(
            self.config, audit_sinks=("http",), audit_http_url="http://collector.invalid"
        )
        attempts = []

        def deliver(_sink, event):
            attempts.append(event["event_id"])
            if len(attempts) == 1:
                raise OSError("collector unavailable")

        with mock.patch.object(HttpAuditSink, "emit", deliver):
            manager = AuditManager.from_config(config, AdapterMetrics())
            try:
                manager.emit({"action": "external-outage"})
                deadline = time.monotonic() + 3
                while len(attempts) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(len(attempts), 2)
                self.assertEqual(attempts[0], attempts[1])
                self.assertEqual(len(manager.recent()), 1)
            finally:
                manager.close()

    def test_offline_reconciliation_keeps_original_and_resolution(self):
        journal = self.journal()
        journal.append({"action": "uncertain"}, begin="op1")
        journal.close()
        reopened = self.journal()
        self.assertTrue(reopened.failed)
        reopened.reconcile("op1", "operator-hash", "evidence-hash")
        reopened.close()
        recovered = self.journal()
        self.assertFalse(recovered.failed)
        self.assertEqual(recovered.pending_operations(), [])
        self.assertEqual(
            [r["action"] for r in recovered.recent()], ["uncertain", "audit_reconcile"]
        )

    def test_second_process_cannot_share_journal(self):
        self.journal()
        result = subprocess.run(  # noqa: S603 - fixed test program
            [
                sys.executable,
                "-c",  # noqa: S603 - fixed test program
                "from adapter.audit_journal import AuditJournal; import sys; AuditJournal(sys.argv[1], ())",
                self.path,
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_concurrent_appends_are_complete_and_unique(self):
        journal = self.journal()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(lambda i: journal.append({"action": "concurrent", "index": i}), range(80))
            )
        rows = journal.recent()
        self.assertEqual(len(rows), 80)
        self.assertEqual(len({r["event_id"] for r in rows}), 80)

    def test_authority_and_lock_permissions_are_private(self):
        Path(self.path).touch(mode=0o644)
        os.chmod(self.path, 0o644)
        journal = self.journal()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.path + ".lock").st_mode & 0o777, 0o600)
        journal.append({"action": "permission-check"})

    def test_denied_action_is_durable_and_redacted(self):
        adapter = self.adapter()
        auth = AuthContext("tenant", "private-subject", allowed_actions=frozenset({"task:plan"}))
        with self.assertRaises(AdapterError):
            adapter.exec("lease_" + "a" * 20, {"command": "super-private-command"}, auth)
        events = adapter.audit.recent()
        self.assertEqual(events[-1]["outcome"], "rejected")
        encoded = json.dumps(events)
        self.assertNotIn("super-private-command", encoded)
        self.assertNotIn("private-subject", encoded)
        self.assertIn("request_hmac_sha256", encoded)

    def test_best_effort_is_explicit_and_has_no_journal(self):
        from dataclasses import replace

        manager = AuditManager.from_config(
            replace(self.config, audit_mode="best_effort"), AdapterMetrics()
        )
        try:
            self.assertIsNone(manager.journal)
        finally:
            manager.close()

    def test_invalid_mode_fails_startup(self):
        from dataclasses import replace

        with self.assertRaises(RuntimeError):
            AuditManager.from_config(replace(self.config, audit_mode="typo"), AdapterMetrics())

    def test_all_public_execution_entrypoints_are_guarded(self):
        import inspect

        exempt = {
            "authorize",
            "health",
            "readiness",
            "metrics_payload",
            "audit_html",
            "close",
        }
        for name, method in inspect.getmembers(CubeAdapter, inspect.isfunction):
            if not name.startswith("_") and name not in exempt:
                self.assertTrue(hasattr(method, "__wrapped__"), name)

    def test_monitoring_exposes_required_and_blocked_mode(self):
        adapter = self.adapter()
        adapter.audit.journal.failed = True
        metrics = adapter.metrics_payload().decode()
        self.assertIn("cube_adapter_audit_required 1.0", metrics)
        self.assertIn("cube_adapter_audit_blocked 1.0", metrics)

    def test_environment_defaults_to_required(self):
        with mock.patch.dict(
            os.environ,
            {
                "CUBE_ADAPTER_TOKEN": self.config.token,
                "CUBE_TEMPLATE_ID": "agent-code",
                "CUBE_ADAPTER_HMAC_KEY": self.config.session_hmac_key,
            },
            clear=True,
        ):
            self.assertEqual(AdapterConfig.from_env().audit_mode, "required")

    def test_commit_failure_rolls_back_result_and_keeps_intent(self):
        journal = self.journal()
        journal.append({"phase": "intent"}, begin="op1")

        def authorizer(action, arg1, _arg2, _db, _source):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        journal.db.set_authorizer(authorizer)
        with self.assertRaises(AuditUnavailable):
            journal.append({"phase": "result"}, finish="op1")
        journal.db.set_authorizer(None)
        self.assertEqual(len(journal.pending_operations()), 1)
        self.assertEqual(len(journal.recent()), 1)
        self.assertTrue(journal.failed)

    def test_sigkill_recovers_committed_intent_and_blocks_restart(self):
        code = """from adapter.audit_journal import AuditJournal
import os,sys,signal
j=AuditJournal(sys.argv[1], ())
j.append({'action':'external-call'}, begin='crash-window')
os.kill(os.getpid(), signal.SIGKILL)
"""
        result = subprocess.run([sys.executable, "-c", code, self.path], check=False)  # noqa: S603 - fixed test program
        self.assertEqual(result.returncode, -9)
        journal = self.journal()
        self.assertTrue(journal.failed)
        self.assertEqual(journal.pending_operations()[0]["action"], "external-call")

    def test_http_returns_503_without_external_execution(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        from adapter.http_api import make_handler

        adapter = self.adapter()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(adapter))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter.audit.journal.db.execute("PRAGMA query_only=ON")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/leases",
                data=b'{"runtime":"mcp","session_key":"test"}',
                headers={
                    "Authorization": "Bearer " + self.config.token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request)
            self.assertEqual(raised.exception.code, 503)
            self.assertEqual(FakeSandbox.created, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
