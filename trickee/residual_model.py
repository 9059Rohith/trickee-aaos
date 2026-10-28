"""Experimental regularized residual baseline for range and ETA.

Training uses TRAIN rows only. The model predicts a bounded correction to an
existing physics/map baseline and has no authority to control the vehicle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model_eval import EvaluationRow, evaluate, load_jsonl


ROADS = ("URBAN", "HIGHWAY", "HILL", "MIXED")
WEATHERS = ("CLEAR", "RAIN", "HOT", "COLD")
FEATURES = ("log_baseline", "temperature_c", "soc_pct") + tuple(
    f"road_{road}" for road in ROADS) + tuple(f"weather_{weather}" for weather in WEATHERS)


def _features(predicted: float, temperature_c: float | None,
              soc_pct: float | None, road_type: str | None,
              weather: str | None) -> list[float]:
    if not math.isfinite(predicted) or predicted <= 0:
        raise ValueError("baseline prediction must be finite and positive")
    return [math.log1p(predicted),
            temperature_c if temperature_c is not None else 20.0,
            soc_pct if soc_pct is not None else 50.0,
            *[float(road_type == road) for road in ROADS],
            *[float(weather == condition) for condition in WEATHERS]]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Small ridge normal equation with pivoting; fail on numerical collapse."""
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda index: abs(augmented[index][col]))
        if abs(augmented[pivot][col]) < 1e-10:
            raise ValueError("residual feature matrix is singular")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        for index in range(col, size + 1):
            augmented[col][index] /= scale
        for row in range(size):
            if row == col:
                continue
            multiple = augmented[row][col]
            for index in range(col, size + 1):
                augmented[row][index] -= multiple * augmented[col][index]
    return [augmented[row][-1] for row in range(size)]


class ResidualModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model_type: Literal["RIDGE_RESIDUAL_V1"] = "RIDGE_RESIDUAL_V1"
    task: Literal["RANGE", "ETA"]
    trained_at: datetime
    training_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_count: int = Field(ge=30)
    source: Literal["FIELD", "SIMULATED", "REPLAY", "MIXED"]
    regularization: float = Field(gt=0)
    feature_names: tuple[str, ...]
    means: list[float]
    scales: list[float]
    coefficients: list[float]

    @model_validator(mode="after")
    def valid_shape(self) -> "ResidualModel":
        size = len(FEATURES)
        if (self.feature_names != FEATURES or len(self.means) != size or
                len(self.scales) != size or len(self.coefficients) != size + 1 or
                any(scale <= 0 for scale in self.scales)):
            raise ValueError("residual feature schema mismatch")
        if self.trained_at.tzinfo is None:
            raise ValueError("training time requires timezone")
        return self

    def predict(self, baseline: float, *, temperature_c: float | None = None,
                soc_pct: float | None = None, road_type: str | None = None,
                weather: str | None = None) -> float:
        raw = _features(baseline, temperature_c, soc_pct, road_type, weather)
        normalized = [(value - mean) / scale
                      for value, mean, scale in zip(raw, self.means, self.scales)]
        correction = self.coefficients[0] + sum(
            coefficient * value for coefficient, value in
            zip(self.coefficients[1:], normalized))
        # Residuals are advisory: cap correction to avoid runaway estimates.
        return round(max(0, baseline * (1 + max(-.4, min(.4, correction)))), 4)


def train(rows: list[EvaluationRow], task: Literal["RANGE", "ETA"],
          regularization: float = 10.0) -> ResidualModel:
    if task not in ("RANGE", "ETA") or not math.isfinite(regularization) or regularization <= 0:
        raise ValueError("only range and ETA residuals with positive regularization are supported")
    # Includes duplicate-ID and cross-split trip-leakage checks.
    evaluate(rows, minimum_samples=1)
    training = [row for row in rows if row.task == task and row.split == "TRAIN"]
    if len(training) < 30:
        raise ValueError("at least 30 training rows are required")
    if any(row.predicted <= 0 for row in training):
        raise ValueError("training baselines must be positive")
    raw = [_features(row.predicted, row.temperature_c, row.soc_pct,
                     row.road_type, row.weather) for row in training]
    columns = list(zip(*raw))
    means = [sum(column) / len(column) for column in columns]
    scales = [max(math.sqrt(sum((value - mean) ** 2 for value in column)
                            / len(column)), 1e-9)
              for column, mean in zip(columns, means)]
    normalized = [[1.0] + [(value - mean) / scale
                           for value, mean, scale in zip(vector, means, scales)]
                  for vector in raw]
    outcomes = [max(-.4, min(.4, row.actual / row.predicted - 1))
                for row in training]
    size = len(FEATURES) + 1
    matrix = [[sum(vector[i] * vector[j] for vector in normalized)
               + (regularization if i == j and i > 0 else 0.0)
               for j in range(size)] for i in range(size)]
    right = [sum(vector[i] * outcome for vector, outcome in
                 zip(normalized, outcomes)) for i in range(size)]
    coefficients = _solve(matrix, right)
    canonical = "\n".join(row.model_dump_json() for row in training)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    sources = {row.source for row in training}
    return ResidualModel(task=task, trained_at=datetime.now(timezone.utc),
                         training_dataset_sha256=fingerprint,
                         training_count=len(training),
                         source=next(iter(sources)) if len(sources) == 1 else "MIXED",
                         regularization=regularization, feature_names=FEATURES,
                         means=means, scales=scales, coefficients=coefficients)


def evaluate_residual(model: ResidualModel, rows: list[EvaluationRow],
                      minimum_samples: int = 30) -> dict:
    baseline = evaluate(rows, minimum_samples=minimum_samples)
    held_out = [row for row in rows if row.task == model.task and row.split == "TEST"]
    adjusted = [row.model_copy(update={"predicted": model.predict(
        row.predicted, temperature_c=row.temperature_c, soc_pct=row.soc_pct,
        road_type=row.road_type, weather=row.weather)}) for row in held_out]
    scored = evaluate(adjusted, minimum_samples=minimum_samples)
    return {"task": model.task, "training_count": model.training_count,
            "training_source": model.source,
            "baseline": baseline["tasks"][model.task],
            "residual": scored["tasks"][model.task],
            "test_rows": len(held_out)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Train an experimental residual baseline")
    parser.add_argument("dataset", help="JSONL evaluation dataset")
    parser.add_argument("--task", choices=("RANGE", "ETA"), required=True)
    parser.add_argument("--artifact", required=True, help="Output model JSON")
    parser.add_argument("--report", required=True, help="Output held-out report JSON")
    parser.add_argument("--regularization", type=float, default=10.0)
    args = parser.parse_args()
    rows = load_jsonl(args.dataset)
    model = train(rows, args.task, args.regularization)
    report = evaluate_residual(model, rows)
    for path, content in ((Path(args.artifact), model.model_dump_json(indent=2)),
                          (Path(args.report), json.dumps(report, indent=2))):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content + "\n", encoding="utf-8")
        temporary.replace(path)
    print(json.dumps({"artifact": args.artifact, "report": args.report,
                      "task": model.task, "training_source": model.source}))


if __name__ == "__main__":
    main()
