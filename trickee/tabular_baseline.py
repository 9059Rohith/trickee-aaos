"""Interpretable charger and health baselines trained on disjoint trip splits.

These are measurement baselines, not safety diagnoses or automatic actions.
Synthetic training/evaluation stays marked SIMULATED in every report.
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


Task = Literal["CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"]


def _bucket(value: float | None, cutoffs: tuple[float, ...]) -> str:
    if value is None:
        return "UNKNOWN"
    for cutoff in cutoffs:
        if value < cutoff:
            return f"BELOW_{cutoff:g}"
    return f"AT_LEAST_{cutoffs[-1]:g}"


def _group(task: Task, row: EvaluationRow) -> str:
    if task == "HEALTH":
        return "|".join((_bucket(row.soh_pct, (60, 75, 85, 95)),
                         _bucket(row.temperature_c, (0, 10, 30, 45, 55))))
    return "|".join((row.charger_class or "UNKNOWN", row.weather or "UNKNOWN"))


class TabularBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model_type: Literal["SHRUNK_GROUP_MEAN_V1"] = "SHRUNK_GROUP_MEAN_V1"
    task: Task
    trained_at: datetime
    training_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_count: int = Field(ge=30)
    source: Literal["FIELD", "SIMULATED", "REPLAY", "MIXED"]
    prior_strength: float = Field(gt=0, le=1000)
    global_mean: float = Field(ge=0)
    groups: dict[str, tuple[int, float]]

    @model_validator(mode="after")
    def valid_model(self) -> "TabularBaseline":
        if self.trained_at.tzinfo is None:
            raise ValueError("training time requires timezone")
        if self.task != "CHARGER_QUEUE" and self.global_mean > 1:
            raise ValueError("classification mean must be a probability")
        if any(count < 1 or total < 0 or
               self.task != "CHARGER_QUEUE" and total > count
               for count, total in self.groups.values()):
            raise ValueError("invalid group training statistics")
        return self

    def predict(self, row: EvaluationRow) -> float:
        if row.task != self.task:
            raise ValueError("prediction task does not match model")
        count, total = self.groups.get(_group(self.task, row), (0, 0.0))
        estimate = (total + self.prior_strength * self.global_mean) / (
            count + self.prior_strength)
        if self.task == "CHARGER_QUEUE":
            return round(max(0.0, estimate), 4)
        return round(min(.9999, max(.0001, estimate)), 4)


def train(rows: list[EvaluationRow], task: Task,
          prior_strength: float = 20.0) -> TabularBaseline:
    if task not in ("CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"):
        raise ValueError("unsupported tabular baseline task")
    if not math.isfinite(prior_strength) or prior_strength <= 0 or prior_strength > 1000:
        raise ValueError("prior strength must be finite and positive")
    # Validate unique samples and trip-level split isolation before training.
    evaluate(rows, minimum_samples=1)
    training = [row for row in rows if row.task == task and row.split == "TRAIN"]
    if len(training) < 30:
        raise ValueError("at least 30 training rows are required")
    totals: dict[str, tuple[int, float]] = {}
    for row in training:
        group = _group(task, row)
        count, total = totals.get(group, (0, 0.0))
        totals[group] = (count + 1, total + row.actual)
    sources = {row.source for row in training}
    canonical = "\n".join(row.model_dump_json() for row in training)
    return TabularBaseline(
        task=task, trained_at=datetime.now(timezone.utc),
        training_dataset_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        training_count=len(training),
        source=next(iter(sources)) if len(sources) == 1 else "MIXED",
        prior_strength=prior_strength,
        global_mean=sum(row.actual for row in training) / len(training),
        groups=totals,
    )


def evaluate_baseline(model: TabularBaseline, rows: list[EvaluationRow],
                      minimum_samples: int = 30) -> dict:
    original = evaluate(rows, minimum_samples=minimum_samples)
    held_out = [row for row in rows if row.task == model.task and row.split == "TEST"]
    adjusted = [row.model_copy(update={"predicted": model.predict(row)})
                for row in held_out]
    measured = evaluate(adjusted, minimum_samples=minimum_samples)
    return {"task": model.task, "training_count": model.training_count,
            "training_source": model.source,
            "original": original["tasks"][model.task],
            "group_baseline": measured["tasks"][model.task],
            "test_rows": len(held_out)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Train held-out charger/health baselines")
    parser.add_argument("dataset")
    parser.add_argument("--task", required=True,
                        choices=("CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"))
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--prior-strength", type=float, default=20.0)
    args = parser.parse_args()
    rows = load_jsonl(args.dataset)
    model = train(rows, args.task, args.prior_strength)
    report = evaluate_baseline(model, rows)
    for path, body in ((Path(args.artifact), model.model_dump_json(indent=2)),
                       (Path(args.report), json.dumps(report, indent=2))):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(body + "\n", encoding="utf-8")
        temporary.replace(path)
    print(json.dumps({"task": model.task, "source": model.source,
                      "artifact": args.artifact, "report": args.report}))


if __name__ == "__main__":
    main()
