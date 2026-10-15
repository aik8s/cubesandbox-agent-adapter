#!/usr/bin/env python3
"""Capture the live Light console and verify independent approval/export.

Requires the private workdir from production_features_live.py --hold.
Credentials are read only from that private workdir, never written to evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--browser-executable", type=Path, help="Reuse an installed browser; do not download another one")
    args = parser.parse_args()
    auth = json.loads((args.workdir / "session-private.json").read_text())["auth"]
    url = "http://127.0.0.1:34881"
    response = requests.post(url + "/v1/tasks/plan", headers={"Authorization": "Bearer " + auth["runtime"]},
                             json={"template": "approved-demo", "parameters": {"message": "PIPELINE_OK"}}, timeout=30)
    response.raise_for_status()
    plan = response.json()["plan_ref"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=str(args.browser_executable) if args.browser_executable else None)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, color_scheme="light", accept_downloads=True)
        page = context.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        response = page.goto(url + "/console")
        assert "sha256-" in response.headers["content-security-policy"]
        page.locator("#token").fill(auth["approver"])
        page.locator("#connect").click()
        row = page.locator("#plan-table tbody tr").filter(has_text=plan)
        row.wait_for()
        assert page.locator("#token").input_value() == ""
        assert page.evaluate("localStorage.length===0 && sessionStorage.length===0")
        page.locator("section").nth(1).screenshot(path=str(args.output_dir / "01-console-profiles-light.png"))
        row.click()
        page.locator("#approve").wait_for(state="visible")
        assert "PIPELINE_OK" in page.locator("#plan-detail").inner_text()
        page.locator("section").nth(2).screenshot(path=str(args.output_dir / "02-console-approval-light.png"))
        page.locator("#approve").click()
        expect(page.locator("#message")).to_have_text("审批已记录")
        expect(page.locator("#plan-table tbody tr").filter(has_text=plan)).to_contain_text("approved")
        page.locator("#action").fill("task_approve")
        page.locator("#query").click()
        expect(page.locator("#events tbody tr").first.locator("td").nth(3)).to_have_text("task_approve")
        assert all("task_approve" in value for value in page.locator("#events tbody tr").all_inner_texts())
        page.locator("#events tbody tr").last.click()
        page.locator("section").nth(3).screenshot(path=str(args.output_dir / "03-console-audit-light.png"))
        with page.expect_download() as download:
            page.locator("#export").click()
        path = args.workdir / "console-page.jsonl"
        download.value.save_as(path)
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert events and all(event["action"] == "task_approve" for event in events)
        assert all("tenant_hash" in event for event in events)
        assert not page_errors
        # Token must not remain in the visible document or saved screenshots.
        assert all(token not in page.locator("body").inner_text() for token in auth.values())
        browser.close()
    report = {"result": "PASS", "real_ui": True, "color_scheme": "light", "independent_approval": True,
              "exported_events": len(events), "token_browser_storage": False, "csp": "inline script SHA256 + same-origin data requests"}
    (args.output_dir / "console-results-2026-10-14.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
