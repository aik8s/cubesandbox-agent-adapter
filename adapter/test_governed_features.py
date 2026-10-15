from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from adapter.audit_admin import export_archive, verify_chain
from adapter.config import AdapterConfig, AuthContext
from adapter.core import AdapterError, CubeAdapter
from adapter.mcp_policy import GatewayClient, McpPolicy
from adapter.test_support import FakeResult, FakeSandbox, fake_template


class Gateway:
    calls = []

    def __init__(self, *_args):
        pass

    def initialize(self):
        pass

    def request(self, method, params):
        self.calls.append((method, params))
        return {
            "content": [{"type": "text", "text": "gateway-private-test-token"}],
            "unsafe": "hidden",
        }

    def close(self):
        pass


class GovernedFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.adapters = []
        FakeSandbox.reset()
        Gateway.calls = []

    def tearDown(self):
        for item in self.adapters:
            item.close()
        self.tmp.cleanup()

    def adapter(self, **kwargs):
        profiles = self.root / "profiles.yaml"
        profiles.write_text(
            yaml.safe_dump(
                {
                    "profiles": {
                        "controlled": {
                            "template": "code",
                            "mcp_policy": "echo",
                            "keepalive_enabled": True,
                            "sandbox_timeout_seconds": 120,
                            "max_keepalive_seconds": 300,
                        }
                    }
                }
            )
        )
        policy = self.root / "mcp.yaml"
        policy.write_text(
            yaml.safe_dump(
                {
                    "policies": {
                        "echo": {
                            "servers": {"echo": {"run_cmd": "python /opt/echo.py"}},
                            "tools": {
                                "echo": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {"text": {"type": "string", "maxLength": 64}},
                                    "required": ["text"],
                                }
                            },
                        }
                    }
                }
            )
        )
        config = AdapterConfig(
            token="test-token-with-at-least-24-chars",
            session_hmac_key="test-key-with-at-least-32-chars",
            template="code",
            profiles_file=str(profiles),
            mcp_policies_file=str(policy),
            audit_log=str(self.root / ("audit-" + str(len(self.adapters)) + ".jsonl")),
            **kwargs,
        )
        adapter = CubeAdapter(
            config,
            sandbox_factory=FakeSandbox.create,
            sandbox_connector=FakeSandbox.connect,
            template_checker=fake_template,
            mcp_client_factory=Gateway,
            start_gc=False,
        )
        self.adapters.append(adapter)
        return adapter

    @staticmethod
    def lease(adapter, auth=None):
        return adapter.acquire(
            {"runtime": "mcp", "session_key": "test", "profile": "controlled"}, auth
        )["lease_ref"]

    def test_mcp_fixed_servers_allowlist_schema_redaction_and_audit(self):
        adapter = self.adapter()
        lease = self.lease(adapter)
        result = adapter.mcp_call(lease, {"name": "echo", "arguments": {"text": "approved-input"}})
        self.assertEqual(result["result"]["content"][0]["text"], "[REDACTED]")
        self.assertNotIn("unsafe", result["result"])
        self.assertEqual(len(adapter.mcp_list(lease)["tools"]), 1)
        self.assertIn("mcp", FakeSandbox.created[0].create_args)
        serialized = json.dumps(adapter.audit.recent())
        self.assertNotIn("approved-input", serialized)
        self.assertNotIn("gateway-private-test-token", serialized)
        for name, args in (
            ("not-allowed", {}),
            ("echo", {"text": "okay", "endpoint": "https://evil.invalid"}),
            ("echo", {"text": "x" * 65}),
        ):
            with self.assertRaises(AdapterError):
                adapter.mcp_call(lease, {"name": name, "arguments": args})
        self.assertEqual(len(Gateway.calls), 1)
        self.assertFalse(adapter.audit.journal.failed)

    def test_renewal_policy_lifetime_and_expired_vm(self):
        adapter = self.adapter()
        lease = self.lease(adapter)
        result = adapter.lease_renew(lease, {})
        self.assertEqual(result["lease_ref"], lease)
        self.assertEqual(FakeSandbox.created[0].last_timeout, 120)
        record = adapter.state.get_lease(lease)
        record.created_at = time.time() - 301
        adapter.state.put_lease(record)
        with self.assertRaises(AdapterError) as caught:
            adapter.lease_renew(lease, {})
        self.assertEqual(caught.exception.code, "renew_limit_reached")
        adapter.profiles["controlled"] = replace(
            adapter.profiles["controlled"], keepalive_enabled=False
        )
        with self.assertRaises(AdapterError) as caught:
            adapter.lease_renew(lease, {})
        self.assertEqual(caught.exception.code, "renew_denied")

    def test_independent_log_cursors_do_not_restart_command(self):
        adapter = self.adapter()
        lease = self.lease(adapter)
        job = adapter.job_start(lease, {"command": "printf original"})
        adapter._handles.clear()
        calls = []

        def command(value, **_kwargs):
            calls.append(value)
            if "dd if=" in value:
                return FakeResult(stdout=base64.b64encode(b"chunk").decode())
            return FakeResult()

        with mock.patch.object(FakeSandbox.created[0].commands, "run", side_effect=command):
            result = adapter.job_resume(job["job_ref"], {"stdout_offset": 7, "stderr_offset": 3})
        self.assertEqual(result["stdout"]["next_offset"], 12)
        self.assertEqual(result["stderr"]["next_offset"], 8)
        self.assertFalse(result["restarted"])
        self.assertTrue(all("nohup" not in value for value in calls))
        self.assertEqual(len(FakeSandbox.created), 1)

    def test_full_history_tenant_scope_pagination_and_export(self):
        adapter = self.adapter()
        for tenant in ("one", "two"):
            runtime = AuthContext(tenant, tenant)
            lease = self.lease(adapter, runtime)
            adapter.exec(lease, {"command": "true"}, runtime)
        auditor = AuthContext("one", "auditor", roles=frozenset({"auditor"}))
        first = adapter.audit_query({"limit": 2}, auditor)
        self.assertIsNotNone(first["next_cursor"])
        second = adapter.audit_query({"limit": 2, "before": first["next_cursor"]}, auditor)
        self.assertTrue(
            {event["seq"] for event in first["events"]}.isdisjoint(
                {event["seq"] for event in second["events"]}
            )
        )
        for event in first["events"] + second["events"]:
            self.assertEqual(event["tenant_hash"], hashlib.sha256(b"one").hexdigest()[:16])
        with self.assertRaises(AdapterError):
            adapter.audit_query({}, AuthContext("one", "runtime"))
        archive = self.root / "archive.jsonl"
        export_archive(adapter.audit.journal, str(archive))
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        manifest = json.loads(Path(str(archive) + ".manifest.json").read_text())
        self.assertEqual(manifest["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertGreater(verify_chain(adapter.audit.journal)["hashed_events"], 0)
        with self.assertRaises(RuntimeError):
            export_archive(adapter.audit.journal, str(archive))

    def test_recovery_never_recreates_an_expired_vm(self):
        adapter = self.adapter()
        lease = self.lease(adapter)
        FakeSandbox.created[0].kill()
        result = adapter.recover()
        self.assertEqual(result["leases"][0]["state"], "expired")
        self.assertEqual(result["commands_restarted"], 0)
        self.assertEqual(len(FakeSandbox.created), 1)
        self.assertEqual(result["leases"][0]["lease_ref"], lease)

    def test_readonly_roles_cannot_gain_runtime_from_wildcard_actions(self):
        for role in ("auditor", "approver"):
            identity = AuthContext("one", role, roles=frozenset({role}))
            self.assertTrue(identity.permits_action("audit:read"))
            for action in ("exec:run", "lease:acquire", "mcp:call", "task:submit"):
                self.assertFalse(identity.permits_action(action))
        self.assertTrue(
            AuthContext("one", "approver", roles=frozenset({"approver"})).permits_action(
                "task:approve"
            )
        )

    def test_job_unknown_observation_is_retryable_but_never_relaunched(self):
        adapter = self.adapter()
        lease = self.lease(adapter)
        job = adapter.job_start(lease, {"command": "true"})
        with mock.patch.object(FakeSandbox.created[0].files, "exists", side_effect=TimeoutError):
            self.assertEqual(adapter.job_status(job["job_ref"])["state"], "unknown")
        self.assertEqual(adapter.job_status(job["job_ref"])["state"], "running")
        FakeSandbox.created[0].kill()
        self.assertEqual(adapter.job_status(job["job_ref"])["state"], "orphaned")
        self.assertEqual(len(FakeSandbox.created), 1)
        self.assertFalse(adapter.audit.journal.failed)

    def test_mcp_setup_failure_removes_new_vm(self):
        adapter = self.adapter()
        with mock.patch.object(FakeSandbox, "get_mcp_token", side_effect=RuntimeError):
            with self.assertRaises(AdapterError) as caught:
                self.lease(adapter)
        self.assertEqual(caught.exception.code, "mcp_setup_failed")
        self.assertTrue(FakeSandbox.created[0].killed)
        self.assertFalse(adapter.audit.journal.failed)

    def test_uncertain_gc_kill_keeps_intent_and_blocks_execution(self):
        adapter = self.adapter()
        lease = self.lease(adapter)
        record = adapter.state.get_lease(lease)
        record.last_used_at = time.time() - 7200
        adapter.state.put_lease(record)
        with mock.patch.object(FakeSandbox.created[0], "kill", side_effect=TimeoutError):
            with self.assertRaises(AdapterError) as caught:
                adapter.force_gc()
        self.assertEqual(caught.exception.code, "audit_unavailable")
        self.assertIsNotNone(adapter.state.get_lease(lease))
        self.assertTrue(adapter.audit.journal.failed)
        self.assertTrue(adapter.audit.journal.pending_operations())

    def test_volume_cleanup_uses_creation_policy_and_does_not_hide_failure(self):
        adapter = self.adapter()
        lease = self.lease(adapter)
        record = adapter.state.get_lease(lease)
        record.volume_owned = True
        record.volume_id = "owned-test-volume"
        record.volume_retain_on_kill = False
        adapter.state.put_lease(record)
        # Current profile retains volumes; the lease's captured policy deletes.
        with mock.patch.object(adapter, "_volume_destroyer", side_effect=TimeoutError) as destroy:
            with self.assertRaises(TimeoutError):
                adapter.release(lease, {"action": "kill"})
        destroy.assert_called_once_with("owned-test-volume")
        self.assertIsNotNone(adapter.state.get_lease(lease))
        self.assertTrue(adapter.audit.journal.failed)

    def test_mcp_transport_timeout_kills_worker_without_retry(self):
        client = GatewayClient.__new__(GatewayClient)
        client.descriptor = {"headers": {"Authorization": "synthetic-private-value"}}
        client.policy = McpPolicy("test", {}, {}, timeout_seconds=1)
        process = mock.Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 1), (b"", b"")]
        with mock.patch("adapter.mcp_policy.subprocess.Popen", return_value=process) as popen:
            with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
                client.request("tools/call", {"name": "echo", "arguments": {}})
        process.kill.assert_called_once()
        popen.assert_called_once()
        self.assertNotIn("synthetic-private-value", str(popen.call_args))


if __name__ == "__main__":
    unittest.main()
