from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from adapter.cube_adapter import AdapterConfig, CubeAdapter, make_handler
from adapter.test_support import FakeSandbox


class AdapterHttpTest(unittest.TestCase):
    def setUp(self):
        FakeSandbox.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        config = AdapterConfig(
            token="test-token-with-at-least-24-chars",
            session_hmac_key="test-session-hmac-key-with-32-characters",
            template="agent-code",
            audit_log=str(self.audit_path),
        )
        self.adapter = CubeAdapter(
            config,
            sandbox_factory=FakeSandbox.create,
            sandbox_connector=FakeSandbox.connect,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.adapter))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.adapter.close()
        self.tmp.cleanup()

    def post(self, path, body, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer test-token-with-at-least-24-chars"
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def test_auth_reuse_execution_files_release_and_redacted_audit(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.post("/v1/leases/acquire", {"runtime": "openclaw", "session_key": "secret-session"}, False)
        self.assertEqual(denied.exception.code, 401)

        first = self.post("/v1/leases/acquire", {"runtime": "openclaw", "session_key": "secret-session"})
        second = self.post("/v1/leases/acquire", {"runtime": "openclaw", "session_key": "secret-session"})
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["lease_ref"], second["lease_ref"])
        self.assertEqual(len(FakeSandbox.created), 1)

        lease = first["lease_ref"]
        result = self.post(f"/v1/leases/{lease}/exec", {"command": "private-command"})
        self.assertEqual(result["stdout"], "remote-ok\n")
        self.assertNotIn("lease_ref", result)
        self.post(f"/v1/leases/{lease}/write", {"path": "/workspace/a.txt", "content": "hello"})
        read = self.post(f"/v1/leases/{lease}/read", {"path": "/workspace/a.txt"})
        self.assertEqual(read["content"], "hello")
        self.post(f"/v1/leases/{lease}/release", {"action": "kill"})
        self.assertTrue(FakeSandbox.created[0].killed)

        audit = self.audit_path.read_text()
        self.assertNotIn("private-command", audit)
        self.assertNotIn("secret-session", audit)
        self.assertNotIn("test-token", audit)
        self.assertNotIn("full-private-id", audit)
        self.assertIn('"command_sha256"', audit)

    def test_policy_and_path_fail_closed(self):
        with self.assertRaises(urllib.error.HTTPError) as profile:
            self.post(
                "/v1/leases/acquire",
                {"runtime": "dsh", "session_key": "s", "profile": "arbitrary-egress"},
            )
        self.assertEqual(profile.exception.code, 403)

        lease = self.post("/v1/leases/acquire", {"runtime": "dsh", "session_key": "s"})["lease_ref"]
        with self.assertRaises(urllib.error.HTTPError) as path:
            self.post(f"/v1/leases/{lease}/read", {"path": "/etc/shadow"})
        self.assertEqual(path.exception.code, 403)

        with self.assertRaises(urllib.error.HTTPError) as traversal:
            self.post(f"/v1/leases/{lease}/read", {"path": "/workspace/../etc/shadow"})
        self.assertEqual(traversal.exception.code, 403)

        with self.assertRaises(urllib.error.HTTPError) as unknown:
            self.post(
                f"/v1/leases/{lease}/exec",
                {"command": "true", "unexpected": "fail-closed"},
            )
        self.assertEqual(unknown.exception.code, 400)

    def test_hermes_runtime_is_isolated_from_other_runtime_sessions(self):
        hermes = self.post(
            "/v1/leases/acquire",
            {"runtime": "hermes", "session_key": "shared-session"},
        )
        openclaw = self.post(
            "/v1/leases/acquire",
            {"runtime": "openclaw", "session_key": "shared-session"},
        )
        self.assertNotEqual(hermes["lease_ref"], openclaw["lease_ref"])
        self.assertEqual(len(FakeSandbox.created), 2)

        self.post(f"/v1/leases/{hermes['lease_ref']}/release", {"action": "kill"})
        self.post(f"/v1/leases/{openclaw['lease_ref']}/release", {"action": "kill"})

        rows = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        self.assertIn("hermes", {row["runtime"] for row in rows})


if __name__ == "__main__":
    unittest.main()
