#!/usr/bin/env python3
"""Exercise CubeSandbox v0.7.1 external-reference snapshot semantics.

Run this inside an Adapter Pod or another environment that already has the
CubeSandbox SDK and connection variables. The script emits only a redacted
summary; backend object identifiers and credentials are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from importlib.metadata import version
from typing import Any

from cubesandbox import Sandbox, Volume


def run(sandbox: Sandbox, command: str) -> str:
    result = sandbox.commands.run(command, timeout=60)
    if result.exit_code != 0:
        raise RuntimeError("sandbox command failed during v0.7.1 acceptance")
    return (result.stdout or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=str)
    args = parser.parse_args()
    template = os.environ.get("CUBE_TEMPLATE_ID", "agent-code")
    volume = None
    source = None
    writer = None
    verifier = None
    clone = None
    snapshot_id = ""
    snapshot_deleted = False
    cleanup: dict[str, bool] = {
        "clone_killed": False,
        "source_killed": False,
        "writer_killed": False,
        "verifier_killed": False,
        "snapshot_deleted": False,
        "volume_destroyed": False,
    }
    try:
        volume = Volume.create(f"v071-acceptance-{uuid.uuid4().hex[:16]}", driver="s3")
        source = Sandbox.create(
            template=template,
            timeout=300,
            allow_internet_access=False,
            volume_mounts={"/workspace": volume},
        )
        run(
            source,
            "printf root-before > /tmp/v071-root && "
            "printf volume-before > /workspace/v071-external && sync",
        )
        snapshot = source.create_snapshot(name="v071-external-reference")
        snapshot_id = snapshot.snapshot_id

        run(source, "printf root-after > /tmp/v071-root")
        writer = Sandbox.create(
            template=template,
            timeout=300,
            allow_internet_access=False,
            volume_mounts={"/workspace": volume},
        )
        if run(writer, "cat /workspace/v071-external") != "volume-before":
            raise AssertionError("writer did not see the source volume state")
        run(writer, "printf volume-after > /workspace/v071-created-after && sync")
        writer.kill()
        writer = None
        cleanup["writer_killed"] = True
        time.sleep(2)

        verifier = Sandbox.create(
            template=template,
            timeout=300,
            allow_internet_access=False,
            volume_mounts={"/workspace": volume},
        )
        if run(verifier, "cat /workspace/v071-created-after") != "volume-after":
            raise AssertionError("independent verifier did not see current volume data")
        verifier.kill()
        verifier = None
        cleanup["verifier_killed"] = True
        source.rollback(snapshot_id)
        root_after_rollback = run(source, "cat /tmp/v071-root")
        volume_after_rollback = run(source, "cat /workspace/v071-created-after")
        if root_after_rollback != "root-before":
            raise AssertionError("rootfs did not return to the checkpoint state")
        if volume_after_rollback != "volume-after":
            raise AssertionError("mounted data unexpectedly rolled back with rootfs")

        clones = source.clone(n=1)
        if len(clones) != 1:
            raise AssertionError("clone did not return exactly one sandbox")
        clone = clones[0]
        if run(clone, "cat /tmp/v071-root") != "root-before":
            raise AssertionError("clone did not inherit the restored rootfs state")
        if run(clone, "cat /workspace/v071-created-after") != "volume-after":
            raise AssertionError("clone did not remount current external data")

        run(clone, "printf volume-from-clone > /workspace/v071-from-clone && sync")
        if run(source, "cat /workspace/v071-from-clone") != "volume-from-clone":
            raise AssertionError("clone and source do not share the external volume")

        Sandbox.delete_snapshot(snapshot_id)
        snapshot_deleted = True
        cleanup["snapshot_deleted"] = True

        result: dict[str, Any] = {
            "result": "PASS",
            "backend": "CubeSandbox v0.7.1",
            "sdk": version("cubesandbox"),
            "checks": {
                "mounted_snapshot": "created",
                "rootfs_rollback": "restored",
                "external_volume_rollback": "kept-current",
                "clone_rootfs": "isolated",
                "clone_external_volume": "shared-current-data",
                "snapshot_delete_while_referenced": "accepted",
            },
        }
    finally:
        if verifier is not None:
            verifier.kill()
            cleanup["verifier_killed"] = True
        if writer is not None:
            writer.kill()
            cleanup["writer_killed"] = True
        if clone is not None:
            clone.kill()
            cleanup["clone_killed"] = True
        if source is not None:
            source.kill()
            cleanup["source_killed"] = True
        if snapshot_id and not snapshot_deleted:
            Sandbox.delete_snapshot(snapshot_id)
            cleanup["snapshot_deleted"] = True
        if volume is not None:
            cleanup["volume_destroyed"] = Volume.destroy(volume.volume_id)

    if not all(cleanup.values()):
        raise AssertionError("v0.7.1 acceptance cleanup was incomplete")
    result["cleanup"] = cleanup
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as output_file:
            output_file.write(serialized)
    print(serialized, end="")


if __name__ == "__main__":
    main()
