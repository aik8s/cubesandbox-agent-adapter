#!/usr/bin/env python3
"""Exercise the Codex/MCP and Hermes trusted-task transports against a live Adapter."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from adapter.mcp_server import AdapterHttpClient
from plugins.hermes import register


class HermesContext:
    def __init__(self, adapter_url: str):
        self.settings = {
            "adapter_url": adapter_url,
            "token_env": "CUBE_ACCEPT_AGENT_TOKEN",
            "profile": "trusted-acceptance",
        }
        self.tools: dict[str, dict[str, Any]] = {}

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def register_tool(self, **definition: Any) -> None:
        self.tools[definition["name"]] = definition


def wait_terminal(call, task_ref: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for _ in range(120):
        latest = call("cube_task_status", {"task_ref": task_ref})
        if latest.get("state") not in {"starting", "running"}:
            return latest
        time.sleep(0.25)
    raise TimeoutError(f"task did not finish: {task_ref}")


def flow(name: str, call) -> dict[str, Any]:
    suffix = f"safe-{name}-{int(time.time() * 1000):x}".lower()
    plan = call(
        "cube_task_plan",
        {"template": "trusted-auto", "parameters": {"message": suffix}},
    )
    assert plan["state"] == "ready"
    task = call("cube_task_submit", {"plan_ref": plan["plan_ref"]})
    status = wait_terminal(call, task["task_ref"])
    assert status["state"] == "succeeded"
    result = call("cube_task_result", {"task_ref": task["task_ref"]})
    assert result["state"] == "succeeded"
    assert result["result"]["cleanup"] == "verified"
    receipt = call("cube_task_receipt", {"task_ref": task["task_ref"]})
    assert receipt["receipt"]["signature"]["alg"] == "HS256"
    return {
        "client": name,
        "state": result["state"],
        "cleanup": result["result"]["cleanup"],
        "receipt_alg": receipt["receipt"]["signature"]["alg"],
        "task_ref": task["task_ref"],
    }


def main() -> None:
    adapter_url = os.environ.get("CUBE_ADAPTER_URL", "http://127.0.0.1:19080")
    token = os.environ.get("CUBE_ACCEPT_AGENT_TOKEN", "")
    if not token:
        raise SystemExit("CUBE_ACCEPT_AGENT_TOKEN is required")

    mcp_client = AdapterHttpClient(adapter_url, token)

    def call_mcp(name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name == "cube_task_plan":
            return mcp_client.post("/v1/tasks/plan", params)
        if name == "cube_task_submit":
            return mcp_client.post(
                f"/v1/task-plans/{mcp_client.segment(params['plan_ref'])}/submit", {}
            )
        task_ref = mcp_client.segment(params["task_ref"])
        action = name.removeprefix("cube_task_")
        return mcp_client.post(f"/v1/tasks/{task_ref}/{action}", {})

    codex = flow("codex-mcp", call_mcp)

    hermes_context = HermesContext(adapter_url)
    register(hermes_context)
    assert len(hermes_context.tools) == 19

    def call_hermes(name: str, params: dict[str, Any]) -> dict[str, Any]:
        raw = hermes_context.tools[name]["handler"](
            params, task_id="trusted-acceptance-hermes"
        )
        value = json.loads(raw)
        if "error" in value:
            raise RuntimeError(value["error"])
        return value

    hermes = flow("hermes", call_hermes)
    result = {
        "result": "PASS",
        "clients": [codex, hermes],
        "tools_per_client": 19,
        "trusted_task_tools_per_client": 6,
    }
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output := os.environ.get("CUBE_ACCEPT_CLIENT_OUTPUT"):
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
