#!/usr/bin/env python3
"""Merge live client results into the redacted acceptance evidence report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trusted_tasks_acceptance import public_ref, render_report

V071_SUMMARIES = {
    "mounted_snapshot": "A running sandbox with a mounted S3 volume created a snapshot.",
    "rootfs_rollback": "Rollback restored the root filesystem to the snapshot state.",
    "external_volume_rollback": "Rollback kept mounted external data at its current state.",
    "clone_rootfs": "The clone inherited the restored root filesystem state.",
    "clone_external_volume": "The clone remounted and shared current external data.",
    "snapshot_delete_while_referenced": (
        "The referenced snapshot accepted deletion under v0.7.1 lifecycle semantics."
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    result_path = args.output_dir / "results.json"
    result: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))
    result["checks"] = [
        check
        for check in result["checks"]
        if check["section"] not in {"Agent clients", "CubeSandbox v0.7.1"}
    ]
    for name in ("clients-node.json", "clients-python.json"):
        client_group = json.loads((args.output_dir / name).read_text(encoding="utf-8"))
        if client_group.get("result") != "PASS":
            raise RuntimeError(f"client result did not pass: {name}")
        for client in client_group["clients"]:
            if (
                client.get("state") != "succeeded"
                or client.get("cleanup") != "verified"
                or client.get("receipt_alg") != "HS256"
            ):
                raise RuntimeError(f"invalid client evidence: {client!r}")
            result["checks"].append(
                {
                    "name": client["client"],
                    "section": "Agent clients",
                    "summary": (
                        "Client plugin completed plan, submit, status, result and "
                        "receipt against the live Adapter."
                    ),
                    "evidence": {
                        "registered_tools": client_group["tools_per_client"],
                        "trusted_task_tools": client_group[
                            "trusted_task_tools_per_client"
                        ],
                        "task_ref": public_ref("task", client["task_ref"]),
                        "state": client["state"],
                        "cleanup": client["cleanup"],
                        "receipt_algorithm": client["receipt_alg"],
                    },
                }
            )
    v071_path = args.output_dir / "cubesandbox-v071.json"
    if v071_path.exists():
        v071 = json.loads(v071_path.read_text(encoding="utf-8"))
        if v071.get("result") != "PASS" or not all(v071.get("cleanup", {}).values()):
            raise RuntimeError("CubeSandbox v0.7.1 acceptance did not pass cleanly")
        for name, summary in V071_SUMMARIES.items():
            outcome = v071.get("checks", {}).get(name)
            if outcome is None:
                raise RuntimeError(f"missing CubeSandbox v0.7.1 check: {name}")
            result["checks"].append(
                {
                    "name": name.replace("_", "-"),
                    "section": "CubeSandbox v0.7.1",
                    "summary": summary,
                    "evidence": {
                        "backend": v071.get("backend"),
                        "sdk": v071.get("sdk"),
                        "outcome": outcome,
                    },
                }
            )
        result["checks"].append(
            {
                "name": "backend-resource-cleanup",
                "section": "CubeSandbox v0.7.1",
                "summary": "Every temporary sandbox, snapshot and volume was removed.",
                "evidence": {"cleanup": "verified", "resources": len(v071["cleanup"])},
            }
        )
    result["passed"] = len(result["checks"])
    result["failed"] = 0
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "report.html").write_text(
        render_report(result), encoding="utf-8"
    )
    print(json.dumps({"result": "PASS", "passed": result["passed"]}))


if __name__ == "__main__":
    main()
