"""Read-only local connection diagnostic. Never prints tokens or backend addresses."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .mcp_server import AdapterClientError, AdapterHttpClient


def main() -> None:
    token = os.getenv("CUBE_ADAPTER_TOKEN", "")
    token_file = os.getenv("CUBE_ADAPTER_TOKEN_FILE")
    if not token and token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            print(json.dumps({"status": "error", "stage": "token_file", "code": "unreadable"}))
            raise SystemExit(2) from None
    if not token:
        print(json.dumps({"status": "error", "stage": "authentication", "code": "token_not_configured"}))
        raise SystemExit(2)
    try:
        client = AdapterHttpClient(
            os.getenv("CUBE_ADAPTER_URL", "http://127.0.0.1:18080"), token,
            ca_file=os.getenv("CUBE_ADAPTER_CA_FILE"), timeout=10,
        )
        result = client.post("/v1/doctor", {})
    except (AdapterClientError, RuntimeError, OSError) as error:
        code = "connection_failed"
        if isinstance(error, AdapterClientError):
            # Adapter error codes are bounded and never include headers or URLs.
            prefix = str(error).partition(":")[0]
            if prefix in {"unauthorized", "invalid_token", "action_denied", "adapter_error", "audit_unavailable"}:
                code = prefix
        print(json.dumps({"status": "error", "stage": "adapter", "code": code}))
        raise SystemExit(1) from None
    degraded = result["state"] == "error" or result["audit"] == "reconciliation_required" or any(
        profile["template"].lower() != "ready"
        or profile["proxy"] == "error"
        or profile["backend"] == "not_ready"
        for profile in result["profiles"]
    )
    print(json.dumps({"status": "degraded" if degraded else "ok", **result}, ensure_ascii=False, indent=2))
    if degraded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
