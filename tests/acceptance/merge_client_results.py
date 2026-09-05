#!/usr/bin/env python3
"""Merge live client results into the redacted acceptance evidence report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trusted_tasks_acceptance import render_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    result_path = args.output_dir / "results.json"
    result: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))
    result["checks"] = [
        check for check in result["checks"] if check["section"] != "Agent clients"
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
                        "task_ref": client["task_ref"],
                        "state": client["state"],
                        "cleanup": client["cleanup"],
                        "receipt_algorithm": client["receipt_alg"],
                    },
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
