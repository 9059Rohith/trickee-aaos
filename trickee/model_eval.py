"""Held-out evaluation for Trickee predictions, with explicit data provenance.

No metric is emitted from training/validation rows or an empty test set.
Synthetic evaluations are labelled and cannot become field evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Task = Literal["RANGE", "ETA", "CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"]
Source = Literal["FIELD", "SIMULATED", "REPLAY"]


class EvaluationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    sample_id: str = Field(min_length=1, max_length=128)
    trip_id: str | None = Field(default=None, max_length=128)
    task: Task
    source: Source
    split: Literal["TRAIN", "VALIDATION", "TEST"]
    observed_at: datetime
    predicted: float
    actual: float
    baseline_predicted: float | None = None
    personal_predicted: float | None = None
    interval_low: float | None = None
    interval_high: float | None = None
    vehicle_id: str | None = None
    driver_id: str | None = None
    road_type: str | None = None
    weather: str | None = None
    temperature_c: float | None = None
    soc_pct: float | None = Field(default=None, ge=0, le=100)
    soh_pct: float | None = Field(default=None, ge=0, le=100)
    season: str | None = None
    charger_class: str | None = None
    lead_time_hours: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_task(self) -> "EvaluationRow":
        if self.observed_at.tzinfo is None:
            raise ValueError("evaluation observation requires timezone")
        if self.task in ("HEALTH", "CHARGER_AVAILABILITY"):
            if not 0 <= self.predicted <= 1 or self.actual not in (0, 1):
                raise ValueError("classification needs probability and binary outcome")
        elif self.predicted < 0 or self.actual < 0:
            raise ValueError("negative duration or distance is invalid")
        if self.task == "ETA" and (self.predicted == 0 or self.actual == 0):
            raise ValueError("ETA durations must be positive")
        if (self.interval_low is None) != (self.interval_high is None):
            raise ValueError("prediction interval requires both bounds")
        if self.interval_low is not None and not self.interval_low <= self.predicted <= self.interval_high:
            raise ValueError("prediction must lie inside its interval")
        if self.baseline_predicted is not None and self.baseline_predicted < 0:
            raise ValueError("negative baseline prediction")
        if self.personal_predicted is not None and self.personal_predicted < 0:
            raise ValueError("negative personal prediction")
        return self


def load_jsonl(path: str | Path) -> list[EvaluationRow]:
    rows: list[EvaluationRow] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(EvaluationRow.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"invalid evaluation row at line {line_number}") from exc
    return rows


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _regression(rows: list[EvaluationRow], field: str = "predicted") -> dict:
    pairs = [(getattr(row, field), row.actual) for row in rows
             if getattr(row, field) is not None]
    if not pairs:
        return {"status": "NO_DATA", "sample_count": 0}
    errors = [prediction - actual for prediction, actual in pairs]
    absolute = [abs(error) for error in errors]
    result = {
        "status": "MEASURED", "sample_count": len(pairs),
        "mae": round(sum(absolute) / len(absolute), 4),
        "rmse": round(math.sqrt(sum(error * error for error in errors) / len(errors)), 4),
        "median_absolute_error": round(median(absolute), 4),
        "p90_absolute_error": round(_percentile(absolute, .9), 4),
        "bias": round(sum(errors) / len(errors), 4),
    }
    nonzero = [(prediction, actual) for prediction, actual in pairs if actual > 0]
    result["mape_pct"] = (round(sum(abs(prediction - actual) / actual for prediction, actual in nonzero)
                                / len(nonzero) * 100, 4) if nonzero else None)
    return result


def _auc(rows: list[EvaluationRow]) -> float | None:
    positive_count = sum(row.actual == 1 for row in rows)
    negative_count = len(rows) - positive_count
    if not positive_count or not negative_count:
        return None
    # The Mann-Whitney rank statistic handles ties in O(n log n), so a large
    # field dataset cannot turn evaluation into a quadratic request.
    ranked = sorted((row.predicted, row.actual) for row in rows)
    negative_below = 0
    wins = 0.0
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        tied_positive = sum(actual == 1 for _, actual in ranked[index:end])
        tied_negative = end - index - tied_positive
        wins += tied_positive * (negative_below + .5 * tied_negative)
        negative_below += tied_negative
        index = end
    return round(wins / (positive_count * negative_count), 4)


def _classification(rows: list[EvaluationRow]) -> dict:
    tp = sum(row.predicted >= .5 and row.actual == 1 for row in rows)
    fp = sum(row.predicted >= .5 and row.actual == 0 for row in rows)
    tn = sum(row.predicted < .5 and row.actual == 0 for row in rows)
    fn = sum(row.predicted < .5 and row.actual == 1 for row in rows)
    positive = tp + fn
    negative = tn + fp
    probability_error = sum(abs(row.predicted - row.actual) for row in rows) / len(rows)
    return {
        "status": "MEASURED", "sample_count": len(rows),
        "true_positive": tp, "false_positive": fp,
        "true_negative": tn, "false_negative": fn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / positive, 4) if positive else None,
        "false_positive_rate": round(fp / negative, 4) if negative else None,
        "false_negative_rate": round(fn / positive, 4) if positive else None,
        "brier_score": round(sum((row.predicted - row.actual) ** 2 for row in rows)
                             / len(rows), 4),
        "mean_absolute_probability_error": round(probability_error, 4),
        "auroc": _auc(rows),
    }


def _subgroups(rows: list[EvaluationRow], minimum: int) -> dict:
    result: dict[str, dict] = {}
    for field in ("vehicle_id", "driver_id", "road_type", "weather", "season",
                  "charger_class"):
        groups: dict[str, list[EvaluationRow]] = defaultdict(list)
        for row in rows:
            value = getattr(row, field)
            if value is not None:
                groups[str(value)].append(row)
        usable = {key: _regression(group) for key, group in groups.items()
                  if len(group) >= minimum}
        if usable:
            result[field] = usable
    for field, breaks in (("temperature_c", (-10, 10, 30, 45)),
                          ("soc_pct", (10, 20, 50, 80)),
                          ("soh_pct", (60, 75, 85, 95))):
        groups = defaultdict(list)
        for row in rows:
            value = getattr(row, field)
            if value is not None:
                label = next((f"below_{edge}" for edge in breaks if value < edge),
                             f"at_least_{breaks[-1]}")
                groups[label].append(row)
        usable = {key: _regression(group) for key, group in groups.items()
                  if len(group) >= minimum}
        if usable:
            result[field] = usable
    return result


def evaluate(rows: list[EvaluationRow], minimum_samples: int = 30,
             minimum_subgroup: int = 10) -> dict:
    if minimum_samples < 1 or minimum_subgroup < 1:
        raise ValueError("minimum sample counts must be positive")
    ids = [row.sample_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sample_id")
    splits: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row.trip_id:
            splits[row.trip_id].add(row.split)
    if any(len(parts) > 1 for parts in splits.values()):
        raise ValueError("same trip appears in multiple dataset splits")
    canonical = "\n".join(json.dumps(row.model_dump(mode="json"), sort_keys=True,
                                      separators=(",", ":")) for row in rows)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    test_rows = [row for row in rows if row.split == "TEST"]
    by_task: dict[str, list[EvaluationRow]] = defaultdict(list)
    for row in test_rows:
        by_task[row.task].append(row)
    results: dict[str, dict] = {}
    for task in ("RANGE", "ETA", "CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"):
        task_rows = by_task[task]
        if not task_rows:
            results[task] = {"status": "NO_DATA", "sample_count": 0}
            continue
        provenance = {row.source for row in task_rows}
        quality = next(iter(provenance)) if len(provenance) == 1 else "MIXED"
        if len(task_rows) < minimum_samples:
            results[task] = {"status": "INSUFFICIENT_DATA", "sample_count": len(task_rows),
                             "minimum_samples": minimum_samples, "source": quality}
            continue
        if task in ("HEALTH", "CHARGER_AVAILABILITY"):
            metrics = _classification(task_rows)
            if task == "HEALTH":
                leads = [row.lead_time_hours for row in task_rows
                         if row.actual == 1 and row.predicted >= .5
                         and row.lead_time_hours is not None]
                metrics["median_detection_lead_hours"] = round(median(leads), 2) if leads else None
        else:
            metrics = _regression(task_rows)
            if task == "RANGE":
                intervals = [row for row in task_rows if row.interval_low is not None]
                metrics["interval_sample_count"] = len(intervals)
                metrics["interval_coverage"] = (
                    round(sum(row.interval_low <= row.actual <= row.interval_high
                              for row in intervals) / len(intervals), 4)
                    if intervals else None)
                metrics["by_segment"] = _subgroups(task_rows, minimum_subgroup)
            if task == "ETA":
                metrics["map_baseline"] = _regression(task_rows, "baseline_predicted")
                metrics["personal_multiplier"] = _regression(task_rows, "personal_predicted")
            if task == "CHARGER_QUEUE":
                metrics["by_charger_class"] = _subgroups(task_rows, minimum_subgroup).get(
                    "charger_class", {})
        metrics["source"] = quality
        metrics["field_verified"] = quality == "FIELD"
        results[task] = metrics
    return {"dataset_sha256": fingerprint, "total_rows": len(rows),
            "test_rows": len(test_rows), "tasks": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate held-out Trickee predictions")
    parser.add_argument("dataset", help="Canonical JSONL rows with explicit source and split")
    parser.add_argument("--minimum-samples", type=int, default=30)
    parser.add_argument("--minimum-subgroup", type=int, default=10)
    parser.add_argument("--output", help="Write evaluation JSON atomically")
    args = parser.parse_args()
    result = evaluate(load_jsonl(args.dataset), args.minimum_samples,
                      args.minimum_subgroup)
    body = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(target)
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
