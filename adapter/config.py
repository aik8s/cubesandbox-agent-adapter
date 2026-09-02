"""Configuration, policy profiles, and authenticated caller identities."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlparse

import yaml

PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
ENV_REF_RE = re.compile(r"^\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}$")
WORKSPACE_FIELDS = frozenset(
    {
        "mode",
        "mount_path",
        "volume_id",
        "volume_name",
        "driver",
        "read_only",
        "retain_on_kill",
    }
)
PROFILE_FIELDS = frozenset(
    {
        "template",
        "sandbox_timeout_seconds",
        "max_command_seconds",
        "lease_idle_ttl_seconds",
        "max_active_leases_per_tenant",
        "max_jobs_per_lease",
        "allow_internet_access",
        "network",
        "lifecycle",
        "distribution_scope",
        "allowed_runtimes",
        "workspace",
        "checkpoints_enabled",
        "allow_checkpoint_with_mounts",
    }
)


def _validate_url(
    name: str,
    value: Optional[str],
    *,
    schemes: frozenset[str],
    https_or_loopback: bool = False,
) -> None:
    if not value:
        return
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.hostname:
        allowed = ", ".join(f"{scheme}://" for scheme in sorted(schemes))
        raise RuntimeError(f"{name} must be an absolute URL using {allowed}")
    if https_or_loopback and parsed.scheme != "https":
        if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError(f"{name} must use https:// except on loopback")


def _positive_int(name: str, value: Any, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _optional_positive_int(name: str, value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return _positive_int(name, value)


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{name} must be a boolean")
    return value


def _string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise RuntimeError(f"{field_name} must be a list of strings")
    if not all(isinstance(item, str) and item for item in values):
        raise RuntimeError(f"{field_name} must contain non-empty strings")
    return tuple(values)


def _resolve_env_refs(value: Any) -> Any:
    """Resolve explicit ``${ENV:NAME}`` leaves without expanding arbitrary text."""

    if isinstance(value, str):
        match = ENV_REF_RE.fullmatch(value)
        if not match:
            return value
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise RuntimeError(f"profile references missing environment variable {name}")
        return resolved
    if isinstance(value, list):
        return [_resolve_env_refs(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _resolve_env_refs(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class WorkspaceConfig:
    """Operator-owned persistent workspace attachment."""

    mode: str = "ephemeral"
    mount_path: str = "/workspace"
    volume_id: Optional[str] = None
    volume_name: Optional[str] = None
    driver: Optional[str] = None
    read_only: bool = False
    retain_on_kill: bool = True

    @classmethod
    def from_mapping(cls, value: Any) -> "WorkspaceConfig":
        if value in (None, {}):
            return cls()
        if not isinstance(value, Mapping):
            raise RuntimeError("profile.workspace must be an object")
        unknown = set(value) - WORKSPACE_FIELDS
        if unknown:
            raise RuntimeError(
                "profile.workspace contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        mode = str(value.get("mode", "ephemeral"))
        if mode not in {"ephemeral", "existing-volume", "per-session-volume"}:
            raise RuntimeError(
                "profile.workspace.mode must be ephemeral, existing-volume, or per-session-volume"
            )
        mount_path = str(value.get("mount_path", "/workspace"))
        if not mount_path.startswith("/") or ".." in Path(mount_path).parts:
            raise RuntimeError("profile.workspace.mount_path must be an absolute safe path")
        volume_id = value.get("volume_id")
        if mode == "existing-volume" and not volume_id:
            raise RuntimeError("existing-volume workspace requires volume_id")
        return cls(
            mode=mode,
            mount_path=mount_path,
            volume_id=str(volume_id) if volume_id else None,
            volume_name=str(value["volume_name"]) if value.get("volume_name") else None,
            driver=str(value["driver"]) if value.get("driver") else None,
            read_only=_boolean("profile.workspace.read_only", value.get("read_only", False)),
            retain_on_kill=_boolean(
                "profile.workspace.retain_on_kill", value.get("retain_on_kill", True)
            ),
        )


@dataclass(frozen=True)
class ProfileConfig:
    """A platform-owned sandbox policy selectable only by name."""

    name: str
    template: str
    sandbox_timeout_seconds: int = 300
    max_command_seconds: int = 120
    lease_idle_ttl_seconds: int = 3600
    max_active_leases_per_tenant: int = 8
    max_jobs_per_lease: int = 8
    allow_internet_access: bool = False
    network: Dict[str, Any] = field(
        default_factory=lambda: {"allow_public_traffic": False}
    )
    lifecycle: Dict[str, Any] = field(
        default_factory=lambda: {"on_timeout": "pause", "auto_resume": True}
    )
    distribution_scope: tuple[str, ...] = ()
    allowed_runtimes: tuple[str, ...] = ("openclaw", "dsh", "hermes", "mcp")
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    checkpoints_enabled: bool = False
    allow_checkpoint_with_mounts: bool = False

    @classmethod
    def from_mapping(
        cls,
        name: str,
        value: Mapping[str, Any],
        *,
        default_template: str,
        defaults: Optional[Mapping[str, Any]] = None,
    ) -> "ProfileConfig":
        if not PROFILE_NAME_RE.fullmatch(name):
            raise RuntimeError(f"invalid profile name: {name!r}")
        merged: Dict[str, Any] = dict(defaults or {})
        merged.update(dict(value))
        unknown = set(merged) - PROFILE_FIELDS
        if unknown:
            raise RuntimeError(
                f"profile {name!r} contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        template = str(merged.get("template") or default_template)
        if not template:
            raise RuntimeError(f"profile {name!r} requires a template")

        lifecycle = merged.get("lifecycle") or {
            "on_timeout": "pause",
            "auto_resume": True,
        }
        if not isinstance(lifecycle, Mapping):
            raise RuntimeError(f"profile {name!r} lifecycle must be an object")
        on_timeout = lifecycle.get("on_timeout", "pause")
        if on_timeout not in {"pause", "kill"}:
            raise RuntimeError(f"profile {name!r} lifecycle.on_timeout is invalid")
        if "auto_resume" in lifecycle:
            _boolean(
                f"profile {name}.lifecycle.auto_resume", lifecycle["auto_resume"]
            )

        network = merged.get("network") or {"allow_public_traffic": False}
        if not isinstance(network, Mapping):
            raise RuntimeError(f"profile {name!r} network must be an object")

        allowed_runtimes = _string_tuple(
            merged.get("allowed_runtimes", ("openclaw", "dsh", "hermes", "mcp")),
            field_name=f"profile {name}.allowed_runtimes",
        )
        unsupported = set(allowed_runtimes) - {"openclaw", "dsh", "hermes", "mcp"}
        if unsupported:
            raise RuntimeError(f"profile {name!r} has unsupported runtimes: {sorted(unsupported)}")

        return cls(
            name=name,
            template=template,
            sandbox_timeout_seconds=_positive_int(
                f"profile {name}.sandbox_timeout_seconds",
                merged.get("sandbox_timeout_seconds", 300),
            ),
            max_command_seconds=_positive_int(
                f"profile {name}.max_command_seconds",
                merged.get("max_command_seconds", 120),
            ),
            lease_idle_ttl_seconds=_positive_int(
                f"profile {name}.lease_idle_ttl_seconds",
                merged.get("lease_idle_ttl_seconds", 3600),
            ),
            max_active_leases_per_tenant=_positive_int(
                f"profile {name}.max_active_leases_per_tenant",
                merged.get("max_active_leases_per_tenant", 8),
            ),
            max_jobs_per_lease=_positive_int(
                f"profile {name}.max_jobs_per_lease",
                merged.get("max_jobs_per_lease", 8),
            ),
            allow_internet_access=_boolean(
                f"profile {name}.allow_internet_access",
                merged.get("allow_internet_access", False),
            ),
            network=_resolve_env_refs(dict(network)),
            lifecycle=dict(lifecycle),
            distribution_scope=_string_tuple(
                merged.get("distribution_scope"),
                field_name=f"profile {name}.distribution_scope",
            ),
            allowed_runtimes=allowed_runtimes,
            workspace=WorkspaceConfig.from_mapping(merged.get("workspace")),
            checkpoints_enabled=_boolean(
                f"profile {name}.checkpoints_enabled",
                merged.get("checkpoints_enabled", False),
            ),
            allow_checkpoint_with_mounts=_boolean(
                f"profile {name}.allow_checkpoint_with_mounts",
                merged.get("allow_checkpoint_with_mounts", False),
            ),
        )


def load_profiles(
    path: Optional[str],
    *,
    default_template: str,
    sandbox_timeout_seconds: int,
    max_command_seconds: int,
) -> Dict[str, ProfileConfig]:
    defaults: Dict[str, Any] = {
        "sandbox_timeout_seconds": sandbox_timeout_seconds,
        "max_command_seconds": max_command_seconds,
    }
    if not path:
        return {
            "offline-code": ProfileConfig.from_mapping(
                "offline-code", {}, default_template=default_template, defaults=defaults
            )
        }

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError as error:
        raise RuntimeError(f"cannot read profile file {path!r}") from error
    except yaml.YAMLError as error:
        raise RuntimeError(f"profile file {path!r} is not valid YAML") from error
    if not isinstance(raw, Mapping):
        raise RuntimeError("profile file must contain an object")
    if "profiles" in raw:
        unknown_top_level = set(raw) - {"defaults", "profiles"}
        if unknown_top_level:
            raise RuntimeError(
                "profile file contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown_top_level))
            )
    profile_values = raw.get("profiles", raw)
    file_defaults = raw.get("defaults", {}) if "profiles" in raw else {}
    if not isinstance(profile_values, Mapping) or not profile_values:
        raise RuntimeError("profile file must define at least one profile")
    if not isinstance(file_defaults, Mapping):
        raise RuntimeError("profile defaults must be an object")
    defaults.update(dict(file_defaults))
    invalid = [str(name) for name, value in profile_values.items() if value is not None and not isinstance(value, Mapping)]
    if invalid:
        raise RuntimeError(f"profile definitions must be objects: {sorted(invalid)}")
    return {
        str(name): ProfileConfig.from_mapping(
            str(name), value or {}, default_template=default_template, defaults=defaults
        )
        for name, value in profile_values.items()
    }


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    subject: str
    roles: frozenset[str] = frozenset({"runtime"})
    allowed_profiles: frozenset[str] = frozenset()
    allowed_runtimes: frozenset[str] = frozenset()

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def permits_profile(self, name: str) -> bool:
        return not self.allowed_profiles or name in self.allowed_profiles

    def permits_runtime(self, name: str) -> bool:
        return not self.allowed_runtimes or name in self.allowed_runtimes


@dataclass(frozen=True)
class TokenPrincipal:
    token: str = field(repr=False)
    context: AuthContext


def _decode_fernet_key(value: str) -> bytes:
    """Validate a Fernet-compatible key without importing cryptography here."""

    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("CUBE_ADAPTER_STATE_ENCRYPTION_KEY is not valid base64") from error
    if len(decoded) != 32:
        raise RuntimeError("CUBE_ADAPTER_STATE_ENCRYPTION_KEY must encode 32 bytes")
    return decoded


@dataclass(frozen=True)
class AdapterConfig:
    token: str
    session_hmac_key: str
    template: str
    audit_log: str
    bind: str = "127.0.0.1"
    port: int = 18080
    sandbox_timeout_seconds: int = 300
    max_command_seconds: int = 120
    audit_ui: bool = False
    profiles_file: Optional[str] = None
    state_backend_url: Optional[str] = None
    state_encryption_key: Optional[str] = field(default=None, repr=False)
    state_prefix: str = "cube-agent-adapter"
    gc_interval_seconds: int = 30
    lock_ttl_seconds: int = 180
    audit_sinks: tuple[str, ...] = ("file",)
    audit_http_url: Optional[str] = None
    audit_http_token: Optional[str] = field(default=None, repr=False)
    token_principals: tuple[TokenPrincipal, ...] = ()
    oidc_jwks_url: Optional[str] = None
    oidc_issuer: Optional[str] = None
    oidc_audience: Optional[str] = None
    oidc_tenant_claim: str = "tenant_id"
    oidc_roles_claim: str = "roles"
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    tls_client_ca_file: Optional[str] = None
    trust_client_cert_subject: bool = False
    readiness_cache_seconds: int = 5
    admin_enabled: bool = True

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        token = os.environ.get("CUBE_ADAPTER_TOKEN", "")
        principals = _load_token_principals(os.environ.get("CUBE_ADAPTER_TOKENS_FILE"))
        oidc_jwks_url = os.environ.get("CUBE_ADAPTER_OIDC_JWKS_URL") or None
        oidc_issuer = os.environ.get("CUBE_ADAPTER_OIDC_ISSUER") or None
        oidc_audience = os.environ.get("CUBE_ADAPTER_OIDC_AUDIENCE") or None
        trust_client_cert_subject = os.environ.get(
            "CUBE_ADAPTER_TRUST_CLIENT_CERT_SUBJECT", "0"
        ) == "1"
        if token and len(token) < 24:
            raise RuntimeError("CUBE_ADAPTER_TOKEN must contain at least 24 characters")
        if not token and not principals and not oidc_jwks_url and not trust_client_cert_subject:
            raise RuntimeError(
                "configure bearer, OIDC, or trusted mTLS authentication"
            )
        if oidc_jwks_url and (not oidc_issuer or not oidc_audience):
            raise RuntimeError("OIDC authentication requires issuer and audience")
        _validate_url(
            "CUBE_ADAPTER_OIDC_JWKS_URL",
            oidc_jwks_url,
            schemes=frozenset({"http", "https"}),
            https_or_loopback=True,
        )
        session_hmac_key = os.environ.get("CUBE_ADAPTER_HMAC_KEY", "")
        if len(session_hmac_key) < 32:
            raise RuntimeError("CUBE_ADAPTER_HMAC_KEY must contain at least 32 characters")
        template = os.environ.get("CUBE_TEMPLATE_ID", "")
        if not template:
            raise RuntimeError("CUBE_TEMPLATE_ID is required")

        encryption_key = os.environ.get("CUBE_ADAPTER_STATE_ENCRYPTION_KEY") or None
        state_url = os.environ.get("CUBE_ADAPTER_STATE_BACKEND_URL") or None
        if state_url not in {None, "memory", "memory://"}:
            _validate_url(
                "CUBE_ADAPTER_STATE_BACKEND_URL",
                state_url,
                schemes=frozenset({"redis", "rediss"}),
            )
            if not encryption_key:
                raise RuntimeError(
                    "Redis state requires CUBE_ADAPTER_STATE_ENCRYPTION_KEY"
                )
            _decode_fernet_key(encryption_key)

        tls_cert = os.environ.get("CUBE_ADAPTER_TLS_CERT_FILE") or None
        tls_key = os.environ.get("CUBE_ADAPTER_TLS_KEY_FILE") or None
        if bool(tls_cert) != bool(tls_key):
            raise RuntimeError("TLS certificate and key must be configured together")
        tls_client_ca = os.environ.get("CUBE_ADAPTER_TLS_CLIENT_CA_FILE") or None
        if tls_client_ca and not tls_cert:
            raise RuntimeError("mTLS client CA requires a TLS certificate and key")
        if trust_client_cert_subject and not tls_client_ca:
            raise RuntimeError(
                "trusted mTLS subject authentication requires a verified client CA"
            )

        sinks = _string_tuple(
            os.environ.get("CUBE_ADAPTER_AUDIT_SINKS", "file"),
            field_name="CUBE_ADAPTER_AUDIT_SINKS",
        )
        unsupported_sinks = set(sinks) - {"file", "stdout", "http"}
        if not sinks:
            raise RuntimeError("CUBE_ADAPTER_AUDIT_SINKS must not be empty")
        if unsupported_sinks:
            raise RuntimeError(f"unsupported audit sinks: {sorted(unsupported_sinks)}")
        audit_http_url = os.environ.get("CUBE_ADAPTER_AUDIT_HTTP_URL") or None
        if "http" in sinks and not audit_http_url:
            raise RuntimeError("http audit sink requires CUBE_ADAPTER_AUDIT_HTTP_URL")
        _validate_url(
            "CUBE_ADAPTER_AUDIT_HTTP_URL",
            audit_http_url,
            schemes=frozenset({"http", "https"}),
        )

        return cls(
            token=token,
            token_principals=principals,
            session_hmac_key=session_hmac_key,
            template=template,
            audit_log=os.environ.get(
                "CUBE_ADAPTER_AUDIT_LOG", "./cube-adapter-audit.jsonl"
            ),
            bind=os.environ.get("CUBE_ADAPTER_BIND", "127.0.0.1"),
            port=_positive_int("CUBE_ADAPTER_PORT", os.environ.get("CUBE_ADAPTER_PORT", 18080)),
            sandbox_timeout_seconds=_positive_int(
                "CUBE_ADAPTER_SANDBOX_TIMEOUT",
                os.environ.get("CUBE_ADAPTER_SANDBOX_TIMEOUT", 300),
            ),
            max_command_seconds=_positive_int(
                "CUBE_ADAPTER_MAX_COMMAND_SECONDS",
                os.environ.get("CUBE_ADAPTER_MAX_COMMAND_SECONDS", 120),
            ),
            audit_ui=os.environ.get("CUBE_ADAPTER_AUDIT_UI", "0") == "1",
            profiles_file=os.environ.get("CUBE_ADAPTER_PROFILES_FILE") or None,
            state_backend_url=state_url,
            state_encryption_key=encryption_key,
            state_prefix=os.environ.get("CUBE_ADAPTER_STATE_PREFIX", "cube-agent-adapter"),
            gc_interval_seconds=_positive_int(
                "CUBE_ADAPTER_GC_INTERVAL", os.environ.get("CUBE_ADAPTER_GC_INTERVAL", 30)
            ),
            lock_ttl_seconds=_positive_int(
                "CUBE_ADAPTER_LOCK_TTL", os.environ.get("CUBE_ADAPTER_LOCK_TTL", 180)
            ),
            audit_sinks=sinks,
            audit_http_url=audit_http_url,
            audit_http_token=os.environ.get("CUBE_ADAPTER_AUDIT_HTTP_TOKEN") or None,
            oidc_jwks_url=oidc_jwks_url,
            oidc_issuer=oidc_issuer,
            oidc_audience=oidc_audience,
            oidc_tenant_claim=os.environ.get(
                "CUBE_ADAPTER_OIDC_TENANT_CLAIM", "tenant_id"
            ),
            oidc_roles_claim=os.environ.get("CUBE_ADAPTER_OIDC_ROLES_CLAIM", "roles"),
            tls_cert_file=tls_cert,
            tls_key_file=tls_key,
            tls_client_ca_file=tls_client_ca,
            trust_client_cert_subject=trust_client_cert_subject,
            readiness_cache_seconds=_positive_int(
                "CUBE_ADAPTER_READINESS_CACHE_SECONDS",
                os.environ.get("CUBE_ADAPTER_READINESS_CACHE_SECONDS", 5),
            ),
            admin_enabled=os.environ.get("CUBE_ADAPTER_ADMIN_ENABLED", "1") == "1",
        )

    def profiles(self) -> Dict[str, ProfileConfig]:
        return load_profiles(
            self.profiles_file,
            default_template=self.template,
            sandbox_timeout_seconds=self.sandbox_timeout_seconds,
            max_command_seconds=self.max_command_seconds,
        )


def _load_token_principals(path: Optional[str]) -> tuple[TokenPrincipal, ...]:
    if not path:
        return ()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise RuntimeError(f"cannot read token principals file {path!r}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"token principals file {path!r} is invalid JSON") from error
    entries: Iterable[Any]
    if isinstance(raw, Mapping):
        entries = [dict(value, token=key) for key, value in raw.items()]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise RuntimeError("token principals file must be an object or list")
    principals = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RuntimeError("token principal entries must be objects")
        token = str(entry.get("token", ""))
        tenant = str(entry.get("tenant_id", ""))
        subject = str(entry.get("subject") or tenant)
        if len(token) < 24:
            raise RuntimeError("every configured bearer token must contain 24 characters")
        if not TENANT_ID_RE.fullmatch(tenant):
            raise RuntimeError(f"invalid token tenant_id: {tenant!r}")
        principals.append(
            TokenPrincipal(
                token=token,
                context=AuthContext(
                    tenant_id=tenant,
                    subject=subject,
                    roles=frozenset(
                        _string_tuple(entry.get("roles", ["runtime"]), field_name="roles")
                    ),
                    allowed_profiles=frozenset(
                        _string_tuple(
                            entry.get("allowed_profiles"), field_name="allowed_profiles"
                        )
                    ),
                    allowed_runtimes=frozenset(
                        _string_tuple(
                            entry.get("allowed_runtimes"), field_name="allowed_runtimes"
                        )
                    ),
                ),
            )
        )
    return tuple(principals)
