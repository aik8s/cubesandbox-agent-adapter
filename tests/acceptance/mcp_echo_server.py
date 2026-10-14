#!/usr/bin/env python3
"""Offline stdio MCP fixture: one harmless echo tool, no external services."""
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "offline-echo", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "echo", "description": "Echo a test marker",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        }]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": os.environ["ECHO_NAME"] + ":" + request["params"]["arguments"]["text"]}]}
    elif method == "ping":
        result = {}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "unsupported method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
