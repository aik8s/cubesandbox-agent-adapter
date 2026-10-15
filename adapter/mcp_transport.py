"""Private, fixed-protocol subprocess transport. Not a listening service."""

from __future__ import annotations

import json
import sys
import time

import requests

from .mcp_policy import McpPolicy, _GatewaySession


def main() -> int:
    session = None
    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return 1
        value = json.loads(raw)
        session = _GatewaySession.__new__(_GatewaySession)
        session.url = value["url"]
        session.headers = value["headers"]
        session.policy = McpPolicy(
            "transport",
            {},
            {},
            timeout_seconds=value["timeout_seconds"],
            max_output_bytes=value["max_output_bytes"],
        )
        session.session = requests.Session()
        session.session.trust_env = False
        session.sequence = 0
        session.deadline = time.monotonic() + session.policy.timeout_seconds
        session.initialize()
        result = session.request("tools/call", value["params"])
        result = {
            key: item
            for key, item in result.items()
            if key in {"content", "structuredContent", "isError"}
        }
        encoded = json.dumps(result, ensure_ascii=False).encode()
        if len(encoded) > session.policy.max_output_bytes:
            return 1
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1
    finally:
        if session:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
