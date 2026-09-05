from __future__ import annotations

import base64
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from adapter.config import AdapterConfig, AuthContext
from adapter.core import AdapterError, CubeAdapter
from adapter.mcp_server import AdapterHttpClient
from adapter.state import MemoryStateStore, RecordCipher
from adapter.test_support import FakeSandbox, FakeVolume, fake_template


class DurableMemoryState(MemoryStateStore):
    durable = True


class CoreFeatureTest(unittest.TestCase):
    def setUp(self):
        FakeSandbox.reset()
        FakeVolume.created.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.adapters = []

    def tearDown(self):
        for adapter in reversed(self.adapters):
            adapter.close()
        self.tmp.cleanup()

    def config(self, *, profiles_file=None, **values):
        return AdapterConfig(
            token="test-token-with-at-least-24-chars",
            session_hmac_key="test-session-hmac-key-with-32-characters",
            template="agent-code",
            audit_log=str(self.root / "audit.jsonl"),
            profiles_file=str(profiles_file) if profiles_file else None,
            **values,
        )

    def adapter(self, config=None, state=None, **values):
        snapshot_deleter = values.pop("snapshot_deleter", lambda _snapshot_id: None)
        adapter = CubeAdapter(
            config or self.config(),
            sandbox_factory=FakeSandbox.create,
            sandbox_connector=FakeSandbox.connect,
            volume_factory=FakeVolume.create,
            volume_connector=lambda volume_id: FakeVolume(volume_id),
            volume_destroyer=lambda _volume_id: None,
            template_checker=fake_template,
            snapshot_deleter=snapshot_deleter,
            state_store=state,
            start_gc=False,
            **values,
        )
        self.adapters.append(adapter)
        return adapter

    def acquire(self, adapter, session="session", auth=None, profile="offline-code"):
        return adapter.acquire(
            {"runtime": "openclaw", "session_key": session, "profile": profile}, auth
        )

    def write_profiles(self, content):
        path = self.root / "profiles.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_typed_files_binary_artifacts_and_jobs(self):
        adapter = self.adapter()
        lease = self.acquire(adapter)["lease_ref"]

        adapter.make_dir(lease, {"path": "/workspace/src"})
        adapter.write(lease, {"path": "/workspace/src/a.txt", "content": "hello"})
        listing = adapter.list_files(lease, {"path": "/workspace/src"})
        self.assertEqual(listing["entries"][0]["name"], "a.txt")
        adapter.move_file(
            lease,
            {"path": "/workspace/src/a.txt", "destination": "/workspace/src/b.txt"},
        )
        self.assertEqual(
            adapter.read(lease, {"path": "/workspace/src/b.txt"})["content"], "hello"
        )

        raw = b"\x00binary\xff"
        uploaded = adapter.artifact_upload(
            lease,
            {
                "path": "/workspace/blob.bin",
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        )
        downloaded = adapter.artifact_download(
            lease, {"path": "/workspace/blob.bin"}
        )
        self.assertEqual(base64.b64decode(downloaded["content_base64"]), raw)
        self.assertEqual(uploaded["sha256"], downloaded["sha256"])

        started = adapter.job_start(
            lease, {"command": "printf done", "cwd": "/workspace"}
        )
        job = adapter.state.get_job(started["job_ref"])
        sandbox = FakeSandbox.created[0]
        self.assertIn("&& { nohup setsid", sandbox.commands.last_command)
        self.assertIn("& echo $!; }", sandbox.commands.last_command)
        self.assertIn("timeout --signal=TERM", sandbox.commands.last_command)
        sandbox.files.write(job.stdout_path, "done")
        sandbox.files.write(job.stderr_path, "")
        sandbox.files.write(job.exit_path, "0")
        self.assertEqual(adapter.job_status(job.job_ref)["state"], "succeeded")
        self.assertIn("done", adapter.job_output(job.job_ref)["data"])

    def test_concurrent_jobs_cannot_exceed_profile_quota(self):
        profiles = self.write_profiles(
            """
profiles:
  one-job:
    max_jobs_per_lease: 1
"""
        )
        adapter = self.adapter(self.config(profiles_file=profiles))
        lease = self.acquire(adapter, profile="one-job")["lease_ref"]
        started = []
        denied = []

        def launch():
            try:
                started.append(adapter.job_start(lease, {"command": "sleep 30"}))
            except AdapterError as error:
                denied.append(error)

        threads = [threading.Thread(target=launch) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(started), 1)
        self.assertEqual(len(denied), 7)
        self.assertTrue(all(error.status == 429 for error in denied))

    def test_pty_lifecycle_and_base64_stream(self):
        adapter = self.adapter()
        lease = self.acquire(adapter)["lease_ref"]
        value = adapter.pty_create(
            lease, {"rows": 40, "cols": 120, "cwd": "/workspace"}
        )
        pty_ref = value["pty_ref"]
        adapter.pty_input(pty_ref, {"data": "echo hello\n"})
        adapter.pty_resize(pty_ref, {"rows": 30, "cols": 100})
        events = list(adapter.iter_pty_events(pty_ref, AuthContext("default", "test")))
        output = b"".join(
            base64.b64decode(item["data"]["content_base64"])
            for item in events
            if item["event"] == "output"
        )
        self.assertEqual(output, b"hello pty\n")
        self.assertEqual(events[-1]["data"]["state"], "succeeded")

    def test_profiles_persistent_volume_checkpoint_rollback_and_fork(self):
        profiles = self.write_profiles(
            """
profiles:
  durable-code:
    checkpoints_enabled: true
    allow_checkpoint_with_mounts: true
    max_active_leases_per_tenant: 3
    workspace:
      mode: per-session-volume
      driver: local
      retain_on_kill: false
"""
        )
        adapter = self.adapter(self.config(profiles_file=profiles))
        lease = self.acquire(adapter, profile="durable-code")["lease_ref"]
        self.assertTrue(FakeSandbox.created[0].create_args["volume_mounts"])
        checkpoint = adapter.checkpoint_create(lease, {"name": "before-edit"})
        adapter.checkpoint_rollback(lease, checkpoint["checkpoint_ref"], {})
        self.assertEqual(FakeSandbox.created[0].rolled_back_to, "snapshot-before-edit")
        fork = adapter.checkpoint_fork(
            lease, checkpoint["checkpoint_ref"], {"branch": "experiment"}
        )
        self.assertNotEqual(fork["lease_ref"], lease)
        self.assertEqual(len(FakeSandbox.created), 2)
        reused = adapter.checkpoint_fork(
            lease, checkpoint["checkpoint_ref"], {"branch": "experiment"}
        )
        self.assertEqual(reused["lease_ref"], fork["lease_ref"])
        self.assertTrue(reused["reused"])
        self.assertEqual(len(FakeSandbox.created), 2)

    def test_checkpoint_mount_guard_matches_upstream_limitation(self):
        profiles = self.write_profiles(
            """
profiles:
  guarded:
    checkpoints_enabled: true
    workspace:
      mode: per-session-volume
"""
        )
        adapter = self.adapter(self.config(profiles_file=profiles))
        lease = self.acquire(adapter, profile="guarded")["lease_ref"]
        with self.assertRaises(AdapterError) as denied:
            adapter.checkpoint_create(lease, {})
        self.assertEqual(denied.exception.code, "checkpoint_with_mount_denied")

    def test_kill_cleans_snapshots_after_sandbox_and_preserves_retry_state(self):
        profiles = self.write_profiles(
            """
profiles:
  checkpoint-code:
    checkpoints_enabled: true
"""
        )
        attempts = []

        def delete_snapshot(snapshot_id):
            attempts.append((snapshot_id, FakeSandbox.created[0].killed))
            if len(attempts) == 1:
                raise RuntimeError("transient upstream delete failure")

        adapter = self.adapter(
            self.config(profiles_file=profiles), snapshot_deleter=delete_snapshot
        )
        lease = self.acquire(adapter, profile="checkpoint-code")["lease_ref"]
        checkpoint = adapter.checkpoint_create(lease, {"name": "cleanup"})

        with self.assertRaises(AdapterError) as pending:
            adapter.release(lease, {"action": "kill"})
        self.assertEqual(pending.exception.code, "checkpoint_cleanup_failed")
        self.assertTrue(FakeSandbox.created[0].killed)
        self.assertEqual(adapter.state.get_lease(lease).state, "released")
        self.assertIsNotNone(adapter.state.get_checkpoint(checkpoint["checkpoint_ref"]))

        adapter.release(lease, {"action": "kill"})
        self.assertEqual(attempts, [("snapshot-cleanup", True), ("snapshot-cleanup", True)])
        self.assertIsNone(adapter.state.get_checkpoint(checkpoint["checkpoint_ref"]))
        self.assertIsNone(adapter.state.get_lease(lease))

    def test_profiles_reject_unknown_fields_and_string_booleans(self):
        unknown = self.write_profiles(
            """
profiles:
  typo:
    max_job_per_lease: 2
"""
        )
        with self.assertRaisesRegex(RuntimeError, "unsupported fields"):
            self.adapter(self.config(profiles_file=unknown))

        invalid_boolean = self.write_profiles(
            """
profiles:
  weak:
    allow_internet_access: "false"
"""
        )
        with self.assertRaisesRegex(RuntimeError, "must be a boolean"):
            self.adapter(self.config(profiles_file=invalid_boolean))

    def test_concurrent_acquire_is_idempotent_and_quota_is_tenant_scoped(self):
        profiles = self.write_profiles(
            """
profiles:
  one:
    max_active_leases_per_tenant: 1
"""
        )
        adapter = self.adapter(self.config(profiles_file=profiles))
        auth = AuthContext("tenant-a", "caller")
        results = []

        def acquire_same():
            results.append(self.acquire(adapter, "shared", auth, "one"))

        threads = [threading.Thread(target=acquire_same) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({item["lease_ref"] for item in results}), 1)
        self.assertEqual(len(FakeSandbox.created), 1)
        with self.assertRaises(AdapterError) as quota:
            self.acquire(adapter, "different", auth, "one")
        self.assertEqual(quota.exception.status, 429)
        other = self.acquire(
            adapter, "different", AuthContext("tenant-b", "caller"), "one"
        )
        self.assertTrue(other["lease_ref"].startswith("lease_"))

    def test_durable_state_reconnects_without_killing_sandbox(self):
        state = DurableMemoryState()
        first = self.adapter(state=state)
        lease = self.acquire(first)["lease_ref"]
        sandbox = FakeSandbox.created[0]
        first.close()
        self.adapters.remove(first)
        self.assertFalse(sandbox.killed)
        second = self.adapter(state=state)
        result = second.exec(lease, {"command": "true"})
        self.assertEqual(result["exit_code"], 0)
        self.assertIs(second._handles[lease], sandbox)

    def test_readiness_metrics_gc_and_encrypted_records(self):
        adapter = self.adapter()
        lease = self.acquire(adapter)["lease_ref"]
        status, ready = adapter.readiness(force=True)
        self.assertEqual(status, 200)
        self.assertEqual(ready["checks"]["template:agent-code"], "ready")
        metrics = adapter.metrics_payload().decode("utf-8")
        self.assertIn("cube_adapter_active_leases", metrics)

        record = adapter.state.get_lease(lease)
        record.last_used_at = time.time() - 7200
        adapter.state.put_lease(record)
        self.assertEqual(adapter.force_gc()["collected"], 1)
        self.assertTrue(FakeSandbox.created[0].killed)

        key = Fernet.generate_key().decode("ascii")
        cipher = RecordCipher(key)
        encrypted = cipher.dumps({"trafficAccessToken": "secret"})
        self.assertNotIn(b"secret", encrypted)
        self.assertEqual(cipher.loads(encrypted)["trafficAccessToken"], "secret")

    def test_mcp_http_client_enforces_transport_and_parses_errors(self):
        with self.assertRaises(RuntimeError):
            AdapterHttpClient("http://example.com", "x" * 24)
        with self.assertRaises(RuntimeError):
            AdapterHttpClient("http://127.0.0.1:8787", "")
        with self.assertRaises(RuntimeError):
            AdapterHttpClient(
                "http://127.0.0.1:8787",
                "",
                client_cert_file="client.crt",
                client_key_file="client.key",
            )


if __name__ == "__main__":
    unittest.main()
