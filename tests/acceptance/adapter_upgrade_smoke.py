#!/usr/bin/env python3
"""Run in the upgraded Adapter Pod; credentials never leave the process.

Creates and deletes one uniquely named test lease. Does not change policy,
storage or network settings. Optional audit snapshot verification is read-only.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sqlite3
import uuid
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from adapter.audit_admin import verify_chain


def main():
    token = os.environ["CUBE_ADAPTER_TOKEN"]
    base = "http://127.0.0.1:" + os.environ.get("CUBE_ADAPTER_PORT", "18080")
    profile = os.environ.get("CUBE_UPGRADE_TEST_PROFILE", "offline-code")
    report = {"result": "FAIL", "checks": {}, "production_settings_changed": ["image only"]}
    lease = None
    stage = "readiness"

    def request(path, body=None, expected=200):
        raw = json.dumps(body).encode() if body is not None else None
        req = Request(base + path, data=raw, headers={"Authorization": "Bearer " + token,
                                                     "Content-Type": "application/json"})
        try:
            response = urlopen(req, timeout=90)
        except HTTPError as error:
            response = error
        with response:
            value = json.load(response)
            if response.status != expected:
                raise RuntimeError("unexpected status " + str(response.status))
        return value

    try:
        report["sdk"] = importlib.metadata.version("cubesandbox")
        assert report["sdk"] == "0.7.3"
        assert request("/readyz")["status"] == "ready"
        report["checks"][stage] = "PASS"
        stage = "profile-discovery"
        profiles = request("/v1/profiles", {})["profiles"]
        approved = next(item for item in profiles if item["name"] == profile)
        report["features_enabled"] = {"mcp": approved["mcp_enabled"],
                                      "keepalive": approved["keepalive_enabled"],
                                      "console": os.environ.get("CUBE_ADAPTER_AUDIT_UI") == "1",
                                      "postgresql": bool(os.environ.get("CUBE_ADAPTER_AUDIT_DATABASE_URL"))}
        report["checks"][stage] = "PASS"
        stage = "create-execute-status"
        lease = request("/v1/leases/acquire", {"runtime": "mcp", "profile": profile,
                                              "session_key": "upgrade-probe-" + uuid.uuid4().hex})["lease_ref"]
        result = request("/v1/leases/" + lease + "/exec", {"command": "printf ADAPTER_UPGRADE_OK"})
        assert result["exit_code"] == 0 and result["stdout"] == "ADAPTER_UPGRADE_OK"
        assert request("/v1/leases/" + lease + "/status", {"refresh": True})["state"] == "running"
        report["checks"][stage] = "PASS"
        stage = "disabled-features-remain-denied"
        if not approved["mcp_enabled"]:
            assert request("/v1/leases/" + lease + "/mcp-list", {}, expected=403)["error"]["code"] == "mcp_denied"
        if not approved["keepalive_enabled"]:
            assert request("/v1/leases/" + lease + "/renew", {}, expected=403)["error"]["code"] == "renew_denied"
        report["checks"][stage] = "PASS"
        stage = "durable-history-query"
        audit = request("/v1/audit/query", {"lease_ref": lease, "limit": 100})
        assert audit["durable"] and any(item["action"] == "exec" for item in audit["events"])
        report["checks"][stage] = "PASS"
        report["result"] = "PASS"
    except Exception as error:
        report["failure"] = {"stage": stage, "type": type(error).__name__}
    finally:
        if lease:
            try:
                request("/v1/leases/" + lease + "/release", {"action": "kill"})
                report["test_sandbox_deleted"] = True
            except Exception as error:
                report["test_sandbox_deleted"] = False
                report["cleanup_error"] = type(error).__name__
                report["result"] = "FAIL"
        # Use a read-only SQLite connection, not a second AuditJournal writer.
        snapshot = os.environ.get("CUBE_UPGRADE_AUDIT_SNAPSHOT")
        if snapshot:
            try:
                source = os.environ["CUBE_ADAPTER_AUDIT_LOG"] + ".sqlite3"
                with sqlite3.connect("file:" + snapshot + "?mode=ro", uri=True, timeout=15) as old:
                    maximum = old.execute("SELECT max(seq) FROM events").fetchone()[0] or 0

                    def digest(db):
                        count, sha = 0, hashlib.sha256()
                        for row in db.execute("SELECT seq,event_id,payload FROM events WHERE seq<=? ORDER BY seq", (maximum,)):
                            sha.update(json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode())
                            count += 1
                        return count, sha.hexdigest()

                    before = digest(old)
                with sqlite3.connect("file:" + source + "?mode=ro", uri=True, timeout=15) as current:
                    current.execute("BEGIN")
                    after = digest(current)
                    assert before == after
                    assert current.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    assert current.execute("SELECT count(*) FROM operations WHERE completed=0").fetchone()[0] == 0
                    chain = verify_chain(SimpleNamespace(db=current))
                report["audit_preserved"] = {"historical_events": before[0], "original_rows_sha256": before[1],
                                           "historical_rows_unchanged": True, "quick_check": "ok",
                                           "incomplete_operations": 0, "chain": chain}
            except Exception as error:
                report["audit_verification_error"] = type(error).__name__
                report["result"] = "FAIL"
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
