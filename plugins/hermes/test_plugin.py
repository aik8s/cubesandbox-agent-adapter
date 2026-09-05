from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from plugins.hermes import register


class FakeContext:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **definition):
        self.tools[definition["name"]] = definition


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit=-1):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class HermesPluginTest(unittest.TestCase):
    def test_registers_and_routes_exec_by_hermes_task(self):
        context = FakeContext()
        calls = []

        def fake_urlopen(request, timeout):
            self.assertEqual(timeout, 125)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            body = json.loads(request.data)
            calls.append((request.full_url, body))
            if request.full_url.endswith("/v1/leases/acquire"):
                return FakeResponse({"lease_ref": "lease_hermes_test", "sandbox_ref": "abcd1234"})
            return FakeResponse(
                {
                    "executor": "cubesandbox-microvm",
                    "sandbox_ref": "abcd1234",
                    "stdout": "hermes-cube-ok\n",
                    "exit_code": 0,
                }
            )

        with mock.patch.dict(os.environ, {"CUBE_ADAPTER_TOKEN": "test-token"}, clear=False):
            with mock.patch("plugins.hermes.urlopen", side_effect=fake_urlopen):
                register(context)
                result = json.loads(
                    context.tools["cube_exec"]["handler"](
                        {"command": "printf hermes-cube-ok"},
                        task_id="hermes-task-123",
                    )
                )

        self.assertEqual(
            set(context.tools),
            {
                "cube_exec",
                "cube_status",
                "cube_read",
                "cube_write",
                "cube_list",
                "cube_job_start",
                "cube_job_status",
                "cube_job_output",
                "cube_job_cancel",
                "cube_checkpoint",
                "cube_rollback",
                "cube_fork",
                "cube_release",
                "cube_task_plan",
                "cube_task_submit",
                "cube_task_status",
                "cube_task_result",
                "cube_task_cancel",
                "cube_task_receipt",
            },
        )
        self.assertEqual(result["executor"], "cubesandbox-microvm")
        self.assertEqual(calls[0][1]["runtime"], "hermes")
        self.assertEqual(calls[0][1]["session_key"], "hermes-task-123")
        self.assertTrue(calls[1][0].endswith("/v1/leases/lease_hermes_test/exec"))
        with mock.patch.dict(os.environ, {"CUBE_ADAPTER_TOKEN": "test-token"}, clear=False):
            with mock.patch("plugins.hermes.urlopen", side_effect=fake_urlopen):
                context.tools["cube_task_plan"]["handler"](
                    {"template": "train-logistic", "parameters": {"epochs": 10}}
                )
        self.assertTrue(calls[2][0].endswith("/v1/tasks/plan"))
        self.assertEqual(calls[2][1]["template"], "train-logistic")

    def test_missing_token_hides_tools_and_returns_redacted_error(self):
        context = FakeContext({"token_env": "MISSING_CUBE_TOKEN"})
        with mock.patch.dict(os.environ, {}, clear=True):
            register(context)
            self.assertFalse(context.tools["cube_exec"]["check_fn"]())
            result = json.loads(context.tools["cube_exec"]["handler"]({"command": "id"}))
        self.assertEqual(
            result,
            {"error": "MISSING_CUBE_TOKEN or token_file is not configured"},
        )


if __name__ == "__main__":
    unittest.main()
