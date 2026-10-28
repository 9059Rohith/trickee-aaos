from datetime import datetime, timezone

import pytest

from trickee.model_eval import EvaluationRow
from trickee.synthetic_dataset import generate
from trickee.tabular_baseline import TabularBaseline, evaluate_baseline, train


@pytest.mark.parametrize("task", ["CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"])
def test_held_out_baseline_is_measured_and_explicitly_synthetic(task):
    rows = generate(400)
    model = train(rows, task)
    report = evaluate_baseline(model, rows)
    assert model.training_count == 320
    assert model.source == "SIMULATED"
    assert report["test_rows"] == 40
    assert report["group_baseline"]["status"] == "MEASURED"
    assert report["group_baseline"]["field_verified"] is False
    restored = TabularBaseline.model_validate_json(model.model_dump_json())
    sample = next(row for row in rows if row.task == task and row.split == "TEST")
    assert restored.predict(sample) == model.predict(sample)
    if task != "CHARGER_QUEUE":
        assert 0 < model.predict(sample) < 1


def test_queue_group_model_reduces_seeded_weather_bias():
    rows = generate(1200)
    report = evaluate_baseline(train(rows, "CHARGER_QUEUE"), rows)
    assert report["group_baseline"]["mae"] < report["original"]["mae"]


def test_training_rejects_leakage_small_data_and_invalid_prior():
    rows = generate(40)
    original = next(row for row in rows if row.task == "HEALTH" and row.split == "TRAIN")
    leaked = EvaluationRow.model_validate({
        **original.model_dump(), "sample_id": "leaked-health",
        "split": "TEST", "observed_at": datetime.now(timezone.utc),
    })
    with pytest.raises(ValueError, match="multiple dataset splits"):
        train(rows + [leaked], "HEALTH")
    with pytest.raises(ValueError, match="positive"):
        train(rows, "HEALTH", prior_strength=0)
    with pytest.raises(ValueError, match="30 training"):
        train([row for row in rows if row.split == "TEST"], "HEALTH")
    model = train(rows, "HEALTH")
    wrong_task = next(row for row in rows if row.task == "ETA")
    with pytest.raises(ValueError, match="does not match"):
        model.predict(wrong_task)
