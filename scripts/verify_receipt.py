#!/usr/bin/env python3
"""Offline verifier for Cube Adapter HS256 execution receipts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Dict


def _receipt(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("receipt file must contain a JSON object")
    candidate = value.get("receipt", value)
    if not isinstance(candidate, dict):
        raise ValueError("receipt is not a JSON object")
    return candidate


def verify(value: Any, key: str) -> Dict[str, Any]:
    receipt = _receipt(value)
    payload = receipt.get("payload")
    signature = receipt.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, dict):
        raise ValueError("receipt payload or signature is missing")
    if signature.get("alg") != "HS256" or not isinstance(signature.get("value"), str):
        raise ValueError("receipt signature algorithm is unsupported")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected = base64.urlsafe_b64encode(
        hmac.new(key.encode("utf-8"), encoded, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(expected, signature["value"]):
        raise ValueError("receipt signature is invalid")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path, help="JSON receipt or Adapter response")
    parser.add_argument(
        "--key-file",
        type=Path,
        help="receipt HMAC key file; otherwise use the receipt/HMAC environment key",
    )
    args = parser.parse_args()
    key = (
        args.key_file.read_text(encoding="utf-8").strip()
        if args.key_file
        else os.environ.get("CUBE_ADAPTER_RECEIPT_HMAC_KEY")
        or os.environ.get("CUBE_ADAPTER_HMAC_KEY", "")
    )
    if len(key) < 32:
        raise SystemExit("a receipt HMAC key of at least 32 characters is required")
    try:
        payload = verify(json.loads(args.receipt.read_text(encoding="utf-8")), key)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(
        json.dumps(
            {
                "valid": True,
                "task_ref": payload.get("task_ref"),
                "state": payload.get("state"),
                "template_sha256": payload.get("template_sha256"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
