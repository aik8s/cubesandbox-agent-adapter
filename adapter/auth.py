"""Bearer, OIDC, and mTLS caller authentication."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Optional

import jwt

from .config import TENANT_ID_RE, AdapterConfig, AuthContext


@dataclass
class AuthFailure(Exception):
    status: int
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


class Authenticator:
    def __init__(self, config: AdapterConfig) -> None:
        self._config = config
        self._jwks_client = (
            jwt.PyJWKClient(config.oidc_jwks_url, cache_jwk_set=True, lifespan=300)
            if config.oidc_jwks_url
            else None
        )

    def authenticate(
        self,
        authorization: Optional[str],
        *,
        peer_subject: Optional[str] = None,
    ) -> AuthContext:
        if peer_subject and self._config.trust_client_cert_subject:
            tenant = _safe_tenant(peer_subject)
            return AuthContext(
                tenant_id=tenant,
                subject=peer_subject,
                roles=frozenset({"runtime"}),
            )

        if not authorization or not authorization.startswith("Bearer "):
            raise AuthFailure(401, "unauthorized", "valid bearer token required")
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise AuthFailure(401, "unauthorized", "valid bearer token required")

        if self._config.token and hmac.compare_digest(token, self._config.token):
            return AuthContext(
                tenant_id="default",
                subject="shared-bearer",
                roles=frozenset({"runtime", "admin"}),
            )
        for principal in self._config.token_principals:
            if hmac.compare_digest(token, principal.token):
                return principal.context

        if self._jwks_client is not None:
            return self._authenticate_oidc(token)
        raise AuthFailure(401, "unauthorized", "valid bearer token required")

    def _authenticate_oidc(self, token: str) -> AuthContext:
        assert self._jwks_client is not None
        try:
            key = self._jwks_client.get_signing_key_from_jwt(token)
            kwargs: dict[str, Any] = {
                "algorithms": ["RS256", "RS384", "RS512", "ES256", "ES384"],
                "options": {"require": ["exp", "sub"]},
            }
            if self._config.oidc_audience:
                kwargs["audience"] = self._config.oidc_audience
            else:
                kwargs["options"]["verify_aud"] = False
            if self._config.oidc_issuer:
                kwargs["issuer"] = self._config.oidc_issuer
            claims = jwt.decode(token, key.key, **kwargs)
        except Exception as error:
            raise AuthFailure(401, "invalid_token", "OIDC token validation failed") from error

        tenant_value = claims.get(self._config.oidc_tenant_claim) or claims.get("sub")
        tenant = _safe_tenant(str(tenant_value))
        roles_value = claims.get(self._config.oidc_roles_claim, ["runtime"])
        if isinstance(roles_value, str):
            roles = frozenset(item for item in roles_value.replace(",", " ").split() if item)
        elif isinstance(roles_value, list):
            roles = frozenset(str(item) for item in roles_value if str(item))
        else:
            roles = frozenset({"runtime"})

        profiles = _claim_set(claims.get("cube_profiles"))
        runtimes = _claim_set(claims.get("cube_runtimes"))
        return AuthContext(
            tenant_id=tenant,
            subject=str(claims["sub"]),
            roles=roles or frozenset({"runtime"}),
            allowed_profiles=profiles,
            allowed_runtimes=runtimes,
        )


def _claim_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(item for item in value.replace(",", " ").split() if item)
    if isinstance(value, list):
        return frozenset(str(item) for item in value if str(item))
    return frozenset()


def _safe_tenant(value: str) -> str:
    if TENANT_ID_RE.fullmatch(value):
        return value
    import hashlib

    return "subject-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
