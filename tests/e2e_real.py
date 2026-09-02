#!/usr/bin/env python3
"""Disposable, opt-in E2E against a real deployed Adapter and CubeSandbox."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ["CUBE_E2E_ADAPTER_URL"].rstrip("/")
TOKEN = os.environ["CUBE_E2E_ADAPTER_TOKEN"]
PROFILE = os.environ.get("CUBE_E2E_PROFILE", "offline-code")


def post(path: str, body: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=330) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode("utf-8", "replace")) from error


def main() -> None:
    lease = post(
        "/v1/leases/acquire",
        {
            "runtime": "mcp",
            "session_key": "github-e2e-" + uuid.uuid4().hex,
            "profile": PROFILE,
        },
    )["lease_ref"]
    try:
        result = post(
            f"/v1/leases/{lease}/exec",
            {"command": "printf cube-e2e", "cwd": "/workspace"},
        )
        assert result["exit_code"] == 0 and result["stdout"] == "cube-e2e"
        post(
            f"/v1/leases/{lease}/write",
            {"path": "/workspace/e2e.txt", "content": "persistent-through-api"},
        )
        read = post(f"/v1/leases/{lease}/read", {"path": "/workspace/e2e.txt"})
        assert read["content"] == "persistent-through-api"
        job = post(
            f"/v1/leases/{lease}/jobs",
            {"command": "sleep 1; printf async-e2e", "cwd": "/workspace"},
        )["job_ref"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = post(f"/v1/jobs/{job}/status", {})
            if status["state"] not in {"running", "starting"}:
                break
            time.sleep(0.5)
        assert status["state"] == "succeeded"
        output = post(f"/v1/jobs/{job}/output", {})
        assert "async-e2e" in output["data"]
    finally:
        post(f"/v1/leases/{lease}/release", {"action": "kill"})


if __name__ == "__main__":
    main()
