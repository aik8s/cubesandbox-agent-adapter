#!/usr/bin/env python3
"""Live v0.7.3 MCP canary with an offline template exposing port 50005.

The template must contain mcp-gateway and /opt/mcp_echo_server.py. Build it
on an image relay; snapshotting an ordinary code template does not add ports.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests
from cubesandbox import Sandbox


def payload(response: requests.Response) -> dict:
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("Content-Type", ""):
        return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True, help="Prebuilt template that exposes port 50005")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sandbox = None
    checks = {}
    report = {"backend": "v0.7.3", "checks": checks, "result": "FAIL"}
    stage = "template-prepare"
    try:
        template = args.template
        server_path = "/opt/mcp_echo_server.py"
        checks[stage] = "PASS"

        stage = "sdk-mcp-start"
        sandbox = Sandbox.create(
            template=template, timeout=600, allow_internet_access=False,
            network={"allow_public_traffic": False},
            mcp={name: {"runCmd": "python3 " + server_path, "envs": {"ECHO_NAME": name}} for name in ("alpha", "beta")},
        )
        checks[stage] = "PASS"
        token = sandbox.get_mcp_token()
        assert token
        assert sandbox.files.stat("/etc/mcp-gateway/.token", user="root")
        url = "http://" + os.environ.get("CUBE_PROXY_NODE_IP", "127.0.0.1") + ":" + os.environ.get("CUBE_PROXY_PORT_HTTP", "80") + "/mcp"
        headers = {"Host": sandbox.get_host(50005), "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "release-canary", "version": "1"}}}
        traffic_headers = {**headers, "cube-traffic-access-token": sandbox.traffic_access_token}
        stage = "authentication"
        no_gateway = requests.post(url, json=body, headers=traffic_headers, timeout=30)
        no_traffic = requests.post(url, json=body, headers={**headers, "Authorization": "Bearer " + token}, timeout=30)
        report["unauthenticated_status"] = {"missing_gateway_token": no_gateway.status_code, "missing_traffic_token": no_traffic.status_code}
        assert no_gateway.status_code == 401 and no_traffic.status_code == 403
        headers = {**traffic_headers, "Authorization": "Bearer " + token}
        response = requests.post(url, json=body, headers=headers, timeout=30)
        assert "result" in payload(response)
        if response.headers.get("Mcp-Session-Id"):
            headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        headers["MCP-Protocol-Version"] = "2025-03-26"
        requests.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers, timeout=30).raise_for_status()
        checks[stage] = "PASS"

        stage = "tool-aggregation-and-call"
        tools = payload(requests.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers, timeout=30))["result"]["tools"]
        names = sorted(tool["name"] for tool in tools)
        assert names == ["beta_echo", "echo"]
        for name, expected in (("echo", "alpha:RELEASE_MCP_OK"), ("beta_echo", "beta:RELEASE_MCP_OK")):
            result = payload(requests.post(url, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": {"text": "RELEASE_MCP_OK"}}}, headers=headers, timeout=30))["result"]
            assert result["content"][0]["text"] == expected
        report["tools"] = names
        checks[stage] = "PASS"
        report["result"] = "PASS"
    except Exception as error:
        checks[stage] = "FAIL"
        report["failure"] = {"stage": stage, "type": type(error).__name__}
    finally:
        errors = 0
        for item in (sandbox,):
            if item:
                try:
                    item.kill()
                except Exception:
                    errors += 1
        report["cleanup"] = {"complete": errors == 0, "errors": errors}
        if errors:
            report["result"] = "FAIL"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
