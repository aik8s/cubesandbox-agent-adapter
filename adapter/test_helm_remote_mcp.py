from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml


class RemoteMcpChartTest(unittest.TestCase):
    chart = Path(__file__).resolve().parents[1] / "charts/cubesandbox-agent-adapter"

    def render(self, *values):
        args = ["helm", "template", "adapter", str(self.chart)]
        for value in values:
            args.extend(["--set", value])
        return subprocess.run(args, capture_output=True, text=True)  # noqa: S603 - fixed Helm test inputs

    def test_default_does_not_expose_mcp(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = list(yaml.safe_load_all(result.stdout))
        deployment = next(x for x in documents if x and x["kind"] == "Deployment")
        names = [x["name"] for x in deployment["spec"]["template"]["spec"]["containers"]]
        self.assertNotIn("remote-mcp", names)

    def test_requires_tls_host_and_candidate_image(self):
        missing = self.render("remoteMcp.enabled=true")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("requires tlsSecret", missing.stderr)
        old = self.render("remoteMcp.enabled=true", "remoteMcp.tlsSecret=tls",
                          "remoteMcp.allowedHosts[0]=mcp.example.test")
        self.assertNotEqual(old.returncode, 0)
        self.assertIn("candidate image", old.stderr)
        incompatible = self.render("remoteMcp.enabled=true", "remoteMcp.tlsSecret=tls",
            "remoteMcp.allowedHosts[0]=mcp.example.test", "image.tag=candidate", "tls.enabled=true",
            "tls.existingSecret=adapter-tls")
        self.assertNotEqual(incompatible.returncode, 0)
        self.assertIn("local plaintext Adapter listener", incompatible.stderr)

    def test_enabled_has_tls_and_no_shared_token(self):
        result = self.render("remoteMcp.enabled=true", "remoteMcp.tlsSecret=tls",
                             "remoteMcp.allowedHosts[0]=mcp.example.test", "image.tag=candidate")
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        deployment = next(x for x in docs if x and x["kind"] == "Deployment")
        containers = deployment["spec"]["template"]["spec"]["containers"]
        mcp = next(x for x in containers if x["name"] == "remote-mcp")
        self.assertIn("--tls-cert-file", mcp["args"])
        self.assertIn("--allowed-host", mcp["args"])
        self.assertNotIn("env", mcp)
        self.assertTrue(mcp["securityContext"]["readOnlyRootFilesystem"])
        service = next(x for x in docs if x and x["kind"] == "Service")
        self.assertIn("mcp", {p["name"] for p in service["spec"]["ports"]})


if __name__ == "__main__":
    unittest.main()
