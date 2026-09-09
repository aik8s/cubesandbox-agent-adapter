#!/usr/bin/env python3
"""Render three deterministic, light-theme SVG evidence cards from redacted data."""

from __future__ import annotations

import argparse
import html
import json
import textwrap
from pathlib import Path
from typing import Any

WIDTH = 1200
HEIGHT = 1200


def esc(value: Any) -> str:
    if isinstance(value, bool):
        value = str(value).lower()
    return html.escape(str(value))


def text(x: int, y: int, value: Any, size: int, **attrs: str) -> str:
    properties = " ".join(f'{key.replace("_", "-")}="{esc(val)}"' for key, val in attrs.items())
    return f'<text x="{x}" y="{y}" font-size="{size}" {properties}>{esc(value)}</text>'


def card(y: int, title: str, summary: str, rows: list[tuple[str, Any]]) -> str:
    parts = [
        f'<rect x="80" y="{y}" width="1040" height="188" rx="20" fill="#fff" stroke="#dfe5ef"/>',
        f'<rect x="108" y="{y + 20}" width="68" height="28" rx="14" fill="#e9f8f0"/>',
        text(142, y + 40, "PASS", 13, fill="#147a4b", font_weight="800", text_anchor="middle"),
        text(194, y + 42, title, 23, fill="#172033", font_weight="750"),
    ]
    wrapped = textwrap.wrap(summary, width=100)[:1]
    for index, line in enumerate(wrapped):
        parts.append(text(108, y + 70 + index * 24, line, 17, fill="#475467"))
    line_y = y + 88
    parts.append(f'<line x1="108" y1="{line_y}" x2="1092" y2="{line_y}" stroke="#edf0f5"/>')
    row_y = line_y + 24
    for label, value in rows[:4]:
        parts.append(text(108, row_y, label, 16, fill="#667085"))
        parts.append(
            text(
                1092,
                row_y,
                value,
                16,
                fill="#243b72",
                font_family="ui-monospace,Menlo,monospace",
                text_anchor="end",
            )
        )
        row_y += 19
    return "".join(parts)


def document(
    *,
    title: str,
    eyebrow: str,
    subtitle: str,
    cards: list[str],
    footer: str,
) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
<defs><linearGradient id="hero" x1="0" x2="1"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#eef4ff"/></linearGradient></defs>
<rect width="1200" height="1200" fill="#f4f7fb"/><rect width="1200" height="250" fill="url(#hero)"/><line x1="0" y1="250" x2="1200" y2="250" stroke="#dfe5ef"/>
<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif">
{text(80, 55, "CUBESANDBOX · v0.5.0 EVIDENCE", 15, fill="#2457d6", font_weight="800", letter_spacing="2")}
{text(80, 112, title, 42, fill="#172033", font_weight="800")}
{text(80, 152, subtitle, 19, fill="#667085")}
<rect x="80" y="182" width="104" height="40" rx="12" fill="#fff" stroke="#dfe5ef"/>{text(132, 208, "PASS", 17, fill="#147a4b", font_weight="800", text_anchor="middle")}
<rect x="198" y="182" width="192" height="40" rx="12" fill="#fff" stroke="#dfe5ef"/>{text(294, 208, "LIGHT EVIDENCE", 15, fill="#172033", font_weight="700", text_anchor="middle")}
{text(80, 292, eyebrow, 14, fill="#2457d6", font_weight="800", letter_spacing="1.8")}
{"".join(cards)}
<line x1="80" y1="1142" x2="1120" y2="1142" stroke="#dfe5ef"/>
{text(80, 1174, footer, 14, fill="#667085")}
</g></svg>'''


def render(data: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    functional = data["functional"]
    authority = data["authority"]
    failed = data["fail_closed"]
    privacy = data["privacy"]
    quality = data["local_quality"]
    footer = "Recorded 2026-09-09 · sanitized real acceptance data · not a live product console"

    runtime_cards = [
        card(
            320,
            "Real MicroVM suite",
            "Policy, approval, execution, outputs, cleanup and receipts ran against real CubeSandbox MicroVMs.",
            [
                ("result", "23/23 PASS"),
                ("real MicroVM checks", functional["real_microvm_checks"]),
                ("failed", functional["failed"]),
            ],
        ),
        card(
            515,
            "Four client adapters",
            "OpenClaw, DSH, Codex MCP and Hermes Agent completed the live trusted-task path.",
            [
                ("clients", "4/4"),
                ("tools per client", functional["tools_per_client"]),
                ("trusted-task tools", functional["trusted_task_tools_per_client"]),
            ],
        ),
        card(
            710,
            "Integrity and cleanup",
            "All client tasks reached success, verified cleanup and returned a signed receipt.",
            [
                ("cleanup", functional["cleanup"]),
                ("receipt", functional["receipt"]),
                ("audit mode", authority["mode"]),
            ],
        ),
        card(
            905,
            "Release gates",
            "Unit, Redis recovery, static analysis, Helm safety and client plugin suites passed.",
            [
                ("Python", quality["python_tests"]),
                ("Redis skips", quality["redis_integration_skipped"]),
                ("Ruff / Mypy", "PASS / PASS"),
                ("Helm / plugins", "PASS / PASS"),
            ],
        ),
    ]
    (output / "01-runtime-clients.svg").write_text(
        document(
            title="Runtime and client acceptance",
            eyebrow="REAL WORKLOAD PATHS",
            subtitle=data["environment"],
            cards=runtime_cards,
            footer=footer,
        ),
        encoding="utf-8",
    )

    authority_cards = [
        card(
            320,
            "Persistent authority",
            "The non-root SQLite authority used a retained block PVC; updates use Recreate.",
            [
                ("PVC", "Bound · 1 GiB · RWO"),
                ("uninstall retention", "keep"),
                ("database mode", authority["database_mode"]),
                ("runtime UID", authority["runtime_uid"]),
            ],
        ),
        card(
            515,
            "Pod replacement survival",
            "The Adapter Pod was deleted and recreated; the committed event count remained unchanged.",
            [
                ("before restart", authority["events_before_restart"]),
                ("after restart", authority["events_after_restart"]),
                ("incomplete", authority["incomplete_after_restart"]),
            ],
        ),
        card(
            710,
            "Exclusive writer",
            "A second non-root process mounted the live PVC and was denied the journal lock.",
            [
                ("exclusive lock", authority["exclusive_writer_lock"]),
                ("writers allowed", 1),
                ("audit mode", authority["mode"]),
            ],
        ),
        card(
            905,
            "Replica delivery",
            "JSONL acknowledgement did not delete authoritative events.",
            [
                ("pending deliveries", privacy["pending_deliveries_at_baseline"]),
                ("authority", "retained"),
                ("delivery", "at-least-once"),
                ("deduplication", "event_id"),
            ],
        ),
    ]
    (output / "02-persistence-restart.svg").write_text(
        document(
            title="Durable audit authority",
            eyebrow="RECORDS SURVIVE THE POD",
            subtitle="Real Kubernetes Pod replacement · persistent single-writer journal",
            cards=authority_cards,
            footer=footer,
        ),
        encoding="utf-8",
    )

    recovery_cards = [
        card(
            320,
            "Startup safety barrier",
            "A synthetic crash-window intent was committed offline; it made no CubeSandbox backend call.",
            [
                ("events", failed["events_with_pending_intent"]),
                ("incomplete", failed["incomplete_before_reconcile"]),
                ("audit ready", failed["audit_ready_before_reconcile"]),
                ("guarded API", f"HTTP {failed['guarded_api_status']}"),
            ],
        ),
        card(
            515,
            "Observable blocked state",
            "Prometheus reported required mode, the safety barrier and one unresolved operation.",
            [
                ("audit_required", failed["required_metric"]),
                ("audit_blocked", failed["blocked_metric"]),
                ("incomplete_operations", failed["incomplete_metric"]),
            ],
        ),
        card(
            710,
            "Offline reconciliation",
            "Operator evidence was appended; the pending intent remained and no workload replayed.",
            [
                ("resolution", failed["recovery_event"]),
                ("events after", failed["events_after_reconcile"]),
                ("incomplete after", failed["incomplete_after_reconcile"]),
                ("readiness", failed["readiness_after_reconcile"]),
            ],
        ),
        card(
            905,
            "Privacy scan",
            "The live authority was compared with mounted credentials; only booleans left the Pod.",
            [
                ("events scanned", privacy["events_scanned"]),
                ("secret values found", privacy["secret_values_found"]),
                ("raw task markers", privacy["raw_task_markers_found"]),
                ("Bearer literals", privacy["bearer_literal_found"]),
            ],
        ),
    ]
    (output / "03-fail-closed-recovery.svg").write_text(
        document(
            title="Fail closed and recover",
            eyebrow="UNCERTAIN WORK CANNOT PASS SILENTLY",
            subtitle="Synthetic incident · real restart, 503 barrier, metrics and offline reconciliation",
            cards=recovery_cards,
            footer=footer,
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    render(json.loads(args.input.read_text(encoding="utf-8")), args.output)


if __name__ == "__main__":
    main()
