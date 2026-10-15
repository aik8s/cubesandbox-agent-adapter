"""Operator-owned backend bindings; no request may supply an endpoint."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml
from cubesandbox import Config

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REFERENCE_RE = re.compile(r"^\$\{(ENV|FILE):([^}]+)\}$")


def resolve_references(value: Any) -> Any:
    """Resolve complete leaves only; never interpolate or evaluate code."""
    if isinstance(value, str):
        match = REFERENCE_RE.fullmatch(value)
        if not match:
            return value
        kind, name = match.groups()
        if kind == "ENV":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name not in os.environ:
                raise RuntimeError("missing or invalid environment reference")
            return os.environ[name]
        path = Path(name)
        if not path.is_absolute():
            raise RuntimeError("Secret file references must be absolute")
        try:
            if path.stat().st_size > 65536:
                raise RuntimeError("Secret file exceeds the size limit")
            return path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise RuntimeError("Secret file reference cannot be read") from error
    if isinstance(value, Mapping):
        return {str(key): resolve_references(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_references(item) for item in value]
    return value


@dataclass(frozen=True)
class BackendConfig:
    backend_id: str
    api_url: str = field(repr=False)
    proxy_host: str = field(repr=False)
    proxy_port: int = 80
    sandbox_domain: str = "cube.app"
    api_key: str | None = field(default=None, repr=False)
    legacy: bool = False

    @property
    def fingerprint(self) -> str:
        # Credential rotation is allowed; rebinding live resources to another
        # cluster is not. Persist this on every lease before its first result.
        return hashlib.sha256(
            json.dumps(
                [self.api_url, self.proxy_host, self.proxy_port, self.sandbox_domain],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def sdk_config(self) -> Config:
        return Config(
            api_url=self.api_url,
            api_key=self.api_key,
            proxy_node_ip=self.proxy_host,
            proxy_port=self.proxy_port,
            sandbox_domain=self.sandbox_domain,
        )

    def kwargs(self) -> dict[str, Any]:
        return {} if self.legacy else {"config": self.sdk_config()}


class BackendRegistry:
    def __init__(self, path: str | None) -> None:
        self.entries: dict[str, BackendConfig] = {}
        if path is None:
            cfg = Config()
            self.entries = {
                "default": BackendConfig(
                    "default",
                    cfg.api_url,
                    cfg.proxy_node_ip or "",
                    cfg.proxy_port,
                    cfg.sandbox_domain,
                    cfg.api_key,
                    legacy=True,
                )
            }
            return
        try:
            document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise RuntimeError("backend registry cannot be loaded") from error
        if not isinstance(document, Mapping) or set(document) != {"backends"}:
            raise RuntimeError("backend registry must contain only backends")
        values = document["backends"]
        if not isinstance(values, Mapping) or not values:
            raise RuntimeError("backend registry must not be empty")
        fields = {"api_url", "api_key", "proxy_host", "proxy_port", "sandbox_domain"}
        for backend_id, raw in values.items():
            if not isinstance(backend_id, str) or not NAME_RE.fullmatch(backend_id):
                raise RuntimeError("invalid backend identifier")
            if not isinstance(raw, Mapping) or set(raw) - fields:
                raise RuntimeError("backend contains unsupported fields")
            value = resolve_references(raw)
            api_url = value.get("api_url", "")
            parsed = urlsplit(api_url) if isinstance(api_url, str) else None
            if (
                parsed is None
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise RuntimeError(
                    "backend API must be an HTTP(S) URL without embedded credentials"
                )
            host = value.get("proxy_host")
            domain = value.get("sandbox_domain", "cube.app")
            if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", host):
                raise RuntimeError("backend proxy_host must be a hostname or IP")
            if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
                raise RuntimeError("invalid sandbox domain")
            port = value.get("proxy_port", 80)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise RuntimeError("invalid backend proxy port")
            api_key = value.get("api_key")
            if api_key is not None and not isinstance(api_key, str):
                raise RuntimeError("invalid backend credential reference")
            self.entries[backend_id] = BackendConfig(
                backend_id,
                api_url.rstrip("/"),
                host,
                port,
                domain,
                api_key,
            )

    def get(self, backend_id: str) -> BackendConfig:
        try:
            return self.entries[backend_id]
        except KeyError as error:
            raise RuntimeError("resource backend is not configured") from error
