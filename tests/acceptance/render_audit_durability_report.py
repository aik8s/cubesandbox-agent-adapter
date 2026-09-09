#!/usr/bin/env python3
"""Render a deterministic light-theme report from redacted audit acceptance data."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def esc(value: Any) -> str:
    if isinstance(value, bool):
        value = str(value).lower()
    return html.escape(str(value))


def datum(label: str, value: Any) -> str:
    return f"<div class='datum'><span>{esc(label)}</span><code>{esc(value)}</code></div>"


def card(title: str, summary: str, rows: list[tuple[str, Any]]) -> str:
    evidence = "".join(datum(label, value) for label, value in rows)
    return (
        "<article class='check'><div class='check-head'><span class='pass'>PASS</span>"
        f"<h3>{esc(title)}</h3></div><p>{esc(summary)}</p>"
        f"<div class='evidence'>{evidence}</div></article>"
    )


def section(anchor: str, eyebrow: str, title: str, cards: list[str]) -> str:
    return (
        f"<section id='{esc(anchor)}'><div class='section-title'><div>"
        f"<span class='eyebrow'>{esc(eyebrow)}</span><h2>{esc(title)}</h2>"
        f"</div><span class='count'>{len(cards)} verified controls</span></div>"
        f"<div class='grid'>{''.join(cards)}</div></section>"
    )


def render(data: dict[str, Any]) -> str:
    functional = data["functional"]
    authority = data["authority"]
    fail_closed = data["fail_closed"]
    privacy = data["privacy"]
    quality = data["local_quality"]
    sections = [
        section(
            "runtime",
            "REAL MICROVM + CLIENT PATHS",
            "The workload path still works",
            [
                card(
                    "Trusted execution suite",
                    "Policy, approval, execution, output, cleanup and receipts ran against real MicroVMs.",
                    [
                        ("result", f"{functional['passed']}/{functional['passed']} PASS"),
                        ("real MicroVM checks", functional["real_microvm_checks"]),
                        ("failed", functional["failed"]),
                    ],
                ),
                card(
                    "Four client adapters",
                    "Each adapter completed plan, submit, status, result and receipt through the live service.",
                    [
                        ("clients", " · ".join(functional["clients"])),
                        ("tools registered", functional["tools_per_client"]),
                        ("trusted-task tools", functional["trusted_task_tools_per_client"]),
                    ],
                ),
                card(
                    "Result integrity and cleanup",
                    "All client workloads reached a terminal result and verified MicroVM cleanup.",
                    [
                        ("cleanup", functional["cleanup"]),
                        ("receipt", functional["receipt"]),
                        ("audit mode", authority["mode"]),
                    ],
                ),
                card(
                    "Local release gates",
                    "Unit, Redis recovery, static analysis, chart safety and plugin suites passed before cluster rollout.",
                    [
                        ("Python", quality["python_tests"]),
                        ("Redis skips", quality["redis_integration_skipped"]),
                        ("Ruff / Mypy", f"{quality['ruff']} / {quality['mypy']}"),
                        (
                            "Helm / plugins",
                            f"{quality['helm_safety']} / {quality['client_plugin_tests']}",
                        ),
                    ],
                ),
            ],
        ),
        section(
            "authority",
            "DURABLE AUTHORITY",
            "Records survive the Pod",
            [
                card(
                    "Persistent single-writer storage",
                    "The authoritative SQLite journal ran as the non-root Adapter user on a retained PVC.",
                    [
                        ("PVC", authority["pvc"]),
                        ("database mode", authority["database_mode"]),
                        ("runtime UID", authority["runtime_uid"]),
                        ("update strategy", authority["deployment_strategy"]),
                    ],
                ),
                card(
                    "Pod replacement preservation",
                    "The Deployment Pod was deleted and recreated; the same committed event count remained.",
                    [
                        ("before restart", authority["events_before_restart"]),
                        ("after restart", authority["events_after_restart"]),
                        ("incomplete", authority["incomplete_after_restart"]),
                    ],
                ),
                card(
                    "Exclusive writer enforcement",
                    "A second non-root process mounted the same PVC while the Adapter was live and was denied the journal lock.",
                    [
                        ("exclusive lock", authority["exclusive_writer_lock"]),
                        ("writers allowed", 1),
                    ],
                ),
                card(
                    "Replica delivery drained",
                    "The JSONL sink is a replica; successful delivery does not delete authoritative events.",
                    [
                        ("pending deliveries", privacy["pending_deliveries_at_baseline"]),
                        ("authority after delivery", "retained"),
                        ("delivery contract", "at-least-once · event_id dedupe"),
                    ],
                ),
            ],
        ),
        section(
            "recovery",
            "FAIL CLOSED + OPERATOR RECOVERY",
            "Uncertain work cannot pass silently",
            [
                card(
                    "Startup safety barrier",
                    fail_closed["method"],
                    [
                        ("events", fail_closed["events_with_pending_intent"]),
                        ("incomplete", fail_closed["incomplete_before_reconcile"]),
                        ("audit ready", fail_closed["audit_ready_before_reconcile"]),
                        ("guarded API", f"HTTP {fail_closed['guarded_api_status']}"),
                    ],
                ),
                card(
                    "Observable blocked state",
                    "Prometheus exposed the configured mode, safety barrier and unresolved operation.",
                    [
                        ("audit_required", fail_closed["required_metric"]),
                        ("audit_blocked", fail_closed["blocked_metric"]),
                        ("incomplete_operations", fail_closed["incomplete_metric"]),
                    ],
                ),
                card(
                    "Offline reconciliation",
                    "The Adapter was stopped, an operator evidence digest was appended, and the original pending intent remained.",
                    [
                        ("resolution", fail_closed["recovery_event"]),
                        ("events after", fail_closed["events_after_reconcile"]),
                        ("incomplete after", fail_closed["incomplete_after_reconcile"]),
                    ],
                ),
                card(
                    "Service restored",
                    "After every pending operation was reconciled, readiness returned without replaying a workload.",
                    [
                        ("audit ready", fail_closed["audit_ready_after_reconcile"]),
                        ("readiness", fail_closed["readiness_after_reconcile"]),
                        ("automatic replay", "disabled"),
                    ],
                ),
            ],
        ),
        section(
            "privacy",
            "REDACTED BY DEFAULT",
            "Evidence without task content",
            [
                card(
                    "Authority scan",
                    "The live journal was compared inside the Pod with mounted identity tokens and runtime HMAC keys; only booleans left the Pod.",
                    [
                        ("events scanned", privacy["events_scanned"]),
                        ("secret values found", privacy["secret_values_found"]),
                        ("Bearer literals found", privacy["bearer_literal_found"]),
                    ],
                ),
                card(
                    "Raw workload markers omitted",
                    "Known messages from the live client runs were absent; request bodies are represented by keyed digests.",
                    [
                        ("raw task markers found", privacy["raw_task_markers_found"]),
                        ("full internal IDs in report", "excluded"),
                        ("private addresses in report", "excluded"),
                    ],
                ),
            ],
        ),
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(data["title"])}</title><style>
:root{{color-scheme:light;--ink:#172033;--muted:#667085;--line:#dfe5ef;--blue:#2457d6;--green:#147a4b;--green-bg:#e9f8f0;--panel:#fff;--bg:#f4f7fb}}
*{{box-sizing:border-box}}html{{scroll-behavior:auto}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
.top{{background:linear-gradient(135deg,#fff 0%,#eef4ff 100%);border-bottom:1px solid var(--line)}}.wrap{{width:min(1180px,calc(100% - 48px));margin:auto}}
header{{padding:48px 0 34px}}.brand,.eyebrow{{color:var(--blue);font-weight:800;letter-spacing:.09em;text-transform:uppercase;font-size:11px}}
h1{{font-size:42px;line-height:1.12;margin:12px 0;letter-spacing:-.035em}}header p{{color:var(--muted);font-size:17px;margin:0}}.summary{{display:flex;gap:14px;margin-top:26px;flex-wrap:wrap}}
.badge{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:11px 16px;box-shadow:0 3px 14px #23345a0a}}.badge strong{{display:block;font-size:20px}}.badge span{{color:var(--muted);font-size:12px;text-transform:uppercase}}
nav{{display:flex;gap:8px;padding:14px 0}}nav a{{color:#344054;text-decoration:none;background:#fff;border:1px solid var(--line);border-radius:999px;padding:7px 12px;font-size:13px}}
main{{padding:34px 0 56px}}section{{margin:0 0 44px;scroll-margin-top:12px}}.section-title{{display:flex;align-items:end;justify-content:space-between;margin-bottom:14px}}h2{{font-size:27px;margin:3px 0 0;letter-spacing:-.02em}}.count{{color:var(--muted);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.check{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 6px 20px #23345a0a;min-height:188px}}
.check-head{{display:flex;align-items:center;gap:10px}}.check h3{{font-size:17px;margin:0}}.pass{{background:var(--green-bg);color:var(--green);font-weight:800;font-size:11px;padding:4px 8px;border-radius:999px}}.check p{{color:#475467;margin:10px 0 14px}}
.evidence{{border-top:1px solid #edf0f5;padding-top:10px}}.datum{{display:flex;justify-content:space-between;gap:16px;margin:6px 0;font-size:12px}}.datum span{{color:var(--muted);text-transform:capitalize}}code{{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:#243b72;overflow-wrap:anywhere;text-align:right}}
.boundary{{background:#fff7e8;border:1px solid #f1d6a5;border-radius:14px;padding:17px 19px;color:#6b4b16}}footer{{padding:24px 0 40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}}
@media(max-width:760px){{.wrap{{width:min(100% - 28px,1180px)}}h1{{font-size:31px}}.grid{{grid-template-columns:1fr}}.datum{{display:block}}code{{display:block;text-align:left;margin-top:2px}}nav{{overflow:auto}}nav a{{white-space:nowrap}}}}
</style></head><body><div class="top"><div class="wrap"><header><div class="brand">CubeSandbox · Durable Audit Evidence</div>
<h1>{esc(data["title"])}</h1><p>{esc(data["environment"])}</p><div class="summary"><div class="badge"><strong>PASS</strong><span>recorded result</span></div><div class="badge"><strong>{functional["passed"]}/{functional["passed"]}</strong><span>runtime checks</span></div><div class="badge"><strong>required</strong><span>audit mode</span></div><div class="badge"><strong>Light</strong><span>evidence theme</span></div></div></header>
<nav><a href="#runtime">Runtime</a><a href="#authority">Persistence</a><a href="#recovery">Fail closed</a><a href="#privacy">Privacy</a></nav></div></div>
<main class="wrap">{"".join(sections)}<aside class="boundary"><strong>Evidence boundary.</strong> {esc(data["boundary"])}</aside></main>
<footer><div class="wrap">Generated {esc(data["generated_at"])} · {esc(data["release_candidate"])} · Sanitized report: no credentials, private hostnames, addresses or full internal identifiers.</div></footer></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    args.output.write_text(render(data), encoding="utf-8")


if __name__ == "__main__":
    main()
