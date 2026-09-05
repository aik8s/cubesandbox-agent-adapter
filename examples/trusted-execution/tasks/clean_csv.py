#!/usr/bin/env python3
"""Stream, validate, minimize, and deduplicate a CSV without logging row data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable


def parse_columns(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_known(columns: Iterable[str], fieldnames: list[str], kind: str) -> None:
    missing = sorted(set(columns) - set(fieldnames))
    if missing:
        raise ValueError(f"unknown {kind} columns: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--required-columns", default="")
    parser.add_argument("--drop-columns", default="")
    parser.add_argument("--hash-columns", default="")
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    args = parser.parse_args()

    if args.max_rows < 1:
        raise SystemExit("max-rows must be positive")
    input_path = args.input.resolve(strict=True)
    output_path = args.output.resolve()
    report_path = args.report.resolve()
    if output_path == input_path or report_path == input_path:
        raise SystemExit("input must not be overwritten")

    required = parse_columns(args.required_columns)
    dropped = parse_columns(args.drop_columns)
    hashed = parse_columns(args.hash_columns)
    if set(dropped) & set(hashed):
        raise SystemExit("a column cannot be both dropped and hashed")
    hash_key = os.environ.get("CLEANING_HASH_KEY", "").encode("utf-8")
    if hashed and not hash_key:
        raise SystemExit("CLEANING_HASH_KEY is required when --hash-columns is used")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    input_rows = 0
    output_rows = 0
    missing_required_rows = 0
    duplicate_rows = 0
    seen: set[str] = set()

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".tmp", dir=output_path.parent
    )
    try:
        with (
            input_path.open("r", encoding="utf-8", newline="") as source,
            os.fdopen(descriptor, "w", encoding="utf-8", newline="") as target,
        ):
            reader = csv.DictReader(source)
            fieldnames = reader.fieldnames or []
            if not fieldnames or len(fieldnames) != len(set(fieldnames)):
                raise ValueError("CSV header is empty or contains duplicate columns")
            require_known(required, fieldnames, "required")
            require_known(dropped, fieldnames, "drop")
            require_known(hashed, fieldnames, "hash")
            output_fields = [name for name in fieldnames if name not in dropped]
            writer = csv.DictWriter(target, fieldnames=output_fields)
            writer.writeheader()

            for row in reader:
                input_rows += 1
                if input_rows > args.max_rows:
                    raise ValueError("input exceeds max-rows")
                normalized = {
                    name: (row.get(name) or "").strip() for name in fieldnames
                }
                if any(not normalized[name] for name in required):
                    missing_required_rows += 1
                    continue
                for name in hashed:
                    normalized[name] = hmac.new(
                        hash_key, normalized[name].encode("utf-8"), hashlib.sha256
                    ).hexdigest()
                minimized = {name: normalized[name] for name in output_fields}
                fingerprint = hashlib.sha256(
                    json.dumps(
                        minimized, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                    ).encode("utf-8")
                ).hexdigest()
                if fingerprint in seen:
                    duplicate_rows += 1
                    continue
                seen.add(fingerprint)
                writer.writerow(minimized)
                output_rows += 1

        os.replace(temporary_name, output_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    report = {
        "columns": {
            "dropped": dropped,
            "hashed": hashed,
            "output": output_fields,
            "required": required,
        },
        "input": {
            "name": input_path.name,
            "rows": input_rows,
            "sha256": sha256_file(input_path),
        },
        "output": {
            "duplicate_rows_removed": duplicate_rows,
            "missing_required_rows_removed": missing_required_rows,
            "rows": output_rows,
            "sha256": sha256_file(output_path),
        },
        "task": "trusted-data-cleaning-reference",
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "input_rows": input_rows,
                "output_rows": output_rows,
                "status": "ok",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
