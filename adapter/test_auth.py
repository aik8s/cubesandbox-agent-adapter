from __future__ import annotations

import os
import time
import unittest
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from adapter.auth import Authenticator, AuthFailure
from adapter.config import AdapterConfig, AuthContext, TokenPrincipal


class FakeSigningKey:
    def __init__(self, key):
        self.key = key


class FakeJwksClient:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, _token):
        return FakeSigningKey(self.key)


class AuthenticationTest(unittest.TestCase):
    def config(self, **values):
        defaults = dict(
            token="",
            session_hmac_key="h" * 32,
            template="agent-code",
            audit_log="/tmp/unused-audit.jsonl",
        )
        defaults.update(values)
        return AdapterConfig(**defaults)

    def test_per_tenant_bearer_scope_and_mtls_subject(self):
        principal = TokenPrincipal(
            token="tenant-token-with-at-least-24-characters",
            context=AuthContext(
                tenant_id="team-a",
                subject="runtime-a",
                allowed_profiles=frozenset({"offline-code"}),
                allowed_runtimes=frozenset({"hermes"}),
            ),
        )
        authenticator = Authenticator(self.config(token_principals=(principal,)))
        context = authenticator.authenticate("Bearer " + principal.token)
        self.assertEqual(context.tenant_id, "team-a")
        self.assertTrue(context.permits_runtime("hermes"))
        self.assertFalse(context.permits_runtime("openclaw"))
        with self.assertRaises(AuthFailure):
            authenticator.authenticate("Bearer invalid")

        mtls = Authenticator(self.config(trust_client_cert_subject=True))
        peer = mtls.authenticate(None, peer_subject="commonName=runtime-a")
        self.assertTrue(peer.tenant_id.startswith("subject-"))
        self.assertEqual(peer.subject, "commonName=runtime-a")

    def test_oidc_signature_issuer_audience_and_claim_scopes(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        config = self.config(
            oidc_jwks_url="https://issuer.example/.well-known/jwks.json",
            oidc_issuer="https://issuer.example",
            oidc_audience="cube-adapter",
        )
        authenticator = Authenticator(config)
        authenticator._jwks_client = FakeJwksClient(private_key.public_key())
        token = jwt.encode(
            {
                "sub": "runtime-b",
                "tenant_id": "team-b",
                "roles": ["runtime"],
                "cube_profiles": ["persistent-code"],
                "cube_runtimes": "mcp hermes",
                "iss": "https://issuer.example",
                "aud": "cube-adapter",
                "exp": int(time.time()) + 300,
            },
            private_key,
            algorithm="RS256",
        )
        context = authenticator.authenticate("Bearer " + token)
        self.assertEqual(context.tenant_id, "team-b")
        self.assertEqual(context.allowed_profiles, frozenset({"persistent-code"}))
        self.assertEqual(context.allowed_runtimes, frozenset({"mcp", "hermes"}))

    def test_environment_allows_verified_mtls_only_and_rejects_weak_oidc(self):
        mtls_env = {
            "CUBE_ADAPTER_HMAC_KEY": "h" * 32,
            "CUBE_TEMPLATE_ID": "agent-code",
            "CUBE_ADAPTER_TLS_CERT_FILE": "/run/tls/tls.crt",
            "CUBE_ADAPTER_TLS_KEY_FILE": "/run/tls/tls.key",
            "CUBE_ADAPTER_TLS_CLIENT_CA_FILE": "/run/tls/client-ca.crt",
            "CUBE_ADAPTER_TRUST_CLIENT_CERT_SUBJECT": "1",
        }
        with mock.patch.dict(os.environ, mtls_env, clear=True):
            config = AdapterConfig.from_env()
        self.assertFalse(config.token)
        self.assertTrue(config.trust_client_cert_subject)

        weak_oidc_env = {
            "CUBE_ADAPTER_HMAC_KEY": "h" * 32,
            "CUBE_TEMPLATE_ID": "agent-code",
            "CUBE_ADAPTER_OIDC_JWKS_URL": "https://issuer.example/jwks.json",
        }
        with mock.patch.dict(os.environ, weak_oidc_env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "requires issuer and audience"):
                AdapterConfig.from_env()


if __name__ == "__main__":
    unittest.main()
