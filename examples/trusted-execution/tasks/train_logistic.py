#!/usr/bin/env python3
"""Deterministic, dependency-free binary logistic-regression reference task."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def parse_columns(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_rows(
    path: Path, label_name: str, requested_features: list[str]
) -> tuple[list[str], list[list[float]], list[float]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        if label_name not in columns:
            raise ValueError(f"missing label column: {label_name}")
        features = requested_features or [name for name in columns if name != label_name]
        if not features or any(name not in columns for name in features):
            raise ValueError("feature columns are empty or missing")

        samples: list[list[float]] = []
        labels: list[float] = []
        for row_number, row in enumerate(reader, start=2):
            try:
                sample = [float(row[name]) for name in features]
                label = float(row[label_name])
            except (TypeError, ValueError) as error:
                raise ValueError(f"non-numeric value at row {row_number}") from error
            if not all(math.isfinite(value) for value in sample + [label]):
                raise ValueError(f"non-finite value at row {row_number}")
            if label not in {0.0, 1.0}:
                raise ValueError(f"label must be 0 or 1 at row {row_number}")
            samples.append(sample)
            labels.append(label)

    if len(samples) < 2 or len(set(labels)) != 2:
        raise ValueError("training data must contain at least two rows and both labels")
    return features, samples, labels


def standardize(samples: list[list[float]]) -> tuple[list[list[float]], list[float], list[float]]:
    width = len(samples[0])
    means = [sum(row[index] for row in samples) / len(samples) for index in range(width)]
    deviations = []
    for index, mean in enumerate(means):
        variance = sum((row[index] - mean) ** 2 for row in samples) / len(samples)
        deviations.append(math.sqrt(variance) or 1.0)
    normalized = [
        [(value - means[index]) / deviations[index] for index, value in enumerate(row)]
        for row in samples
    ]
    return normalized, means, deviations


def sigmoid(value: float) -> float:
    if value >= 0:
        factor = math.exp(-value)
        return 1.0 / (1.0 + factor)
    factor = math.exp(value)
    return factor / (1.0 + factor)


def train(
    samples: list[list[float]],
    labels: list[float],
    epochs: int,
    learning_rate: float,
) -> tuple[list[float], float, float, float]:
    weights = [0.0] * len(samples[0])
    bias = 0.0
    count = float(len(samples))

    for _ in range(epochs):
        probabilities = [
            sigmoid(
                sum(
                    weight * value
                    for weight, value in zip(weights, row, strict=True)
                )
                + bias
            )
            for row in samples
        ]
        errors = [
            probability - label
            for probability, label in zip(probabilities, labels, strict=True)
        ]
        for index in range(len(weights)):
            gradient = (
                sum(
                    error * row[index]
                    for error, row in zip(errors, samples, strict=True)
                )
                / count
            )
            weights[index] -= learning_rate * gradient
        bias -= learning_rate * sum(errors) / count

    probabilities = [
        sigmoid(
            sum(
                weight * value for weight, value in zip(weights, row, strict=True)
            )
            + bias
        )
        for row in samples
    ]
    epsilon = 1e-12
    loss = -sum(
        label * math.log(max(probability, epsilon))
        + (1.0 - label) * math.log(max(1.0 - probability, epsilon))
        for probability, label in zip(probabilities, labels, strict=True)
    ) / count
    accuracy = sum(
        (probability >= 0.5) == bool(label)
        for probability, label in zip(probabilities, labels, strict=True)
    ) / count
    return weights, bias, loss, accuracy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--label", default="label")
    parser.add_argument("--features", default="")
    parser.add_argument("--epochs", type=int, default=300)
    parser.add_argument("--learning-rate", type=float, default=0.1)
    args = parser.parse_args()

    if args.epochs < 1 or args.epochs > 100_000:
        raise SystemExit("epochs must be between 1 and 100000")
    if not 0.0 < args.learning_rate <= 10.0:
        raise SystemExit("learning rate must be greater than 0 and at most 10")

    input_path = args.input.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    features, raw_samples, labels = load_rows(
        input_path, args.label, parse_columns(args.features)
    )
    samples, means, deviations = standardize(raw_samples)
    weights, bias, loss, accuracy = train(
        samples, labels, args.epochs, args.learning_rate
    )

    model_path = output_dir / "model.json"
    metrics_path = output_dir / "metrics.json"
    manifest_path = output_dir / "execution-manifest.json"
    write_json(
        model_path,
        {
            "algorithm": "binary-logistic-regression",
            "bias": bias,
            "features": features,
            "label": args.label,
            "standardization": {"mean": means, "scale": deviations},
            "weights": weights,
        },
    )
    write_json(
        metrics_path,
        {
            "epochs": args.epochs,
            "feature_count": len(features),
            "final_training_loss": loss,
            "learning_rate": args.learning_rate,
            "row_count": len(samples),
            "training_accuracy": accuracy,
        },
    )
    write_json(
        manifest_path,
        {
            "input": {
                "name": input_path.name,
                "sha256": sha256_file(input_path),
            },
            "outputs": {
                "metrics.json": sha256_file(metrics_path),
                "model.json": sha256_file(model_path),
            },
            "task": "trusted-training-reference",
        },
    )
    print(
        json.dumps(
            {
                "accuracy": accuracy,
                "input_sha256": sha256_file(input_path),
                "rows": len(samples),
                "status": "ok",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
