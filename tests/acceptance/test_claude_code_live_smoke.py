from __future__ import annotations

import json
import unittest

from claude_code_live_smoke import FLOW, PREFIX, parse_events, validate


def stream(tools: tuple[str, ...], sentinel: str) -> str:
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": PREFIX + tool}]
            },
        }
        for tool in tools
    ]
    events.append(
        {
            "type": "result",
            "is_error": False,
            "permission_denials": [],
            "result": sentinel,
        }
    )
    return "\n".join(json.dumps(event) for event in events)


class ClaudeCodeAcceptanceRunnerTest(unittest.TestCase):
    def test_both_flows_parse_and_validate(self) -> None:
        for flow in FLOW.values():
            calls, result = parse_events(stream(flow["tools"], flow["sentinel"]))
            validate(calls, result, flow["tools"], flow["sentinel"])

    def test_trusted_flow_allows_status_polling(self) -> None:
        flow = FLOW["trusted"]
        tools = (*flow["tools"][:3], "cube_task_status", *flow["tools"][3:])
        calls, result = parse_events(stream(tools, flow["sentinel"]))
        validate(calls, result, flow["tools"], flow["sentinel"])

    def test_missing_or_unexpected_tool_is_rejected(self) -> None:
        flow = FLOW["direct"]
        for tools in (flow["tools"][:-1], (*flow["tools"], "cube_write_text")):
            calls, result = parse_events(stream(tools, flow["sentinel"]))
            with self.assertRaises(RuntimeError):
                validate(calls, result, flow["tools"], flow["sentinel"])


if __name__ == "__main__":
    unittest.main()
