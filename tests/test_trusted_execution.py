#!/usr/bin/env python3
"""Tests for the trusted-execution profiles and reference tasks."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapter.config import load_profiles  # noqa: E402
from adapter.task_config import load_task_templates  # noqa: E402

EXAMPLES = ROOT / "examples" / "trusted-execution"


class TrustedExecutionTests(unittest.TestCase):
    def test_profiles_are_mcp_only_and_fail_closed(self) -> None:
        profiles = load_profiles(
            str(EXAMPLES / "profiles.yaml"),
            default_template="unused",
            sandbox_timeout_seconds=300,
            max_command_seconds=120,
        )
        self.assertEqual(
            set(profiles), {"trusted-training", "trusted-data-cleaning"}
        )
        for profile in profiles.values():
            self.assertEqual(profile.allowed_runtimes, ("mcp",))
            self.assertFalse(profile.allow_internet_access)
            self.assertFalse(profile.network["allow_public_traffic"])
            self.assertEqual(profile.workspace.mode, "per-session-volume")
            self.assertFalse(profile.workspace.retain_on_kill)
            self.assertFalse(profile.checkpoints_enabled)
            self.assertEqual(profile.max_jobs_per_lease, 1)

    def test_training_task_writes_metrics_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "training"
            completed = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    str(EXAMPLES / "tasks" / "train_logistic.py"),
                    "--input",
                    str(EXAMPLES / "fixtures" / "training.csv"),
                    "--output-dir",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads(completed.stdout)
            metrics = json.loads((output / "metrics.json").read_text())
            manifest = json.loads((output / "execution-manifest.json").read_text())
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(metrics["row_count"], 8)
            self.assertEqual(metrics["training_accuracy"], 1.0)
            self.assertEqual(len(manifest["input"]["sha256"]), 64)
            self.assertEqual(set(manifest["outputs"]), {"metrics.json", "model.json"})

    def test_server_task_templates_are_closed_and_fixed(self) -> None:
        profiles = load_profiles(
            str(EXAMPLES / "profiles.yaml"),
            default_template="unused",
            sandbox_timeout_seconds=300,
            max_command_seconds=120,
        )
        tasks = load_task_templates(
            str(EXAMPLES / "task-templates.yaml"), profiles=profiles
        )
        self.assertEqual(set(tasks), {"train-logistic", "clean-csv"})
        for task in tasks.values():
            self.assertTrue(task.approval_required)
            self.assertFalse(task.parameters["additionalProperties"])
            self.assertTrue(all(output.path.startswith("/workspace/") for output in task.outputs))
        command = tasks["train-logistic"].render_command(
            {
                "input": "/workspace/input/training.csv",
                "label": "label",
                "features": "feature_a,feature_b",
                "epochs": 300,
                "learning_rate": 0.1,
            }
        )
        self.assertIn("/opt/cube-tasks/train_logistic.py", command)

    def test_cleaning_task_minimizes_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cleaned.csv"
            report_path = Path(directory) / "report.json"
            completed = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    str(EXAMPLES / "tasks" / "clean_csv.py"),
                    "--input",
                    str(EXAMPLES / "fixtures" / "raw.csv"),
                    "--output",
                    str(output),
                    "--report",
                    str(report_path),
                    "--required-columns",
                    "id,event_time",
                    "--drop-columns",
                    "email,phone",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads(completed.stdout)
            report = json.loads(report_path.read_text())
            with output.open(newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(summary, {"input_rows": 5, "output_rows": 3, "status": "ok"})
            self.assertEqual(len(rows), 3)
            self.assertEqual(set(rows[0]), {"id", "event_time", "value"})
            self.assertEqual(report["output"]["duplicate_rows_removed"], 1)
            self.assertEqual(report["output"]["missing_required_rows_removed"], 1)
            self.assertEqual(len(report["output"]["sha256"]), 64)

    def test_cleaning_task_pseudonymizes_without_logging_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cleaned.csv"
            report_path = Path(directory) / "report.json"
            completed = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    str(EXAMPLES / "tasks" / "clean_csv.py"),
                    "--input",
                    str(EXAMPLES / "fixtures" / "raw.csv"),
                    "--output",
                    str(output),
                    "--report",
                    str(report_path),
                    "--required-columns",
                    "id,event_time",
                    "--drop-columns",
                    "phone",
                    "--hash-columns",
                    "email",
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "CLEANING_HASH_KEY": "synthetic-test-only-key"},
            )
            with output.open(newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertNotIn("example.invalid", completed.stdout)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(len(row["email"]) == 64 for row in rows))
            self.assertTrue(all("example.invalid" not in row["email"] for row in rows))


if __name__ == "__main__":
    unittest.main()
