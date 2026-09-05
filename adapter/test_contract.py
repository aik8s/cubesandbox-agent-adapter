from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

from adapter.core import VERSION
from adapter.http_api import ALLOWED_FIELDS


class OpenApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.document = yaml.safe_load((root / "docs/openapi.yaml").read_text("utf-8"))
        cls.schemas = cls.document["components"]["schemas"]

    def properties(self, name):
        schema = self.schemas[name]
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            schema = self.schemas[name]
        self.assertIs(schema.get("additionalProperties"), False, name)
        return frozenset(schema.get("properties", {}))

    def test_runtime_request_whitelists_match_openapi(self):
        mapping = {
            "acquire": "AcquireRequest",
            "exec": "ExecRequest",
            "read": "PathRequest",
            "write": "WriteRequest",
            "list": "PathRequest",
            "stat": "PathRequest",
            "mkdir": "PathRequest",
            "remove": "PathRequest",
            "move": "MoveRequest",
            "artifact_upload": "ArtifactUploadRequest",
            "artifact_download": "PathRequest",
            "release": "ReleaseRequest",
            "status": "StatusRequest",
            "job_start": "JobStartRequest",
            "job_output": "JobOutputRequest",
            "job_cancel": "EmptyRequest",
            "pty_create": "PtyCreateRequest",
            "pty_input": "PtyInputRequest",
            "pty_resize": "PtyResizeRequest",
            "pty_kill": "EmptyRequest",
            "checkpoint_create": "CheckpointCreateRequest",
            "checkpoint_action": "CheckpointActionRequest",
            "task_plan": "TaskPlanRequest",
            "task_approve": "TaskApproveRequest",
            "empty": "EmptyRequest",
        }
        self.assertEqual(set(mapping), set(ALLOWED_FIELDS))
        for runtime_name, schema_name in mapping.items():
            self.assertEqual(
                ALLOWED_FIELDS[runtime_name],
                self.properties(schema_name),
                runtime_name,
            )

    def test_operation_ids_are_unique_and_contract_is_complete(self):
        operation_ids = []
        for value in self.document["paths"].values():
            for method in ("get", "post", "put", "patch", "delete"):
                operation = value.get(method)
                if operation:
                    operation_ids.append(operation["operationId"])
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertGreaterEqual(len(operation_ids), 35)

    def test_release_versions_are_synchronized(self):
        root = Path(__file__).resolve().parents[1]
        chart = yaml.safe_load(
            (root / "charts/cubesandbox-agent-adapter/Chart.yaml").read_text("utf-8")
        )
        openclaw = json.loads(
            (root / "plugins/openclaw/openclaw.plugin.json").read_text("utf-8")
        )
        openclaw_package = json.loads(
            (root / "plugins/openclaw/package.json").read_text("utf-8")
        )
        dsh_package = json.loads(
            (root / "plugins/dsh/package.json").read_text("utf-8")
        )
        hermes = yaml.safe_load((root / "plugins/hermes/plugin.yaml").read_text("utf-8"))
        project_text = (root / "pyproject.toml").read_text("utf-8")
        project_version = re.search(
            r'^version = "([^"]+)"$', project_text, flags=re.MULTILINE
        )
        self.assertIsNotNone(project_version)
        versions = {
            (root / "VERSION").read_text("utf-8").strip(),
            VERSION,
            str(chart["version"]),
            str(chart["appVersion"]),
            str(self.document["info"]["version"]),
            str(openclaw["version"]),
            str(openclaw_package["version"]),
            str(dsh_package["version"]),
            str(hermes["version"]),
            str(project_version.group(1)),
        }
        self.assertEqual(versions, {"0.4.0"})


if __name__ == "__main__":
    unittest.main()
