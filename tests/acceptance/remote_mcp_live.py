#!/usr/bin/env python3
"""Opt-in MCP HTTP acceptance against an isolated Adapter and real CubeSandbox.

Requires CUBE_REMOTE_MCP_URL and CUBE_ADAPTER_TOKEN. Creates only test-owned
leases and tasks, and releases them. Never prints credentials or resource IDs.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def unpack(result):
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise RuntimeError("MCP tool returned an error")
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = result.content
    return json.loads(content[0].text)


async def exercise(mode, url, token, profile, ca_file):
    checks = {}
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                  verify=ca_file or True, timeout=60, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(*streams) as session:
                if mode == "modern":
                    discovered = await session.discover()
                    extensions = discovered.capabilities.extensions or {}
                    assert "io.modelcontextprotocol/tasks" not in extensions
                    checks["tasks_extension_not_advertised"] = "PASS"
                else:
                    await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"cube_profiles", "cube_doctor", "cube_acquire", "cube_exec", "cube_release"} <= names
                checks["tool_discovery"] = "PASS"
                profiles = unpack(await session.call_tool("cube_profiles", {}))
                assert any(p["name"] == profile and "capabilities" in p for p in profiles["profiles"])
                diagnostic = unpack(await session.call_tool("cube_doctor", {}))
                assert any(p["name"] == profile and p["template"] == "ready" for p in diagnostic["profiles"])
                checks["profile_and_doctor"] = "PASS"
                if mode == "legacy":
                    return checks
                lease = unpack(await session.call_tool("cube_acquire", {
                    "runtime": "mcp", "session_key": "remote-mcp-" + uuid.uuid4().hex,
                    "profile": profile,
                }))["lease_ref"]
                try:
                    result = unpack(await session.call_tool("cube_exec", {
                        "lease_ref": lease, "command": "printf REMOTE_MCP_ACCEPTANCE_OK",
                    }))
                    assert result["exit_code"] == 0 and result["stdout"] == "REMOTE_MCP_ACCEPTANCE_OK"
                    checks["real_sandbox_exec"] = "PASS"
                finally:
                    unpack(await session.call_tool("cube_release", {"lease_ref": lease, "action": "kill"}))
                    checks["lease_release"] = "PASS"
                plan = unpack(await session.call_tool("cube_task_plan", {
                    "template": "trusted-auto", "parameters": {"message": "safe-remote-mcp"},
                }))
                task = unpack(await session.call_tool("cube_task_submit", {"plan_ref": plan["plan_ref"]}))
                task_ref = task["task_ref"]
                try:
                    for _ in range(60):
                        status = unpack(await session.call_tool("cube_task_status", {"task_ref": task_ref}))
                        if status["state"] not in {"starting", "running"}:
                            break
                        await asyncio.sleep(1)
                    assert status["state"] == "succeeded", status["state"]
                    final = unpack(await session.call_tool("cube_task_result", {"task_ref": task_ref}))
                    assert final["execution_state"] == "succeeded"
                    assert final["sandbox_cleanup_status"] == "verified"
                    assert final["snapshot_storage_cleanup_status"] == "not_verified"
                    assert final["receipt_ready"]
                    checks["task_and_separate_cleanup"] = "PASS"
                finally:
                    if "task_and_separate_cleanup" not in checks:
                        try:
                            await session.call_tool("cube_task_cancel", {"task_ref": task_ref})
                        except Exception:
                            checks["task_cleanup"] = "REQUIRES_OPERATOR_CHECK"
                return checks


async def main():
    url = os.environ["CUBE_REMOTE_MCP_URL"]
    token = os.environ["CUBE_ADAPTER_TOKEN"]
    profile = os.getenv("CUBE_ACCEPTANCE_PROFILE", "trusted-acceptance")
    ca_file = os.getenv("CUBE_REMOTE_MCP_CA_FILE")
    result = {}
    for mode in ("modern", "legacy"):
        result[mode] = await exercise(mode, url, token, profile, ca_file)
    print(json.dumps({"result": "PASS", "transport": "MCP Streamable HTTP",
                      "backend": "real CubeSandbox", "checks": result}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
