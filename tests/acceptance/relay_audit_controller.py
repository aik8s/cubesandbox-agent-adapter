#!/usr/bin/env python3
"""Run on the relay as apps; only controls labeled, task-owned test containers."""
# Fixed Docker commands and task-owned paths; SQL schema is regex-validated.
# ruff: noqa: S603, S607, S608

import json
import os
import pathlib
import re
import subprocess
import time

PG = "cube-adapter-next-pg-20261014"
WORKER = "cube-adapter-drill-worker-20261015-r2"
ROOT = "/tmp/cube-adapter-relay-drill-20261015-r2"
IMAGE = os.environ["CUBE_ADAPTER_CANDIDATE_IMAGE"]


def cmd(*args):
    result = subprocess.run(
        ["sudo", "-n", "docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45
    )  # noqa: S603, S607 - fixed test command/validated identifiers
    if result.returncode:
        raise RuntimeError("test Docker command failed: " + result.stderr.decode()[-180:])
    return result.stdout.decode().strip()


def wait_file(name):
    for _ in range(120):
        result = subprocess.run(
            ["sudo", "-n", "test", "-f", ROOT + "/" + name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )  # noqa: S603, S607 - fixed task-owned evidence files
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(name)


def read_evidence(name):
    result = subprocess.run(
        ["sudo", "-n", "cat", ROOT + "/" + name], check=True, stdout=subprocess.PIPE
    )  # noqa: S603, S607 - fixed task-owned evidence files
    return json.loads(result.stdout.decode())


def main():
    if (
        cmd("inspect", "--format", '{{index .Config.Labels "cube-adapter-task"}}', PG)
        != "next-20261014"
    ):
        raise RuntimeError("refusing a non-test database container")
    subprocess.run(
        ["sudo", "-n", "install", "-d", "-o", "65532", "-g", "65532", "-m", "0700", ROOT],
        check=True,
    )  # noqa: S603, S607 - fixed task-owned directory
    cmd(
        "run",
        "--pull=never",
        "-d",
        "--name",
        WORKER,
        "--label",
        "cube-adapter-task=next-20261014",
        "--network=host",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m,uid=65532,gid=65532",
        "--cpus=1",
        "--memory=512m",
        "-v",
        ROOT + ":/evidence",
        "-v",
        "/tmp/cube-adapter-relay-source-20261015/adapter:/app/adapter:ro",
        "-v",
        "/tmp/relay_audit_worker.py:/app/drill.py:ro",
        "--entrypoint",
        "python",
        IMAGE,
        "/app/drill.py",
    )
    stopped = False
    try:
        wait_file("ready.json")
        cmd("stop", "-t", "5", PG)
        stopped = True
        cmd("exec", WORKER, "touch", "/evidence/check-offline")
        wait_file("offline-checked.json")
        cmd("start", PG)
        stopped = False
        time.sleep(2)
        cmd("exec", WORKER, "touch", "/evidence/database-restarted")
        wait_file("worker-results.json")
        report = read_evidence("worker-results.json")
        if report["result"] != "PASS":
            raise RuntimeError("relay worker failed: " + json.dumps(report))
        private = read_evidence("resources-private.json")
        schema = private["schema"]
        if not re.fullmatch(r"relay_fault_[a-f0-9]{12}", schema):
            raise RuntimeError("refusing non-test schema")
        backup = "/tmp/cube-adapter-audit-20261015.dump"
        restore = "cube_adapter_restore_" + schema[len("relay_fault_") :]
        cmd(
            "exec",
            PG,
            "pg_dump",
            "-U",
            "postgres",
            "-d",
            "cube_adapter_test",
            "-n",
            schema,
            "-Fc",
            "-f",
            backup,
        )
        cmd("exec", PG, "chmod", "600", backup)
        cmd("exec", PG, "createdb", "-U", "postgres", restore)
        cmd(
            "exec",
            PG,
            "pg_restore",
            "-U",
            "postgres",
            "--no-owner",
            "--no-acl",
            "-d",
            restore,
            backup,
        )
        count = int(
            cmd(
                "exec",
                PG,
                "psql",
                "-U",
                "postgres",
                "-d",
                restore,
                "-tA",
                "-c",
                'SELECT count(*) FROM "' + schema + '".events',
            )
        )  # noqa: S608 - schema regex validated above
        tip = cmd(
            "exec",
            PG,
            "psql",
            "-U",
            "postgres",
            "-d",
            restore,
            "-tA",
            "-c",
            'SELECT chain_tip FROM "' + schema + '".metadata',
        )  # noqa: S608 - schema regex validated above
        assert count == report["events"] and tip == report["chain_tip"]
        report["checks"]["pg_dump_restore_into_separate_database"] = "PASS"
        report["restore"] = {
            "source_events": report["events"],
            "restored_events": count,
            "chain_tip_matches": True,
            "source_overwritten": False,
        }
        pathlib.Path("/tmp/relay-audit-drill-results-20261015.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    finally:
        if stopped:
            cmd("start", PG)
        cmd("stop", "-t", "5", WORKER)


if __name__ == "__main__":
    main()
