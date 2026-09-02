#!/usr/bin/env python3
"""Compatibility entrypoint for the modular CubeSandbox Agent Adapter."""

from __future__ import annotations

import argparse
import signal
import threading
from typing import Any

from .config import AdapterConfig
from .core import VERSION, AdapterError, CubeAdapter
from .http_api import build_server, make_handler

__all__ = [
    "AdapterConfig",
    "AdapterError",
    "CubeAdapter",
    "VERSION",
    "make_handler",
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authenticated CubeSandbox Agent adapter"
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.parse_args()
    config = AdapterConfig.from_env()
    adapter = CubeAdapter(config)
    server = build_server(config, adapter)

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        adapter.close()
        server.server_close()


if __name__ == "__main__":
    main()
