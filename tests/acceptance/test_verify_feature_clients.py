#!/usr/bin/env python3
"""The evidence verifier must reject unsuccessful or incomplete real runs."""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "verify_feature_clients", Path(__file__).with_name("verify_feature_clients.py")
)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def fixture():
    def call(name, value):
        return {"name": name, "result": value}

    return [
        call(
            "cube_profiles",
            {
                "profiles": [
                    {"name": "staging-code", "backend_id": "staging"},
                    {"name": "production-code", "backend_id": "production"},
                ]
            },
        ),
        call("cube_exec", {"exit_code": 0, "stdout": "STAGING_ROUTE_OK", "sandbox_ref": "demo-a"}),
        call(
            "cube_exec", {"exit_code": 0, "stdout": "PRODUCTION_ROUTE_OK", "sandbox_ref": "demo-b"}
        ),
        call(
            "cube_renew",
            {
                "profile": "staging-code",
                "backend_id": "staging",
                "state": "running",
                "reused": True,
                "sandbox_ref": "demo-a",
            },
        ),
        call(
            "cube_job_start", {"state": "running", "job_ref": "demo-job", "sandbox_ref": "demo-a"}
        ),
        call(
            "cube_job_status",
            {
                "state": "succeeded",
                "job_ref": "demo-job",
                "sandbox_ref": "demo-a",
                "backend_id": "staging",
            },
        ),
        call(
            "cube_job_resume",
            {
                "state": "succeeded",
                "restarted": False,
                "job_ref": "demo-job",
                "sandbox_ref": "demo-a",
                "backend_id": "staging",
                **{
                    name: {
                        "data_base64": base64.b64encode(value).decode(),
                        "offset": 0,
                        "next_offset": len(value),
                        "eof": True,
                    }
                    for name, value in (
                        ("stdout", b"RESUME_STDOUT_OK"),
                        ("stderr", b"RESUME_STDERR_OK"),
                    )
                },
            },
        ),
        call("cube_release", {"action": "kill", "sandbox_ref": "demo-a"}),
        call("cube_release", {"action": "kill", "sandbox_ref": "demo-b"}),
    ]


class ClientFeatureVerifierTest(unittest.TestCase):
    def test_valid_flow_has_no_private_references_in_report(self):
        report = subject.verify(fixture())
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["tool_calls"], 9)
        self.assertNotIn("demo-a", json.dumps(report))

    def test_failed_feature_results_are_rejected(self):
        mutations = [
            lambda rows: rows[2]["result"].update(sandbox_ref="demo-a"),
            lambda rows: rows[1]["result"].update(exit_code=1),
            lambda rows: rows[3]["result"].update(backend_id="production"),
            lambda rows: rows[5]["result"].update(state="running"),
            lambda rows: rows[6]["result"].update(restarted=True),
            lambda rows: rows[6]["result"]["stdout"].update(next_offset=15),
            lambda rows: rows[6]["result"]["stderr"].update(data_base64="eA=="),
            lambda rows: rows[7]["result"].update(action="pause"),
            lambda rows: rows[8]["result"].update(sandbox_ref="demo-a"),
            lambda rows: rows.append(copy.deepcopy(rows[0])),
            lambda rows: rows[0].update(name="host_exec"),
            lambda rows: rows[1]["result"].update(error={"code": "denied"}),
            lambda rows: rows[3]["result"].update(sandbox_ref="demo-b"),
            lambda rows: rows[6]["result"].update(job_ref="different-job"),
            lambda rows: rows[6]["result"].update(backend_id="production"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                rows = fixture()
                mutate(rows)
                with self.assertRaises(AssertionError):
                    subject.verify(rows)

    def test_codex_zero_exit_with_denied_mcp_call_is_not_a_pass(self):
        event = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "cubesandbox",
                "tool": "cube_profiles",
                "status": "failed",
                "error": {"message": "MCP tool call requires approval"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(json.dumps(event) + "\n")
            with self.assertRaises(AssertionError):
                subject.codex(path)

    def test_codex_host_execution_cannot_be_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}) + "\n"
            )
            with self.assertRaises(AssertionError):
                subject.codex(path)


if __name__ == "__main__":
    unittest.main()
