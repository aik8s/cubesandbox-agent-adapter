#!/usr/bin/env python3
"""Verify an operator-owned internal preset with a real OpenClaw model reply.

Inputs reference private backend/profile/preset files. The preset must provide
the intended model and Secret reference. No endpoint, key or raw reply is
included in the public result. The disposable sandbox is always removed.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import uuid
from pathlib import Path

from cubesandbox import Sandbox

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from adapter.config import AdapterConfig  # noqa: E402
from adapter.core import CubeAdapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("backends", "profiles", "presets", "workdir", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--config-path", default="/tmp/internal-openclaw/openclaw.json")
    parser.add_argument("--state-dir", default="/tmp/internal-openclaw")
    args = parser.parse_args()
    args.workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(args.workdir, 0o700)
    handles = []

    def create(**kwargs):
        sandbox = Sandbox.create(**kwargs)
        handles.append(sandbox)
        return sandbox

    config = AdapterConfig(
        token=uuid.uuid4().hex,
        session_hmac_key=uuid.uuid4().hex + uuid.uuid4().hex,
        template="",
        backends_file=str(args.backends),
        profiles_file=str(args.profiles),
        template_presets_file=str(args.presets),
        audit_log=str(args.workdir / "audit.jsonl"),
    )
    adapter = CubeAdapter(config, sandbox_factory=create, start_gc=False)
    lease = None
    stage = "preset-injection-and-preflight"
    report = {
        "suite": "internal-openclaw-model-response",
        "result": "FAIL",
        "checks": {},
        "client_mode": "embedded OpenClaw CLI; not a gateway/TUI acceptance",
        "production_configuration_changed": False,
    }
    try:
        lease = adapter.acquire(
            {"runtime": "openclaw", "session_key": uuid.uuid4().hex, "profile": args.profile}
        )
        report["checks"][stage] = "PASS"
        stage = "openclaw-application-model-response"
        command = " ".join(
            [
                "env",
                "OPENCLAW_CONFIG_PATH=" + shlex.quote(args.config_path),
                "OPENCLAW_STATE_DIR=" + shlex.quote(args.state_dir),
                "openclaw agent --local --thinking off --timeout 55 --json --session-id",
                uuid.uuid4().hex,
                "--message",
                shlex.quote("Reply exactly OPENCLAW_INTERNAL_MODEL_OK. Do not call any tools."),
            ]
        )
        result = handles[0].commands.run(command, user="root", timeout=65)
        for name, content in (("stdout", result.stdout), ("stderr", result.stderr)):
            path = args.workdir / (name + "-private.log")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(content or "")
        payload = json.loads(result.stdout)
        assert result.exit_code == 0
        assert any(
            "OPENCLAW_INTERNAL_MODEL_OK" in item.get("text", "")
            for item in payload.get("payloads", [])
        )
        report["checks"][stage] = "PASS"
        report["reply"] = "OPENCLAW_INTERNAL_MODEL_OK"
        report["result"] = "PASS"
    except Exception as error:
        report["checks"][stage] = "FAIL"
        report["failure"] = {"stage": stage, "type": type(error).__name__}
    finally:
        try:
            if lease:
                adapter.release(lease["lease_ref"], {"action": "kill"})
            report["cleanup"] = "verified"
        except Exception:
            report["cleanup"] = "FAILED"
            report["result"] = "FAIL"
        adapter.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
