from __future__ import annotations

import os
import threading
import unittest
import uuid

from cryptography.fernet import Fernet

from adapter.state import CheckpointRecord, JobRecord, LeaseRecord, RedisStateStore


@unittest.skipUnless(os.environ.get("CUBE_TEST_REDIS_URL"), "Redis integration not configured")
class RedisStateIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.prefix = "cube-adapter-test-" + uuid.uuid4().hex
        self.key = os.environ.get("CUBE_TEST_REDIS_KEY") or Fernet.generate_key().decode()
        self.store = RedisStateStore(
            os.environ["CUBE_TEST_REDIS_URL"],
            prefix=self.prefix,
            encryption_key=self.key,
        )

    def tearDown(self):
        keys = list(self.store._redis.scan_iter(match=self.prefix + ":*"))
        if keys:
            self.store._redis.delete(*keys)
        self.store.close()

    def test_encrypted_indexes_recovery_locking_and_cascade_delete(self):
        lease = LeaseRecord(
            lease_ref="lease_0123456789abcdefabcd",
            tenant_id="tenant-a",
            runtime="mcp",
            session_hash="session-hash",
            profile="offline-code",
            sandbox_id="full-sensitive-sandbox-id",
            sandbox_ref="full-sen",
            traffic_access_token="sensitive-traffic-token",
        )
        self.store.put_lease(lease)
        self.assertEqual(
            self.store.get_session("tenant-a", "mcp", "session-hash"), lease.lease_ref
        )
        recovered = self.store.get_lease(lease.lease_ref)
        self.assertEqual(recovered.traffic_access_token, "sensitive-traffic-token")
        raw = self.store._redis.get(self.store._key("lease", lease.lease_ref))
        self.assertNotIn(b"sensitive-traffic-token", raw)
        self.assertNotIn(b"full-sensitive-sandbox-id", raw)

        job = JobRecord(
            job_ref="job_0123456789abcdefabcd",
            lease_ref=lease.lease_ref,
            tenant_id=lease.tenant_id,
            command_sha256="digest",
            pid=42,
            stdout_path="/tmp/out",
            stderr_path="/tmp/err",
            exit_path="/tmp/exit",
        )
        checkpoint = CheckpointRecord(
            checkpoint_ref="checkpoint_0123456789abcdefabcd",
            lease_ref=lease.lease_ref,
            tenant_id=lease.tenant_id,
            snapshot_id="snapshot-id",
            name="test",
        )
        self.store.put_job(job)
        self.store.put_checkpoint(checkpoint)
        self.assertEqual(self.store.list_jobs(lease_ref=lease.lease_ref)[0].pid, 42)
        self.assertEqual(
            self.store.list_checkpoints(lease.lease_ref)[0].snapshot_id, "snapshot-id"
        )

        order = []

        def contender():
            with self.store.lock("same", 5):
                order.append("second")

        with self.store.lock("same", 5):
            thread = threading.Thread(target=contender)
            thread.start()
            order.append("first")
        thread.join(timeout=5)
        self.assertEqual(order, ["first", "second"])

        self.store.delete_lease(lease)
        self.assertIsNone(self.store.get_lease(lease.lease_ref))
        self.assertEqual(self.store.list_jobs(lease_ref=lease.lease_ref), [])
        self.assertEqual(self.store.list_checkpoints(lease.lease_ref), [])


if __name__ == "__main__":
    unittest.main()
