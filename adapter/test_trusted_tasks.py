from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from adapter.config import AdapterConfig, AuthContext, TokenPrincipal
from adapter.core import AdapterError, CubeAdapter
from adapter.http_api import make_handler
from adapter.test_support import FakeSandbox, fake_template
from scripts.verify_receipt import verify


class TrustedTaskTest(unittest.TestCase):
    def setUp(self):
        FakeSandbox.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        profiles = self.root / "profiles.yaml"
        profiles.write_text(
            """
profiles:
  trusted-training:
    template: trusted-training
    max_command_seconds: 3600
    max_jobs_per_lease: 1
    allowed_runtimes: [mcp]
    allow_internet_access: false
""",
            encoding="utf-8",
        )
        tasks = self.root / "tasks.yaml"
        tasks.write_text(
            """
tasks:
  train-model:
    description: Train an approved model over a bounded workspace dataset.
    profile: trusted-training
    approval_required: true
    plan_ttl_seconds: 300
    parameters:
      type: object
      additionalProperties: false
      required: [input, epochs]
      properties:
        input:
          type: string
          pattern: ^/workspace/input/[A-Za-z0-9_.-]+\\.csv$
        epochs:
          type: integer
          minimum: 1
          maximum: 1000
    command:
      argv: [python3, /opt/tasks/train.py, --input, "${input}", --epochs, "${epochs}"]
      cwd: /workspace
    outputs:
      - name: metrics
        path: /workspace/results/metrics.json
        format: json
        expose: content
        schema:
          type: object
          additionalProperties: false
          required: [accuracy]
          properties:
            accuracy: {type: number, minimum: 0, maximum: 1}
      - name: model
        path: /workspace/results/model.json
        format: json
        expose: digest
""",
            encoding="utf-8",
        )
        self.key = "receipt-signing-key-with-at-least-32-characters"
        self.agent_token = "task-agent-token-with-at-least-24-characters"
        self.approver_token = "task-approver-token-with-at-least-24-characters"
        self.requester = AuthContext(
            "team-a",
            "local-agent",
            allowed_profiles=frozenset({"trusted-training"}),
            allowed_runtimes=frozenset({"mcp"}),
            allowed_actions=frozenset({"task:*"}),
            allowed_task_templates=frozenset({"train-model"}),
        )
        self.approver = AuthContext(
            "team-a",
            "production-approver",
            roles=frozenset({"approver"}),
            allowed_actions=frozenset({"task:approve"}),
            allowed_task_templates=frozenset({"train-model"}),
        )
        self.adapter = CubeAdapter(
            AdapterConfig(
                token="",
                token_principals=(
                    TokenPrincipal(self.agent_token, self.requester),
                    TokenPrincipal(self.approver_token, self.approver),
                ),
                session_hmac_key="test-session-hmac-key-with-32-characters",
                receipt_hmac_key=self.key,
                template="agent-code",
                audit_log=str(self.root / "audit.jsonl"),
                profiles_file=str(profiles),
                task_templates_file=str(tasks),
            ),
            sandbox_factory=FakeSandbox.create,
            sandbox_connector=FakeSandbox.connect,
            template_checker=fake_template,
            start_gc=False,
        )

    def tearDown(self):
        self.adapter.close()
        self.tmp.cleanup()

    def test_plan_approve_submit_finalize_and_verify_receipt(self):
        plan = self.adapter.task_plan(
            {
                "template": "train-model",
                "parameters": {"input": "/workspace/input/training.csv", "epochs": 20},
            },
            self.requester,
        )
        self.assertEqual(plan["state"], "pending_approval")
        self.assertNotIn("parameters", plan)
        with self.assertRaises(AdapterError) as role_denied:
            self.adapter.task_approve(plan["plan_ref"], {}, self.requester)
        self.assertEqual(role_denied.exception.code, "approver_required")

        review = self.adapter.task_plan_status(plan["plan_ref"], self.approver)
        self.assertEqual(review["parameters"]["epochs"], 20)
        self.assertEqual(review["command_sha256"], plan["command_sha256"])
        approved = self.adapter.task_approve(plan["plan_ref"], {}, self.approver)
        self.assertEqual(approved["state"], "approved")
        submitted = self.adapter.task_submit(plan["plan_ref"], {}, self.requester)
        self.assertEqual(submitted["state"], "running")
        sandbox = FakeSandbox.created[0]
        self.assertIn("/workspace/input/training.csv", sandbox.files.values[next(
            path for path in sandbox.files.values if path.endswith("/command.sh")
        )])

        record = self.adapter.state.get_task(submitted["task_ref"])
        job = self.adapter.state.get_job(record.job_ref)
        sandbox.files.write(job.exit_path, "0")
        sandbox.files.write("/workspace/results/metrics.json", '{"accuracy": 0.95}')
        sandbox.files.write("/workspace/results/model.json", '{"weights": [1, 2]}')
        result = self.adapter.task_result(record.task_ref, self.requester)

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["outputs"]["metrics"]["accuracy"], 0.95)
        self.assertNotIn("model", result["result"]["outputs"])
        self.assertEqual(result["result"]["cleanup"], "verified")
        self.assertTrue(sandbox.killed)
        self.assertIsNone(self.adapter.state.get_lease(record.lease_ref))

        receipt = result["receipt"]
        encoded = json.dumps(
            receipt["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected = base64.urlsafe_b64encode(
            hmac.new(self.key.encode("utf-8"), encoded, hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(receipt["signature"]["value"], expected)
        self.assertEqual(verify(receipt, self.key)["task_ref"], record.task_ref)
        serialized = json.dumps(receipt)
        self.assertNotIn("training.csv", serialized)
        self.assertNotIn("weights", serialized)

    def test_schema_and_action_scopes_fail_closed(self):
        with self.assertRaises(AdapterError) as invalid:
            self.adapter.task_plan(
                {
                    "template": "train-model",
                    "parameters": {"input": "/etc/passwd", "epochs": 20},
                },
                self.requester,
            )
        self.assertEqual(invalid.exception.code, "invalid_parameters")
        with self.assertRaises(AdapterError) as action:
            self.adapter.authorize(self.requester, "exec:run")
        self.assertEqual(action.exception.code, "action_denied")
        self.assertTrue(self.requester.permits_action("task:status"))
        self.assertFalse(self.requester.permits_action("job:start"))

    def test_setup_failure_receipt_verifies_sandbox_cleanup(self):
        plan = self.adapter.task_plan(
            {
                "template": "train-model",
                "parameters": {"input": "/workspace/input/training.csv", "epochs": 20},
            },
            self.requester,
        )
        self.adapter.task_approve(plan["plan_ref"], {}, self.approver)
        with mock.patch.object(
            self.adapter, "job_start", side_effect=RuntimeError("launch failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                self.adapter.task_submit(plan["plan_ref"], {}, self.requester)

        status = self.adapter.task_plan_status(plan["plan_ref"], self.requester)
        task = self.adapter.state.get_task(status["task_ref"])
        self.assertIsNotNone(task)
        assert task is not None
        result = self.adapter.task_result(task.task_ref, self.requester)
        self.assertEqual(result["state"], "setup_failed")
        self.assertEqual(result["result"]["cleanup"], "verified")
        self.assertEqual(result["receipt"]["payload"]["cleanup"], "verified")
        self.assertTrue(FakeSandbox.created[0].killed)

    def test_task_template_rejects_partial_placeholders(self):
        path = self.root / "invalid-tasks.yaml"
        path.write_text(
            """
tasks:
  unsafe:
    description: Unsafe concatenation example.
    profile: trusted-training
    parameters:
      type: object
      additionalProperties: false
      properties: {name: {type: string}}
    command:
      argv: [echo, "prefix-${name}"]
""",
            encoding="utf-8",
        )
        config = AdapterConfig(
            token="test-token-with-at-least-24-chars",
            session_hmac_key="test-session-hmac-key-with-32-characters",
            template="agent-code",
            audit_log=str(self.root / "invalid-audit.jsonl"),
            profiles_file=self.adapter.config.profiles_file,
            task_templates_file=str(path),
        )
        with self.assertRaisesRegex(RuntimeError, "whole, declared placeholders"):
            CubeAdapter(config, start_gc=False)

    def test_http_scope_blocks_raw_execution_for_task_only_token(self):
        lease = self.adapter.acquire(
            {
                "runtime": "mcp",
                "session_key": "scope-test",
                "profile": "trusted-training",
            },
            self.requester,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.adapter))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path, body):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + self.agent_token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            return urllib.request.urlopen(request)

        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                post(f"/v1/leases/{lease['lease_ref']}/exec", {"command": "id"})
            self.assertEqual(denied.exception.code, 403)
            value = json.load(
                post(
                    "/v1/tasks/plan",
                    {
                        "template": "train-model",
                        "parameters": {
                            "input": "/workspace/input/training.csv",
                            "epochs": 20,
                        },
                    },
                )
            )
            self.assertEqual(value["state"], "pending_approval")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
