#!/usr/bin/env python3
"""Run a real Claude Code flow through the Adapter MCP facade.

The supplied MCP config should reference credentials by file path. Raw Claude
events remain in memory; this program emits only a redacted acceptance summary.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

TRUSTED_TOOLS = (
    "cube_task_plan",
    "cube_task_submit",
    "cube_task_status",
    "cube_task_result",
    "cube_task_receipt",
)
DIRECT_TOOLS = ("cube_acquire", "cube_exec", "cube_status", "cube_release")
PREFIX = "mcp__cubesandbox__"
TRUSTED_SENTINEL = (
    "CLAUDE_CODE_CUBESANDBOX_OK "
    "state=succeeded cleanup=verified receipt=HS256"
)
DIRECT_SENTINEL = (
    "CLAUDE_CODE_DIRECT_OK "
    "output=CLAUDE_CODE_CUBESANDBOX_OK cleanup=released"
)
FLOW = {
    "trusted": {
        "tools": TRUSTED_TOOLS,
        "sentinel": TRUSTED_SENTINEL,
        "prompt": (
            "Use only the cubesandbox MCP tools. Call cube_task_plan with template "
            "trusted-auto and parameter message safe-claude-live. Submit that plan, "
            "poll status until terminal, then fetch result and receipt. Verify state "
            "is succeeded, result.cleanup is verified, and receipt.signature.alg is "
            "HS256. Never print identifiers, receipts, credentials, URLs, commands, "
            "or payload values. Finish with exactly: " + TRUSTED_SENTINEL
        ),
    },
    "direct": {
        "tools": DIRECT_TOOLS,
        "sentinel": DIRECT_SENTINEL,
        "prompt": (
            "Use only the cubesandbox MCP tools. Acquire one lease with runtime mcp "
            "and session_key safe-claude-direct. Use its lease_ref to run printf "
            "CLAUDE_CODE_CUBESANDBOX_OK, read lease status, then release it with action "
            "kill. Verify the output and successful release. Never print identifiers, "
            "credentials, URLs, addresses, or payload values. Finish with exactly: "
            + DIRECT_SENTINEL
        ),
    },
}


def parse_events(output: str) -> tuple[list[str], dict[str, Any]]:
    calls: list[str] = []
    result: dict[str, Any] = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for item in event.get("message", {}).get("content", []):
                name = item.get("name", "") if item.get("type") == "tool_use" else ""
                if name.startswith(PREFIX):
                    calls.append(name.removeprefix(PREFIX))
        elif event.get("type") == "result":
            result = event
    return calls, result


def validate(
    calls: list[str],
    result: dict[str, Any],
    expected: tuple[str, ...],
    sentinel: str,
) -> None:
    cursor = 0
    for call in calls:
        if cursor < len(expected) and call == expected[cursor]:
            cursor += 1
        elif not ("cube_task_status" in expected and call == "cube_task_status"):
            raise RuntimeError("Claude Code used an unexpected MCP tool sequence")
    if cursor != len(expected):
        raise RuntimeError("Claude Code did not complete all required MCP tools")
    if result.get("is_error") or sentinel not in str(result.get("result", "")):
        raise RuntimeError("Claude Code did not report the verified terminal outcome")
    if result.get("permission_denials"):
        raise RuntimeError("Claude Code encountered an MCP permission denial")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--flow", choices=sorted(FLOW), default="trusted")
    args = parser.parse_args()

    flow = FLOW[args.flow]
    tools = flow["tools"]
    allowed = ",".join(PREFIX + tool for tool in tools)
    command = [
        args.claude,
        "-p",
        flow["prompt"],
        "--mcp-config",
        str(args.mcp_config),
        "--strict-mcp-config",
        "--allowedTools",
        allowed,
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--max-budget-usd",
        "1.00",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if completed.returncode:
        raise SystemExit(f"Claude Code exited with status {completed.returncode}")
    calls, result = parse_events(completed.stdout)
    validate(calls, result, tools, flow["sentinel"])
    version = subprocess.run(  # noqa: S603
        [args.claude, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    summary = {
        "client": "Claude Code",
        "client_version": version,
        "cleanup": "verified" if args.flow == "trusted" else "released",
        "flow": args.flow,
        "permission_denials": 0,
        "result": "PASS",
        "tool_calls": calls,
    }
    if args.flow == "trusted":
        summary.update(state="succeeded", receipt_algorithm="HS256")
    else:
        summary.update(output="CLAUDE_CODE_CUBESANDBOX_OK")
    serialized = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
