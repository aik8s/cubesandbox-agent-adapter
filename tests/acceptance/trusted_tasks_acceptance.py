#!/usr/bin/env python3
"""Run the trusted-task acceptance flow and render a redacted evidence report."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Check:
    name: str
    section: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)


class Acceptance:
    def __init__(self, base_url: str, agent: str, approver: str, dual: str, key: str):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self.approver = approver
        self.dual = dual
        self.key = key
        self.checks: list[Check] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}
        data = None
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=360) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                value = json.load(error)
            except Exception:
                value = {"error": {"code": "invalid_error_response"}}
            return error.code, value

    @staticmethod
    def error_code(value: dict[str, Any]) -> str:
        error = value.get("error", {})
        return str(error.get("code", "")) if isinstance(error, dict) else ""

    def expect(
        self,
        condition: bool,
        name: str,
        section: str,
        summary: str,
        **evidence: Any,
    ) -> None:
        if not condition:
            raise AssertionError(f"{name}: {summary}; evidence={evidence!r}")
        self.checks.append(Check(name, section, summary, evidence))

    def plan(
        self, template: str, parameters: dict[str, Any], *, token: str | None = None
    ) -> dict[str, Any]:
        status, value = self.request(
            "POST",
            "/v1/tasks/plan",
            token=token or self.agent,
            body={"template": template, "parameters": parameters},
        )
        if status != 201:
            raise AssertionError(f"plan {template} returned {status}: {value}")
        return value

    def approve(
        self, plan_ref: str, *, decision: str = "approve"
    ) -> dict[str, Any]:
        status, value = self.request(
            "POST",
            f"/v1/task-plans/{plan_ref}/approve",
            token=self.approver,
            body={"decision": decision, "reason": "acceptance-policy-check"},
        )
        if status != 200:
            raise AssertionError(f"approve {plan_ref} returned {status}: {value}")
        return value

    def submit(self, plan_ref: str) -> dict[str, Any]:
        status, value = self.request(
            "POST",
            f"/v1/task-plans/{plan_ref}/submit",
            token=self.agent,
            body={},
        )
        if status != 202:
            raise AssertionError(f"submit {plan_ref} returned {status}: {value}")
        return value

    def wait_terminal(self, task_ref: str, timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status, latest = self.request(
                "GET", f"/v1/tasks/{task_ref}/status", token=self.agent
            )
            if status != 200:
                raise AssertionError(f"status {task_ref} returned {status}: {latest}")
            if latest.get("state") not in {"starting", "running"}:
                return latest
            time.sleep(0.5)
        raise TimeoutError(f"task {task_ref} did not finish: {latest}")

    def finalize(self, task_ref: str) -> dict[str, Any]:
        status, value = self.request(
            "POST", f"/v1/tasks/{task_ref}/result", token=self.agent, body={}
        )
        if status != 200:
            raise AssertionError(f"result {task_ref} returned {status}: {value}")
        return value

    def run(self) -> dict[str, Any]:
        status, health = self.request("GET", "/healthz")
        self.expect(
            status == 200 and health.get("status") == "ok",
            "service-health",
            "Runtime",
            "Acceptance Adapter is healthy.",
            version=health.get("version"),
        )

        status, catalog = self.request(
            "GET", "/v1/task-templates", token=self.agent
        )
        names = sorted(item["name"] for item in catalog.get("task_templates", []))
        self.expect(
            status == 200 and len(names) == 5,
            "template-catalog",
            "Runtime",
            "Operator-owned task catalog loaded with signed template digests.",
            templates=names,
        )

        status, denied = self.request(
            "POST",
            "/v1/leases/acquire",
            token=self.agent,
            body={
                "runtime": "mcp",
                "session_key": "must-not-run",
                "profile": "trusted-acceptance",
            },
        )
        self.expect(
            status == 403 and self.error_code(denied) == "action_denied",
            "raw-exec-denied",
            "Trust boundary",
            "Task-only identity cannot acquire a raw execution lease.",
            http_status=status,
            error_code=self.error_code(denied),
        )

        status, invalid = self.request(
            "POST",
            "/v1/tasks/plan",
            token=self.agent,
            body={
                "template": "trusted-smoke",
                "parameters": {"run_id": "acceptance-schema", "epochs": 0},
            },
        )
        self.expect(
            status == 400 and self.error_code(invalid) == "invalid_parameters",
            "input-schema-denied",
            "Trust boundary",
            "JSON Schema rejects parameters outside the operator contract.",
            http_status=status,
            error_code=self.error_code(invalid),
        )

        dual_plan = self.plan(
            "trusted-smoke",
            {"run_id": "acceptance-self-approval", "epochs": 5},
            token=self.dual,
        )
        status, self_denied = self.request(
            "POST",
            f"/v1/task-plans/{dual_plan['plan_ref']}/approve",
            token=self.dual,
            body={"decision": "approve"},
        )
        self.expect(
            status == 403 and self.error_code(self_denied) == "self_approval_denied",
            "self-approval-denied",
            "Trust boundary",
            "A dual-role identity still cannot approve its own plan.",
            http_status=status,
            error_code=self.error_code(self_denied),
        )

        run_id = "acceptance-real-microvm"
        plan = self.plan("trusted-smoke", {"run_id": run_id, "epochs": 25})
        status, review = self.request(
            "GET",
            f"/v1/task-plans/{plan['plan_ref']}",
            token=self.approver,
        )
        self.expect(
            status == 200
            and review.get("parameters", {}).get("epochs") == 25
            and review.get("command_sha256") == plan.get("command_sha256"),
            "approval-review",
            "Plan and approval",
            "Independent approver sees parameters bound to immutable hashes.",
            plan_ref=plan["plan_ref"],
            template_sha256=str(plan["template_sha256"])[:16],
            parameters_sha256=str(plan["parameters_sha256"])[:16],
            command_sha256=str(plan["command_sha256"])[:16],
        )
        approved = self.approve(plan["plan_ref"])
        self.expect(
            approved.get("state") == "approved",
            "independent-approval",
            "Plan and approval",
            "Independent approver authorizes the exact reviewed plan.",
            state=approved.get("state"),
        )

        submitted = self.submit(plan["plan_ref"])
        repeated = self.submit(plan["plan_ref"])
        self.expect(
            submitted.get("task_ref") == repeated.get("task_ref"),
            "idempotent-submit",
            "Plan and approval",
            "Repeated submission returns the original task instead of executing twice.",
            task_ref=submitted.get("task_ref"),
        )
        task_ref = str(submitted["task_ref"])
        status, early_receipt = self.request(
            "GET", f"/v1/tasks/{task_ref}/receipt", token=self.agent
        )
        self.expect(
            status == 409 and self.error_code(early_receipt) == "receipt_not_ready",
            "receipt-gated",
            "Execution",
            "Receipt is unavailable until execution and sandbox cleanup finish.",
            http_status=status,
            error_code=self.error_code(early_receipt),
        )
        first_status, running = self.request(
            "GET", f"/v1/tasks/{task_ref}/status", token=self.agent
        )
        self.expect(
            first_status == 200 and running.get("state") in {"running", "succeeded"},
            "task-started",
            "Execution",
            "Task was scheduled into a real CubeSandbox MicroVM.",
            task_ref=task_ref,
            state=running.get("state"),
        )
        self.wait_terminal(task_ref)
        result = self.finalize(task_ref)
        outputs = result.get("result", {}).get("outputs", {})
        evidence = result.get("result", {}).get("output_evidence", [])
        model_evidence = next(item for item in evidence if item.get("name") == "model")
        self.expect(
            result.get("state") == "succeeded"
            and outputs.get("metrics", {}).get("score") == 0.98
            and "model" not in outputs
            and model_evidence.get("exposed") is False,
            "output-policy",
            "Execution",
            "Allowlisted metrics are returned while model content remains digest-only.",
            sandbox_ref=result.get("result", {}).get("sandbox_ref"),
            metrics=outputs.get("metrics"),
            model_sha256=str(model_evidence.get("sha256", ""))[:16],
        )
        self.expect(
            result.get("result", {}).get("cleanup") == "verified",
            "cleanup-verified",
            "Execution",
            "The MicroVM was killed before a final receipt was issued.",
            cleanup=result.get("result", {}).get("cleanup"),
            state=result.get("state"),
        )

        status, receipt_value = self.request(
            "GET", f"/v1/tasks/{task_ref}/receipt", token=self.agent
        )
        receipt = receipt_value.get("receipt", {})
        payload = receipt.get("payload", {})
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected = base64.urlsafe_b64encode(
            hmac.new(self.key.encode("utf-8"), encoded, hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        signature = receipt.get("signature", {})
        self.expect(
            status == 200
            and signature.get("alg") == "HS256"
            and hmac.compare_digest(str(signature.get("value", "")), expected)
            and payload.get("cleanup") == "verified",
            "receipt-signature",
            "Signed receipt",
            "Canonical receipt signature verifies offline and includes cleanup evidence.",
            algorithm=signature.get("alg"),
            key_id=signature.get("kid"),
            signature_prefix=str(signature.get("value", ""))[:18],
            cleanup=payload.get("cleanup"),
        )
        status, receipt_post = self.request(
            "POST", f"/v1/tasks/{task_ref}/receipt", token=self.agent, body={}
        )
        self.expect(
            status == 200
            and receipt_post.get("receipt", {}).get("signature") == signature,
            "receipt-get-post",
            "Signed receipt",
            "GET and POST receipt transports return the same signed evidence.",
            task_ref=task_ref,
        )

        denied_plan = self.plan(
            "trusted-smoke", {"run_id": "acceptance-denied", "epochs": 5}
        )
        denied_plan = self.approve(denied_plan["plan_ref"], decision="deny")
        status, denied_submit = self.request(
            "POST",
            f"/v1/task-plans/{denied_plan['plan_ref']}/submit",
            token=self.agent,
            body={},
        )
        self.expect(
            denied_plan.get("state") == "denied"
            and status == 409
            and self.error_code(denied_submit) == "plan_not_approved",
            "denied-plan-blocked",
            "Failure handling",
            "A denied plan cannot be submitted.",
            state=denied_plan.get("state"),
            error_code=self.error_code(denied_submit),
        )

        expiring = self.plan("expiring-plan", {})
        time.sleep(1.2)
        status, expired = self.request(
            "GET",
            f"/v1/task-plans/{expiring['plan_ref']}",
            token=self.approver,
        )
        self.expect(
            status == 409 and self.error_code(expired) == "plan_expired",
            "expired-plan-blocked",
            "Failure handling",
            "Expired plans cannot be approved or replayed.",
            http_status=status,
            error_code=self.error_code(expired),
        )

        invalid_plan = self.plan(
            "invalid-output", {"run_id": "acceptance-invalid-output"}
        )
        self.approve(invalid_plan["plan_ref"])
        invalid_task = self.submit(invalid_plan["plan_ref"])
        self.wait_terminal(str(invalid_task["task_ref"]))
        invalid_result = self.finalize(str(invalid_task["task_ref"]))
        self.expect(
            invalid_result.get("state") == "output_validation_failed"
            and invalid_result.get("result", {}).get("cleanup") == "verified",
            "invalid-output-blocked",
            "Failure handling",
            "Invalid task output fails closed, but still produces cleanup evidence.",
            state=invalid_result.get("state"),
            cleanup=invalid_result.get("result", {}).get("cleanup"),
        )

        cancel_plan = self.plan("trusted-cancel", {"wait_seconds": 60})
        self.approve(cancel_plan["plan_ref"])
        cancel_task = self.submit(cancel_plan["plan_ref"])
        status, cancelled = self.request(
            "POST",
            f"/v1/tasks/{cancel_task['task_ref']}/cancel",
            token=self.agent,
            body={},
        )
        self.expect(
            status == 200
            and cancelled.get("state") == "cancelled"
            and cancelled.get("result", {}).get("cleanup") == "verified",
            "cancel-and-cleanup",
            "Failure handling",
            "Cancellation terminates the job and verifies MicroVM cleanup.",
            state=cancelled.get("state"),
            cleanup=cancelled.get("result", {}).get("cleanup"),
        )

        auto_plan = self.plan("trusted-auto", {"message": "safe-auto-run"})
        auto_task = self.submit(auto_plan["plan_ref"])
        self.wait_terminal(str(auto_task["task_ref"]))
        auto_result = self.finalize(str(auto_task["task_ref"]))
        self.expect(
            auto_plan.get("state") == "ready"
            and auto_result.get("state") == "succeeded",
            "operator-auto-task",
            "Failure handling",
            "An explicitly configured no-approval template follows its declared policy.",
            plan_state=auto_plan.get("state"),
            result_state=auto_result.get("state"),
        )

        return {
            "title": "CubeSandbox Trusted Execution Acceptance",
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "environment": "sr1 · isolated acceptance namespace · real CubeSandbox",
            "result": "PASS",
            "passed": len(self.checks),
            "failed": 0,
            "checks": [check.__dict__ for check in self.checks],
            "receipt": {
                "task_ref": task_ref,
                "state": payload.get("state"),
                "cleanup": payload.get("cleanup"),
                "algorithm": signature.get("alg"),
                "key_id": signature.get("kid"),
                "template_sha256": str(payload.get("template_sha256", ""))[:20],
                "parameters_sha256": str(payload.get("parameters_sha256", ""))[:20],
                "command_sha256": str(payload.get("command_sha256", ""))[:20],
                "signature_prefix": str(signature.get("value", ""))[:20],
            },
        }


def render_report(result: dict[str, Any]) -> str:
    sections: dict[str, list[dict[str, Any]]] = {}
    for check in result["checks"]:
        sections.setdefault(check["section"], []).append(check)

    nav = "".join(
        f'<a href="#{html.escape(section.lower().replace(" ", "-"))}">{html.escape(section)}</a>'
        for section in sections
    )
    section_html = []
    for section, checks in sections.items():
        cards = []
        for check in checks:
            evidence = "".join(
                "<div class=datum><span>"
                + html.escape(str(key).replace("_", " "))
                + "</span><code>"
                + html.escape(
                    json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
                + "</code></div>"
                for key, value in check["evidence"].items()
            )
            cards.append(
                '<article class="check"><div class="check-head">'
                '<span class="pass">PASS</span>'
                f'<h3>{html.escape(check["name"])}</h3></div>'
                f'<p>{html.escape(check["summary"])}</p>'
                f'<div class="evidence">{evidence}</div></article>'
            )
        anchor = section.lower().replace(" ", "-")
        section_html.append(
            f'<section id="{html.escape(anchor)}"><div class="section-title">'
            f'<div><span class="eyebrow">VERIFIED CAPABILITY</span><h2>{html.escape(section)}</h2></div>'
            f'<span class="count">{len(checks)} checks</span></div>'
            f'<div class="grid">{"".join(cards)}</div></section>'
        )

    receipt = result["receipt"]
    receipt_rows = "".join(
        f"<tr><th>{html.escape(key.replace('_', ' '))}</th><td><code>{html.escape(str(value))}</code></td></tr>"
        for key, value in receipt.items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(result['title'])}</title>
<style>
:root{{color-scheme:light;--ink:#172033;--muted:#667085;--line:#dfe5ef;--blue:#2457d6;--green:#147a4b;--green-bg:#e9f8f0;--panel:#fff;--bg:#f4f7fb}}
*{{box-sizing:border-box}}html{{scroll-behavior:auto}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
.top{{background:linear-gradient(135deg,#fff 0%,#eef4ff 100%);border-bottom:1px solid var(--line)}}
.wrap{{width:min(1180px,calc(100% - 48px));margin:auto}}header{{padding:50px 0 36px}}.brand{{color:var(--blue);font-weight:800;letter-spacing:.08em;text-transform:uppercase;font-size:12px}}
h1{{font-size:42px;line-height:1.12;margin:12px 0 12px;letter-spacing:-.035em}}header p{{color:var(--muted);font-size:17px;margin:0}}
.summary{{display:flex;gap:14px;margin-top:28px;flex-wrap:wrap}}.badge{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 16px;box-shadow:0 3px 14px #23345a0a}}
.badge strong{{display:block;font-size:20px}}.badge span{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
nav{{display:flex;gap:8px;padding:14px 0;overflow:auto}}nav a{{white-space:nowrap;color:#344054;text-decoration:none;background:#fff;border:1px solid var(--line);border-radius:999px;padding:7px 12px;font-size:13px}}
main{{padding:34px 0 64px}}section{{margin:0 0 44px;scroll-margin-top:12px}}.section-title{{display:flex;align-items:end;justify-content:space-between;margin-bottom:14px}}.eyebrow{{font-size:11px;color:var(--blue);letter-spacing:.09em;font-weight:800}}h2{{font-size:27px;margin:3px 0 0;letter-spacing:-.02em}}.count{{color:var(--muted);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.check{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 6px 20px #23345a0a;min-height:178px}}
.check-head{{display:flex;align-items:center;gap:10px}}.check h3{{font-size:17px;margin:0}}.pass{{background:var(--green-bg);color:var(--green);font-weight:800;font-size:11px;padding:4px 8px;border-radius:999px}}.check p{{color:#475467;margin:10px 0 14px}}
.evidence{{border-top:1px solid #edf0f5;padding-top:10px}}.datum{{display:flex;justify-content:space-between;gap:16px;margin:6px 0;font-size:12px}}.datum span{{color:var(--muted);text-transform:capitalize}}code{{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:#243b72;overflow-wrap:anywhere;text-align:right}}
.receipt{{background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 14px 32px #23345a12}}.receipt h2{{margin-bottom:14px}}table{{width:100%;border-collapse:collapse}}th,td{{border-top:1px solid #edf0f5;padding:9px 0;text-align:left}}th{{width:220px;color:var(--muted);font-size:12px;text-transform:capitalize}}.receipt code{{color:#243b72}}
footer{{padding:24px 0 40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}}
@media(max-width:760px){{.wrap{{width:min(100% - 28px,1180px)}}h1{{font-size:32px}}.grid{{grid-template-columns:1fr}}.datum{{display:block}}code{{display:block;text-align:left;margin-top:2px}}}}
</style></head>
<body><div class="top"><div class="wrap"><header><div class="brand">CubeSandbox · Trusted Execution</div>
<h1>{html.escape(result['title'])}</h1><p>{html.escape(result['environment'])}</p>
<div class="summary"><div class="badge"><strong>{result['result']}</strong><span>overall result</span></div>
<div class="badge"><strong>{result['passed']}</strong><span>checks passed</span></div><div class="badge"><strong>{result['failed']}</strong><span>checks failed</span></div>
<div class="badge"><strong>Light</strong><span>evidence theme</span></div></div></header><nav>{nav}</nav></div></div>
<main class="wrap">{''.join(section_html)}<section id="receipt"><div class="receipt"><span class="eyebrow">OFFLINE VERIFIED</span><h2>Signed execution receipt</h2><table>{receipt_rows}</table></div></section></main>
<footer><div class="wrap">Generated {html.escape(result['generated_at'])}. Real execution evidence; tokens, addresses, full internal identifiers, commands and task data are excluded.</div></footer>
</body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:19080")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    secrets = {
        name: os.environ.get(env, "")
        for name, env in {
            "agent": "CUBE_ACCEPT_AGENT_TOKEN",
            "approver": "CUBE_ACCEPT_APPROVER_TOKEN",
            "dual": "CUBE_ACCEPT_DUAL_TOKEN",
            "key": "CUBE_ACCEPT_RECEIPT_KEY",
        }.items()
    }
    missing = [name for name, value in secrets.items() if not value]
    if missing:
        raise SystemExit("missing acceptance credentials: " + ", ".join(missing))
    acceptance = Acceptance(args.base_url, **secrets)
    result = acceptance.run()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "report.html").write_text(
        render_report(result), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "result": result["result"],
                "passed": result["passed"],
                "failed": result["failed"],
                "output_dir": str(args.output_dir),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
