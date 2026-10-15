from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from adapter.backend_config import BackendRegistry
from adapter.config import AdapterConfig, AuthContext
from adapter.core import AdapterError, CubeAdapter
from adapter.state import MemoryStateStore
from adapter.template_config import load_template_presets
from adapter.test_support import FakeSandbox


class BackendTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.adapters = []
        FakeSandbox.reset()

    def tearDown(self):
        for adapter in reversed(self.adapters):
            adapter.close()
        self.tmp.cleanup()

    def document(self, name, value):
        path = self.root / name
        path.write_text(yaml.safe_dump(value))
        return str(path)

    def config(self, **kwargs):
        backends = self.document(
            "backends.yaml",
            {
                "backends": {
                    name: {
                        "api_url": "https://" + name + ".example.invalid",
                        "proxy_host": name + ".example.invalid",
                    }
                    for name in ("one", "two")
                }
            },
        )
        profiles = self.document(
            "profiles.yaml",
            {
                "profiles": {
                    name: {"backend_id": backend, "template": "code"}
                    for name, backend in (("team-one", "one"), ("team-two", "two"))
                }
            },
        )
        return AdapterConfig(
            token="test-token-with-at-least-24-chars",
            session_hmac_key="test-session-key-with-at-least-32-chars",
            template="code",
            audit_log=str(self.root / "audit.jsonl"),
            backends_file=backends,
            profiles_file=profiles,
            **kwargs,
        )

    def adapter(self, config, state=None):
        adapter = CubeAdapter(
            config,
            sandbox_factory=FakeSandbox.create,
            sandbox_connector=FakeSandbox.connect,
            template_checker=lambda *args, **kwargs: {"status": "ready"},
            state_store=state,
            start_gc=False,
        )
        self.adapters.append(adapter)
        return adapter

    @staticmethod
    def acquire(adapter, profile):
        return adapter.acquire(
            {"runtime": "mcp", "session_key": "same-session", "profile": profile}
        )

    def test_backend_session_binding_and_restart_reconnect(self):
        state = MemoryStateStore()
        config = self.config()
        adapter = self.adapter(config, state)
        one = self.acquire(adapter, "team-one")
        two = self.acquire(adapter, "team-two")
        self.assertNotEqual(one["lease_ref"], two["lease_ref"])
        self.assertEqual(self.acquire(adapter, "team-one")["lease_ref"], one["lease_ref"])
        self.assertEqual(
            FakeSandbox.created[0].create_args["config"].api_url, "https://one.example.invalid"
        )
        self.assertEqual(
            FakeSandbox.created[1].create_args["config"].api_url, "https://two.example.invalid"
        )
        adapter._handles.clear()
        self.assertEqual(adapter.exec(one["lease_ref"], {"command": "true"})["exit_code"], 0)
        self.assertEqual(state.get_lease(two["lease_ref"]).backend_id, "two")
        self.assertNotIn("example.invalid", json.dumps(one))

    def test_rebinding_live_backend_is_rejected_without_external_call(self):
        adapter = self.adapter(self.config())
        lease = self.acquire(adapter, "team-one")
        backend = adapter.backends.get("one")
        adapter.backends.entries["one"] = replace(backend, api_url="https://other.example.invalid")
        with self.assertRaises(AdapterError) as caught:
            adapter.exec(lease["lease_ref"], {"command": "true"})
        self.assertEqual(caught.exception.code, "backend_binding_changed")
        self.assertFalse(adapter.audit.journal.failed)

    def test_profile_acl_prevents_cross_backend_access(self):
        adapter = self.adapter(self.config())
        auth = AuthContext(
            tenant_id="a", subject="a", roles=frozenset({"runtime"}), allowed_profiles=("team-one",)
        )
        with self.assertRaises(AdapterError):
            adapter.acquire({"runtime": "mcp", "session_key": "s", "profile": "team-two"}, auth)
        self.assertEqual(len(FakeSandbox.created), 0)

    def test_registry_rejects_endpoint_credentials_and_unknown_fields(self):
        for value in (
            {"api_url": "https://user:password@example.invalid", "proxy_host": "x"},
            {"api_url": "https://example.invalid", "proxy_host": "x", "command": "arbitrary"},
        ):
            with self.assertRaises(RuntimeError):
                BackendRegistry(self.document("bad.yaml", {"backends": {"one": value}}))

    def test_versioned_models_and_secret_files_are_injected_not_returned(self):
        secret = self.root / "model-key"
        secret.write_text("internal-model-key-do-not-publish")
        model_config = {
            "models": {
                "providers": {
                    "internal": {
                        "apiKey": "${FILE:" + str(secret) + "}",
                        "models": [{"id": "qwen-test"}, {"id": "other-test"}],
                    }
                }
            },
            "agents": {
                "defaults": {
                    "model": {"primary": "internal/qwen-test", "fallbacks": ["internal/other-test"]}
                }
            },
        }
        catalog = self.document(
            "templates.yaml",
            {
                "templates": {
                    "openclaw": {
                        "version": "1",
                        "backend_id": "one",
                        "template": "openclaw-internal",
                        "files": {"/root/.openclaw/openclaw.json": model_config},
                        "preflight_argv": ["true"],
                    }
                }
            },
        )
        config = self.config(template_presets_file=catalog)
        self.document(
            "profiles.yaml",
            {"profiles": {"team-one": {"backend_id": "one", "template_preset": "openclaw"}}},
        )
        adapter = self.adapter(config)
        result = self.acquire(adapter, "team-one")
        self.assertEqual(result["template_version"], "1")
        self.assertIn(
            secret.read_text(), FakeSandbox.created[0].files.values["/root/.openclaw/openclaw.json"]
        )
        self.assertNotIn(
            secret.read_text(), json.dumps(result) + json.dumps(adapter.audit.recent())
        )

    def test_unknown_default_model_and_provider_whitespace_fail_preflight(self):
        for provider, primary in (("internal ", "internal/qwen"), ("internal", "internal/missing")):
            content = {
                "models": {"providers": {provider: {"models": [{"id": "qwen"}]}}},
                "agents": {"defaults": {"model": {"primary": primary}}},
            }
            with self.assertRaises(RuntimeError):
                load_template_presets(
                    self.document(
                        "bad-template.yaml",
                        {
                            "templates": {
                                "bad": {
                                    "version": "1",
                                    "template": "x",
                                    "files": {"/root/.openclaw/openclaw.json": content},
                                }
                            }
                        },
                    )
                )
