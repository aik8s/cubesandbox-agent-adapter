"""In-pod health probe that remains usable with default-deny ingress."""

from __future__ import annotations

import os
import socket
import ssl
import sys
from urllib.request import urlopen


def check(kind: str) -> bool:
    if kind not in {"live", "ready"}:
        raise ValueError("probe kind must be 'live' or 'ready'")
    port = int(os.environ.get("CUBE_ADAPTER_PORT", "18080"))
    timeout = float(os.environ.get("CUBE_ADAPTER_PROBE_TIMEOUT", "3"))

    # A server configured for mTLS rejects an HTTP probe without a client
    # certificate. Match the chart's former tcpSocket behaviour in that mode.
    if os.environ.get("CUBE_ADAPTER_TLS_CLIENT_CA_FILE"):
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True

    tls = bool(os.environ.get("CUBE_ADAPTER_TLS_CERT_FILE"))
    scheme = "https" if tls else "http"
    path = "/healthz" if kind == "live" else "/readyz"
    # Kubernetes HTTPS probes do not verify the serving certificate either;
    # this connection never leaves the Pod network namespace.
    context = ssl._create_unverified_context() if tls else None  # noqa: S323, SLF001
    with urlopen(f"{scheme}://127.0.0.1:{port}{path}", timeout=timeout, context=context) as response:
        response.read(4096)
        return 200 <= response.status < 300


def main() -> int:
    try:
        return 0 if check(sys.argv[1] if len(sys.argv) > 1 else "live") else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
