"""Measured model evidence must not be inferred from training or synthetic data."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from trickee.model_eval import EvaluationRow, evaluate, load_jsonl


NOW = datetime.now(timezone.utc)


def row(number: int, task: str, predicted: float, actual: float, **extra):
    return EvaluationRow(sample_id=f"sample-{number}", task=task, source="FIELD",
                         split="TEST", observed_at=NOW, predicted=predicted,
                         actual=actual, **extra)


def test_no_data_and_training_only_have_no_metrics():
    empty = evaluate([], minimum_samples=2)
    assert empty["tasks"]["RANGE"] == {"status": "NO_DATA", "sample_count": 0}
    training = row(1, "RANGE", 100, 90).model_copy(update={"split": "TRAIN"})
    result = evaluate([training], minimum_samples=2)
    assert result["test_rows"] == 0
    assert result["tasks"]["RANGE"]["status"] == "NO_DATA"


def test_range_and_eta_compare_held_out_predictions_with_baselines():
    rows = [
        row(1, "RANGE", 90, 100, interval_low=80, interval_high=110,
            vehicle_id="V1", road_type="URBAN", soc_pct=80),
        row(2, "RANGE", 110, 100, interval_low=105, interval_high=115,
            vehicle_id="V1", road_type="URBAN", soc_pct=50),
        row(3, "ETA", 12, 10, baseline_predicted=15, personal_predicted=11),
        row(4, "ETA", 18, 20, baseline_predicted=30, personal_predicted=22),
    ]
    result = evaluate(rows, minimum_samples=2, minimum_subgroup=2)
    range_metrics = result["tasks"]["RANGE"]
    assert range_metrics["mae"] == 10
    assert range_metrics["rmse"] == 10
    assert range_metrics["interval_coverage"] == .5
    assert range_metrics["by_segment"]["vehicle_id"]["V1"]["sample_count"] == 2
    assert range_metrics["field_verified"] is True
    eta = result["tasks"]["ETA"]
    assert eta["mae"] == 2
    assert eta["map_baseline"]["mae"] == 7.5
    assert eta["personal_multiplier"]["mae"] == 1.5


def test_charger_and_health_classification_metrics():
    rows = [
        row(1, "CHARGER_AVAILABILITY", .9, 1),
        row(2, "CHARGER_AVAILABILITY", .1, 0),
        row(3, "HEALTH", .8, 1, lead_time_hours=8),
        row(4, "HEALTH", .7, 0),
        row(5, "HEALTH", .2, 1),
        row(6, "HEALTH", .1, 0),
        row(7, "CHARGER_QUEUE", 3, 5, charger_class="DC"),
        row(8, "CHARGER_QUEUE", 8, 6, charger_class="DC"),
    ]
    result = evaluate(rows, minimum_samples=2, minimum_subgroup=2)["tasks"]
    assert result["CHARGER_AVAILABILITY"]["auroc"] == 1
    assert result["HEALTH"]["true_positive"] == 1
    assert result["HEALTH"]["false_positive"] == 1
    assert result["HEALTH"]["false_negative"] == 1
    assert result["HEALTH"]["precision"] == .5
    assert result["HEALTH"]["median_detection_lead_hours"] == 8
    assert result["CHARGER_QUEUE"]["mae"] == 2
    assert result["CHARGER_QUEUE"]["by_charger_class"]["DC"]["sample_count"] == 2


def test_auc_ties_and_one_class_results():
    tied = [row(1, "HEALTH", .5, 1), row(2, "HEALTH", .5, 0),
            row(3, "HEALTH", .9, 1), row(4, "HEALTH", .1, 0)]
    assert evaluate(tied, minimum_samples=1)["tasks"]["HEALTH"]["auroc"] == .875
    assert evaluate(tied[:1], minimum_samples=1)["tasks"]["HEALTH"]["auroc"] is None


def test_synthetic_results_cannot_be_field_verified():
    rows = [row(1, "RANGE", 100, 100), row(2, "RANGE", 110, 100)]
    rows[1] = rows[1].model_copy(update={"source": "SIMULATED"})
    metrics = evaluate(rows, minimum_samples=2)["tasks"]["RANGE"]
    assert metrics["source"] == "MIXED"
    assert metrics["field_verified"] is False
    assert evaluate(rows[:1], minimum_samples=2)["tasks"]["RANGE"]["status"] == "INSUFFICIENT_DATA"


def test_invalid_rows_and_cross_split_trip_leakage_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        row(1, "HEALTH", 1.5, 1)
    with pytest.raises(ValidationError):
        row(1, "RANGE", 20, 30, interval_low=25, interval_high=40)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate([row(1, "RANGE", 1, 1), row(1, "RANGE", 1, 1)])
    train = row(1, "ETA", 10, 10, trip_id="T1").model_copy(update={"split": "TRAIN"})
    test = row(2, "ETA", 10, 10, trip_id="T1")
    with pytest.raises(ValueError, match="multiple dataset splits"):
        evaluate([train, test])
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(row(1, "RANGE", 100, 101).model_dump_json() + "\n",
                       encoding="utf-8")
    assert load_jsonl(dataset)[0].actual == 101
    dataset.write_text("{invalid json}", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        load_jsonl(dataset)
