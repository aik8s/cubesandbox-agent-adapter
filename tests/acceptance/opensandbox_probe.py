#!/usr/bin/env python3
"""Verify OpenSandbox SDK's direct-connection boundary without creating a sandbox.

Requires OpenSandbox SDK 1.1.1 and an isolated candidate Adapter. This is a
negative compatibility probe, not an OpenSandbox protocol implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from urllib.parse import urlsplit

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.transport import RetryPolicy


async def probe(add_bearer):
    token = os.environ["CUBE_ADAPTER_TOKEN"]
    target = urlsplit(os.environ["CUBE_ADAPTER_URL"])
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("probe accepts an isolated loopback Adapter only")
    observed = []

    async def record(response):
        observed.append({"method": response.request.method, "path": response.request.url.path,
                         "status": response.status_code,
                         "opensandbox_header": "open-sandbox-api-key" in response.request.headers,
                         "bearer_header": "authorization" in response.request.headers})

    cfg = ConnectionConfig(domain=target.netloc, protocol="http", api_key=token,
        headers={"Authorization": "Bearer " + token} if add_bearer else {},
        disable_metrics=True, retry_policy=RetryPolicy.disabled(), event_hooks={"response": [record]})
    try:
        sandbox = await Sandbox.create("compatibility-probe", ready_timeout=timedelta(seconds=3),
                                       connection_config=cfg)
    except Exception:
        pass
    else:
        try:
            await sandbox.destroy()
        finally:
            raise AssertionError("a direct OpenSandbox create unexpectedly succeeded")
    assert len(observed) == 1 and observed[0]["path"] == "/v1/sandboxes", observed
    assert observed[0]["status"] == (404 if add_bearer else 401), observed
    return observed[0]


async def main():
    without_bearer = await probe(False)
    with_bearer = await probe(True)
    print(json.dumps({"sdk": "opensandbox==1.1.1", "direct_compatibility": "unsupported",
                      "default_sdk_request": without_bearer,
                      "with_adapter_bearer": with_bearer,
                      "sandbox_created": False}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
