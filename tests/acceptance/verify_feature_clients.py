#!/usr/bin/env python3
"""Verify native client tool-result transcripts and emit a redacted report.

This reads the real clients' private logs/databases, not assistant summaries.
Raw inputs stay private. zstd is needed only for the DSH session log.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def text_json(content: list[dict]) -> dict:
    return json.loads(next(block["text"] for block in content if block.get("type") == "text"))


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def openclaw(path: Path) -> list[dict]:
    calls = []
    with readonly(path) as database:
        for (raw,) in database.execute("SELECT event_json FROM transcript_events ORDER BY seq"):
            message = json.loads(raw).get("message", {})
            if message.get("role") == "toolResult":
                assert not message.get("isError"), "OpenClaw tool error"
                calls.append({"name": message["toolName"], "result": message["details"]})
    return calls


def dsh(path: Path) -> list[dict]:
    executable = shutil.which("zstd")
    if not executable:
        raise RuntimeError("zstd is required to read the DSH session")
    source = subprocess.check_output(  # noqa: S603 -- fixed reader, absolute operator-selected input
        [executable, "-dc", "--", str(path.resolve())], text=True
    )
    names, calls = {}, []
    for line in source.splitlines():
        item = json.loads(line)
        data = item.get("data", {})
        if item["type"] == "tool/call":
            names[data["callId"]] = data["name"]
        elif item["type"] == "tool/result":
            for block in data["message"]["content"]:
                assert not block.get("isError"), "DSH tool error"
                calls.append(
                    {"name": names[block["toolCallId"]], "result": text_json(block["content"])}
                )
    return calls


def hermes(path: Path) -> list[dict]:
    calls = []
    with readonly(path) as database:
        for name, content in database.execute(
            "SELECT tool_name,content FROM messages WHERE role='tool' ORDER BY id"
        ):
            if name == "tool_describe":  # Hermes' built-in schema discovery.
                continue
            calls.append({"name": name, "result": json.loads(content)})
    return calls


def codex(path: Path) -> list[dict]:
    calls = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        item = event.get("item", {})
        assert item.get("type") not in {"command_execution", "file_change", "web_search"}, (
            "unexpected Codex host tool"
        )
        if event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            assert item["server"] == "cubesandbox"
            assert item["status"] == "completed" and not item.get("error"), "Codex tool failed"
            calls.append({"name": item["tool"], "result": text_json(item["result"]["content"])})
    return calls


def opencode(path: Path) -> list[dict]:
    calls = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event.get("type") == "tool_use":
            part = event["part"]
            assert part["state"]["status"] == "completed", "OpenCode tool failed"
            assert part["tool"].startswith("cubesandbox_")
            calls.append(
                {
                    "name": part["tool"].removeprefix("cubesandbox_"),
                    "result": json.loads(part["state"]["output"]),
                }
            )
    return calls


def verify(calls: list[dict]) -> dict:
    allowed = {
        "cube_profiles",
        "cube_acquire",
        "cube_exec",
        "cube_renew",
        "cube_job_start",
        "cube_job_status",
        "cube_job_resume",
        "cube_release",
    }
    assert calls and all(call["name"] in allowed for call in calls), "unexpected client tool"
    grouped = {name: [call["result"] for call in calls if call["name"] == name] for name in allowed}
    for call in calls:
        assert "error" not in call["result"], "Adapter error in client result"
    profiles = grouped["cube_profiles"][0]["profiles"]
    assert {item["name"] for item in profiles} == {"staging-code", "production-code"}
    assert {item["backend_id"] for item in profiles} == {"staging", "production"}
    executions = grouped["cube_exec"]
    assert len(executions) == 2 and all(item["exit_code"] == 0 for item in executions)
    assert {item["stdout"] for item in executions} == {"STAGING_ROUTE_OK", "PRODUCTION_ROUTE_OK"}
    sandboxes = {item["sandbox_ref"] for item in executions}
    assert len(sandboxes) == 2, "profiles shared a sandbox"
    assert len(grouped["cube_renew"]) == 1
    renewed = grouped["cube_renew"][0]
    assert renewed["profile"] == "staging-code" and renewed["reused"]
    assert renewed["backend_id"] == "staging" and renewed["state"] == "running"
    staging_ref = next(
        item["sandbox_ref"] for item in executions if item["stdout"] == "STAGING_ROUTE_OK"
    )
    assert renewed["sandbox_ref"] == staging_ref
    for acquired in grouped["cube_acquire"]:
        assert acquired["state"] == "running"
        assert acquired["profile"] in {"staging-code", "production-code"}
        expected_backend = acquired["profile"].removesuffix("-code")
        assert acquired["backend_id"] == expected_backend
        expected_marker = expected_backend.upper() + "_ROUTE_OK"
        assert acquired["sandbox_ref"] == next(
            item["sandbox_ref"] for item in executions if item["stdout"] == expected_marker
        )
    assert len(grouped["cube_job_start"]) == 1
    started = grouped["cube_job_start"][0]
    assert started["sandbox_ref"] == staging_ref
    assert grouped["cube_job_status"][-1]["state"] == "succeeded"
    for item in grouped["cube_job_status"] + grouped["cube_job_resume"]:
        assert item["job_ref"] == started["job_ref"] and item["sandbox_ref"] == staging_ref
        assert item["backend_id"] == "staging"
    resumed = grouped["cube_job_resume"][0]
    assert resumed["state"] == "succeeded" and not resumed["restarted"]
    for stream, marker in (("stdout", b"RESUME_STDOUT_OK"), ("stderr", b"RESUME_STDERR_OK")):
        value = resumed[stream]
        assert base64.b64decode(value["data_base64"], validate=True) == marker
        assert value["offset"] == 0 and value["next_offset"] == len(marker) and value["eof"]
    for following in grouped["cube_job_resume"][1:]:
        assert following["state"] == "succeeded" and not following["restarted"]
        for stream in ("stdout", "stderr"):
            value = following[stream]
            assert value["offset"] == value["next_offset"] == resumed[stream]["next_offset"]
            assert value["data_base64"] == "" and value["eof"]
    releases = grouped["cube_release"]
    assert len(releases) == 2 and all(item["action"] == "kill" for item in releases)
    assert {item["sandbox_ref"] for item in releases} == sandboxes
    first_release = next(
        index for index, item in enumerate(calls) if item["name"] == "cube_release"
    )
    assert all(item["name"] == "cube_release" for item in calls[first_release:])
    return {
        "result": "PASS",
        "tool_calls": len(calls),
        "tools": [item["name"] for item in calls],
        "checks": {
            name: "PASS"
            for name in (
                "profile-discovery",
                "two-cluster-routing",
                "distinct-sandboxes",
                "bounded-renewal",
                "independent-log-cursors",
                "both-sandboxes-killed",
            )
        },
        "stream_next_offsets": {
            "stdout": len(b"RESUME_STDOUT_OK"),
            "stderr": len(b"RESUME_STDERR_OK"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("openclaw", "dsh", "hermes", "codex", "opencode"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = {
        "suite": "native-feature-clients",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": "CubeSandbox v0.7.3",
        "sdk": "0.7.3",
        "clients": {},
        "evidence": "validated native tool-result JSON; independent stdout/stderr bytes",
    }
    readers = {
        "openclaw": openclaw,
        "dsh": dsh,
        "hermes": hermes,
        "codex": codex,
        "opencode": opencode,
    }
    for name, reader in readers.items():
        try:
            report["clients"][name] = verify(reader(getattr(args, name)))
        except Exception as error:
            report["clients"][name] = {"result": "FAIL", "error_type": type(error).__name__}
    report["result"] = (
        "PASS" if all(item["result"] == "PASS" for item in report["clients"].values()) else "FAIL"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
