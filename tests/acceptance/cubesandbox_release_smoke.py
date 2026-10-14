#!/usr/bin/env python3
"""Disposable release canaries with redacted, version-labelled evidence.

Connection settings use the CubeSandbox SDK's CUBE_* variables. The backend
version is a deployment label, not inferred from the SDK package version.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import requests
from cubesandbox import Config, Sandbox


def execute(sandbox: Sandbox, command: str) -> str:
    result = sandbox.commands.run(command, timeout=30)
    if result.exit_code != 0:
        raise AssertionError("sandbox command did not succeed")
    return (result.stdout or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--expect-v073", action="store_true")
    args = parser.parse_args()
    cfg = Config()
    headers = {"X-API-Key": cfg.api_key} if cfg.api_key else {}
    template = os.environ.get("CUBE_TEMPLATE_ID", "agent-code")
    sandboxes: list[Sandbox] = []
    snapshot_id = ""
    checks: dict[str, str] = {}
    timings: list[dict[str, float]] = []
    stage = "initialization"
    report = {
        "backend": args.backend,
        "environment": args.environment,
        "sdk_package_version": version("cubesandbox"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "result": "FAIL",
        "cleanup": {},
    }
    try:
        stage = "create-and-execute"
        for _ in range(args.rounds):
            started = time.monotonic()
            sandbox = Sandbox.create(template=template, timeout=600, allow_internet_access=False)
            sandboxes.append(sandbox)
            created = time.monotonic()
            assert execute(sandbox, "printf RELEASE_CANARY_OK") == "RELEASE_CANARY_OK"
            executed = time.monotonic()
            sandbox.kill()
            sandboxes.remove(sandbox)
            finished = time.monotonic()
            timings.append({
                "create_ms": round((created - started) * 1000, 2),
                "first_command_ms": round((executed - created) * 1000, 2),
                "delete_ms": round((finished - executed) * 1000, 2),
            })
        checks[stage] = f"PASS ({args.rounds} rounds)"
        report["timings"] = {
            "samples": timings,
            "median_ms": {
                key: round(statistics.median(item[key] for item in timings), 2)
                for key in timings[0]
            },
            "scope": "client-observed API latency including transport; not a concurrency benchmark",
        }

        stage = "pause-metadata-resume"
        metadata = {"release-canary": "metadata-retained"}
        source = Sandbox.create(template=template, timeout=600, metadata=metadata, allow_internet_access=False)
        sandboxes.append(source)
        before = source.get_info()
        source.files.write("/tmp/release-root", "root-before")
        started = time.monotonic()
        source.pause()
        report["pause_client_ms"] = round((time.monotonic() - started) * 1000, 2)
        paused = source.get_info()
        report["paused_metadata_retained"] = all(
            paused.metadata.get(key) == value for key, value in metadata.items()
        )
        report["paused_template_retained"] = paused.template_id == before.template_id
        if args.expect_v073:
            assert report["paused_metadata_retained"] and report["paused_template_retained"]
        source.resume(timeout=600)
        assert source.files.read("/tmp/release-root") == "root-before"
        checks[stage] = "PASS"

        stage = "snapshot-rollback"
        snapshot_id = source.create_snapshot(name="release-smoke").snapshot_id
        source.files.write("/tmp/release-root", "root-after")
        source.rollback(snapshot_id)
        assert source.files.read("/tmp/release-root") == "root-before"
        checks[stage] = "PASS"

        stage = "snapshot-restore-clone"
        restored = Sandbox.create(template=snapshot_id, timeout=600, allow_internet_access=False)
        sandboxes.append(restored)
        assert restored.files.read("/tmp/release-root") == "root-before"
        clone = source.clone(n=1)[0]
        sandboxes.append(clone)
        assert clone.files.read("/tmp/release-root") == "root-before"
        clone.files.write("/tmp/release-root", "clone-only")
        assert source.files.read("/tmp/release-root") == "root-before"
        checks[stage] = "PASS"
        for sandbox in (clone, restored):
            sandbox.kill()
            sandboxes.remove(sandbox)

        stage = "deleted-snapshot-status"
        deleted_id = snapshot_id
        Sandbox.delete_snapshot(snapshot_id)
        snapshot_id = ""
        response = requests.post(
            cfg.api_url + "/sandboxes",
            json={"templateID": deleted_id, "timeout": 60},
            headers=headers,
            timeout=30,
        )
        report["deleted_snapshot_http_status"] = response.status_code
        if response.ok:
            unexpected = Sandbox(response.json(), cfg)
            sandboxes.append(unexpected)
            raise AssertionError("deleted snapshot unexpectedly restored")
        if args.expect_v073:
            assert response.status_code == 404
        checks[stage] = "PASS" if response.status_code == 404 else "OBSERVED (pre-fix)"

        stage = "template-pagination"
        response = requests.get(cfg.api_url + "/templates", params={"limit": 1, "offset": 0}, headers=headers, timeout=30)
        assert response.status_code == 200
        data = response.json()
        items = data if isinstance(data, list) else data.get("templates", data.get("items", []))
        report["template_page_size"] = len(items)
        # The v0.7.3 pagination change is on CubeMaster's /cube/template
        # endpoint and cubemastercli, not the E2B /templates facade.
        checks[stage] = "OBSERVED (E2B facade remains unpaged; verify CLI separately)"

        stage = "mcp-shape-validation"
        response = requests.post(cfg.api_url + "/sandboxes", json={"templateID": template, "mcp": []}, headers=headers, timeout=30)
        report["invalid_mcp_http_status"] = response.status_code
        if response.ok:
            sandboxes.append(Sandbox(response.json(), cfg))
        if args.expect_v073:
            assert response.status_code == 400
        checks[stage] = "PASS" if response.status_code == 400 else "OBSERVED (pre-fix)"
        report["result"] = "PASS"
    except Exception as error:
        checks[stage] = "FAIL"
        report["failure"] = {"stage": stage, "type": type(error).__name__}
    finally:
        cleanup_errors = 0
        for sandbox in reversed(sandboxes):
            try:
                sandbox.kill()
            except Exception:
                cleanup_errors += 1
        if snapshot_id:
            try:
                Sandbox.delete_snapshot(snapshot_id)
            except Exception:
                cleanup_errors += 1
        report["cleanup"] = {"complete": cleanup_errors == 0, "errors": cleanup_errors}
        if cleanup_errors:
            report["result"] = "FAIL"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
