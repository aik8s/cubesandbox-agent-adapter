from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

from starlette.testclient import TestClient

from adapter.config import AdapterConfig, AuthContext, TokenPrincipal
from adapter.core import CubeAdapter
from adapter.http_api import make_handler
from adapter.remote_mcp import make_app
from adapter.state import TaskRecord
from adapter.test_support import FakeSandbox


class RemoteMcpTest(unittest.TestCase):
    def setUp(self):
        FakeSandbox.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.a = "tenant-a-token-with-at-least-24-characters"
        self.b = "tenant-b-token-with-at-least-24-characters"
        self.limited = "limited-token-with-at-least-24-characters"
        principals = (
            TokenPrincipal(self.a, AuthContext("team-a", "a")),
            TokenPrincipal(self.b, AuthContext("team-b", "b")),
            TokenPrincipal(self.limited, AuthContext("team-limited", "c",
                allowed_actions=frozenset({"lease:discover"}), allowed_runtimes=frozenset({"mcp"}))),
        )
        config = AdapterConfig(token="", token_principals=principals,
            session_hmac_key="test-session-hmac-key-with-32-characters",
            template="agent-code", audit_log=str(Path(self.temp.name) / "audit.jsonl"))
        self.adapter = CubeAdapter(config, sandbox_factory=FakeSandbox.create,
            sandbox_connector=FakeSandbox.connect,
            template_checker=lambda *args, **kwargs: {"status": "ready"}, start_gc=False)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.adapter))
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        self.client = TestClient(make_app(f"http://127.0.0.1:{self.api.server_port}",
            allowed_hosts=["testserver"]), base_url="http://testserver")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.api.shutdown()
        self.api.server_close()
        self.adapter.close()
        self.temp.cleanup()

    def call(self, token, name, arguments):
        return self.client.post("/mcp", headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}})

    @staticmethod
    def value(response):
        result = response.json()["result"]
        if result.get("isError"):
            raise AssertionError(result["content"])
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_authentication_and_origin_are_enforced(self):
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        self.assertEqual(self.call("bad-token", "cube_profiles", {}).status_code, 401)
        response = self.client.post("/mcp", headers={"Authorization": "Bearer " + self.a,
            "Host": "attacker.example", "Origin": "https://attacker.example",
            "Accept": "application/json, text/event-stream"}, json={})
        self.assertIn(response.status_code, {403, 421})
        with self.assertRaises(RuntimeError):
            make_app("http://example.invalid")
        with self.assertRaises(ValueError):
            make_app("https://user:password@example.invalid")

    def test_capabilities_reflect_action_scope(self):
        response = self.call(self.limited, "cube_profiles", {})
        self.assertEqual(response.status_code, 200, response.text)
        result = self.value(response)
        profile = result["profiles"][0]
        self.assertEqual(profile["capabilities"]["execute"], "not_allowed")
        self.assertEqual(profile["capabilities"]["cross_node_restore"], "not_verified")
        self.assertEqual(profile["allowed_runtimes"], ["mcp"])
        self.assertNotIn(self.limited, response.text)

    def test_concurrent_callers_keep_separate_tenants(self):
        def acquire(pair):
            token, session = pair
            response = self.call(token, "cube_acquire", {"runtime": "mcp", "session_key": session})
            self.assertEqual(response.status_code, 200, response.text)
            return self.value(response)["lease_ref"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            leases = list(pool.map(acquire, [(self.a if i % 2 == 0 else self.b, f"session-{i}") for i in range(12)]))
        self.assertEqual(len(set(leases)), 12)
        self.assertEqual(self.adapter.state.get_lease(leases[0]).tenant_id, "team-a")
        self.assertEqual(self.adapter.state.get_lease(leases[1]).tenant_id, "team-b")
        denied = self.call(self.b, "cube_status", {"lease_ref": leases[0]})
        self.assertEqual(denied.status_code, 200)
        self.assertTrue(denied.json()["result"]["isError"])
        self.assertNotIn(self.a, json.dumps(denied.json()))

    def test_task_inventory_separates_execution_and_cleanup_and_hides_outputs(self):
        auth_a = self.adapter.authenticate("Bearer " + self.a)
        auth_b = self.adapter.authenticate("Bearer " + self.b)
        record = TaskRecord(task_ref="task_" + "a" * 20, plan_ref="plan_" + "b" * 20,
            tenant_id="team-a", requester_hash=self.adapter._identity_hash(auth_a),
            template_name="demo", template_digest="digest", profile="offline-code",
            state="cleanup_pending", cleanup_status="pending",
            result={"execution_state": "succeeded", "outputs": {"secret": "private-output"}})
        self.adapter.state.put_task(record)
        listed = self.adapter.task_inventory(auth_a)["tasks"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["execution_state"], "succeeded")
        self.assertEqual(listed[0]["sandbox_cleanup_status"], "pending")
        self.assertEqual(listed[0]["snapshot_storage_cleanup_status"], "not_verified")
        self.assertNotIn("private-output", json.dumps(listed))
        self.assertEqual(self.adapter.task_inventory(auth_b)["tasks"], [])


if __name__ == "__main__":
    unittest.main()
